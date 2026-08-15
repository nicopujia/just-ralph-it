from typing import TYPE_CHECKING, Any, Literal, cast, override

from openai.types.responses import ResponseInputParam

from jri.core.ai import prompts
from jri.core.notes import Connection, Notebook, NoteId, ReadQuery, TopicId
from jri.core.settings import Settings
from jri.core.workspace import Workspace
from jri.lib import prompt
from jri.lib.models import estimate_tokens, get_context_limit

from .base import Agent, Stream, Tool, ToolOutput, tool
from .explorer import Explorer

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputItemParam


class Interviewer(Agent):
    CONTEXT_THRESHOLD = 0.4
    FALLBACK_CONTEXT_LIMIT = 100_000
    MIN_CONTEXT_TURNS = 10
    FIRST_MESSAGE = "What do you want to make?"
    EXCERPT_SCOPE = prompts.read("interviewer_excerpt_scope")

    def __init__(self, settings: Settings, notebook: Notebook) -> None:
        self.settings = settings
        self.notebook = notebook
        self.offered_ralphing = False
        self.generated_project = False
        self.initial_topic = notebook.initial_topic
        self.active_topic_id = self.initial_topic.id
        super().__init__(
            client=settings.llm.client,
            profile=settings.agents.interviewer,
            prompt=prompts.read("interviewer"),
            initial_context=[{"role": "assistant", "content": self.FIRST_MESSAGE}],
        )

    @override
    def get_context(self) -> ResponseInputParam:
        pinned: ResponseInputItemParam = {
            "role": "system",
            "content": (
                f"{self.EXCERPT_SCOPE}\n\nCurrent topic: {self.active_topic_id}\n\n"
                f"{prompt.render(project_excerpt=self.notebook.render(self.active_topic_id))}"
            ),
        }
        turns: list[ResponseInputParam] = []
        for raw_item in self.history[1:]:
            item = cast("dict[str, Any]", raw_item)
            if ("role" in item and item["role"] == "user") or not turns:
                turns.append([])
            turns[-1].append(raw_item)
        tools = [item.definition for item in self.get_tools()]
        context: ResponseInputParam = [self.history[0], pinned, *(item for turn in turns for item in turn)]
        budget = get_context_limit(self.profile.model, self.FALLBACK_CONTEXT_LIMIT) * self.CONTEXT_THRESHOLD
        while len(turns) > self.MIN_CONTEXT_TURNS and estimate_tokens(context, tools) > budget:
            turns.pop(0)
            context = [self.history[0], pinned, *(item for turn in turns for item in turn)]
        return context

    # A generated project is one that JRI cannot change yet, so take the offer away once a run reports no ambiguities.
    # Keep the tool itself, because a rewind replays the call that made an earlier offer.
    @override
    def get_tools(self) -> list[Tool]:
        if not self.generated_project:
            return self.tools
        return [item for item in self.tools if item.name != self.offer_ralphing.__name__]

    @tool(
        (
            "Offer the user the Just Ralph It control, without triggering Ralph. "
            "Call it when you and the user agree the definition is complete, and explain that this displays a button "
            "and that only the user can trigger Ralph by clicking it."
        ),
        started_label="Offering you to just Ralph it",
        finished_label="Offered you to just Ralph it",
        symbol="🪏",
    )
    def offer_ralphing(self) -> str:
        self.offered_ralphing = True
        return "Offered the Just Ralph It button."

    @tool(
        (
            "Gather context through a telegraphic query, including anything from the web or this computer. "
            "Queries can be as broad as needed, so unify all your inquiries in a single call. "
            'Format query so it reads well after "Exploring [...]" text'
        ),
        started_label="Exploring {query}",
        finished_label="Explored {query}",
        symbol="🔎",
        replayed=False,
    )
    def explore(self, query: str) -> Stream:
        report = yield from Explorer(self.settings, Workspace.find().root).report(query)
        if not report:
            yield ToolOutput("Exploration produced no report.", "empty")
            return
        # A model creates this report from web content. Quote it because JRI text can follow a long report.
        yield ToolOutput(prompt.render(exploration_report=report))

    @tool(
        (
            "Turn to a project topic by its name or ID, creating it when it does not exist."
            "Capture unresolved unknowns as notes before switching away from a topic."
        ),
        started_label="Switching to {topic}",
        finished_label="Switched to {topic}",
        symbol="📑",
    )
    def switch_topic(self, topic: str) -> str:
        value = topic.strip()
        resolved = self.notebook.find_topic(value)
        if resolved is None:
            if any(note.id == value for note in self.notebook.graph.notes):
                raise ValueError(f"Note `{value}` is not a topic.")
            resolved = self.notebook.add_topic(value)
        if resolved.status == "trashed":
            raise ValueError(f"Topic `{resolved.id}` is trashed. Restore it before switching.")
        self.active_topic_id = resolved.id
        return f"Switched to {resolved.id}."

    @tool(
        "Set a topic's status and optionally replace its summary.",
        started_label="Updating topic",
        finished_label="Updated topic",
        symbol="📑",
    )
    def update_topic(
        self, topic_id: TopicId, status: Literal["open", "done", "trashed"], summary: str | None = None
    ) -> str:
        if topic_id == self.initial_topic.id and status == "trashed":
            raise ValueError(f"The overview topic `{topic_id}` cannot be trashed.")
        topic = self.notebook.update_topic(topic_id, status, summary)
        if topic.id == self.active_topic_id and topic.status == "trashed":
            self.active_topic_id = self.initial_topic.id
        return f"Updated {topic.id} ({topic.status})."

    @tool(
        (
            "Read all notes when called without a query. Set `query.text` for fuzzy search, `query.ids` for exact "
            "lookup, `query.topic_ids` to filter by topic, or `query.traverse_from` with `direction` and `depth` for "
            "graph traversal."
        ),
        started_label="Reading notes",
        finished_label="Read notes",
        symbol="📖",
        strict=False,
        replayed=False,
    )
    def read_notes(self, query: ReadQuery | None = None) -> str:
        notes, connections = self.notebook.read(query or ReadQuery())
        if not notes:
            return "No notes found."
        # Render each edge as in the pinned excerpt. The model must see one format throughout the notebook.
        return prompt.render(
            notes={note.id: note.text for note in notes},
            connections=[f"{item.source_id} {item.label} {item.target_id}" for item in connections] or None,
        )

    @tool(
        (
            "Capture one or more independently meaningful ideas under the active topic. "
            "Switch to the relevant topic before capturing notes that belong to it. "
            "Check whether an existing note already covers the idea, and edit that one instead."
        ),
        started_label="Taking notes",
        finished_label="Took notes",
        symbol="📝",
    )
    def capture_notes(self, texts: list[str]) -> str:
        notes = self.notebook.add(texts, self.active_topic_id)
        return f"Added notes: {', '.join(note.id for note in notes)}."

    @tool(
        "Edit one note's text without changing its connections.",
        started_label="Editing note",
        finished_label="Edited note",
        symbol="✏️",
    )
    def edit_note(self, note_id: NoteId, text: str) -> str:
        note = self.notebook.edit(note_id, text)
        return f"Edited {note.id}."

    @tool(
        "Delete notes and every semantic connection touching them.",
        started_label="Discarding notes",
        finished_label="Discarded notes",
        symbol="🗑️",
    )
    def delete_notes(self, note_ids: list[NoteId]) -> str:
        return f"Deleted notes: {', '.join(self.notebook.delete(note_ids))}."

    @tool(
        (
            "Create directed, labeled semantic connections between notes and/or topics, "
            "useful to express relationships and/or hierarchy between them. "
            "Connect a note and a topic only when the label states something that placement does not, "
            "as the note already sits under its topic."
        ),
        started_label="Organizing notes",
        finished_label="Organized notes",
        symbol="🖇️",
    )
    def connect_notes(self, connections: list[Connection]) -> str:
        return f"Connected {self.notebook.connect(connections)} relationship(s)."

    @tool(
        "Remove directed, labeled semantic connections between notes and/or topics.",
        started_label="Reorganizing notes",
        finished_label="Reorganized notes",
        symbol="📎",
    )
    def disconnect_notes(self, connections: list[Connection]) -> str:
        return f"Disconnected {self.notebook.disconnect(connections)} relationship(s)."
