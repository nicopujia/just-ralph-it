from typing import TYPE_CHECKING, Any, Literal, cast, override

from openai.types.responses import ResponseInputParam

from jri.core.ai import prompts
from jri.core.ai.agent import Agent
from jri.core.ai.tool import Stream, Tool, ToolOutput, tool
from jri.core.notes import Connection, Notebook, NoteId, ReadQuery, TopicId
from jri.core.settings import Settings
from jri.core.workspace import Workspace
from jri.lib import prompt
from jri.lib.context import estimate_tokens, measure_item, measure_request
from jri.lib.models_dot_dev import get_limit

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
        budget = get_limit(self.profile.model, self.FALLBACK_CONTEXT_LIMIT) * self.CONTEXT_THRESHOLD
        # Weigh each turn once, and take the weight of a dropped turn off the total. Weighing the whole context
        # again for each dropped turn would make this grow with the square of the interview length.
        weights = [sum(measure_item(item) for item in turn) for turn in turns]
        total = measure_request([self.history[0], pinned], tools) + sum(weights)
        dropped = 0
        while len(turns) - dropped > self.MIN_CONTEXT_TURNS and estimate_tokens(total) > budget:
            total -= weights[dropped]
            dropped += 1
        return [self.history[0], pinned, *(item for turn in turns[dropped:] for item in turn)]

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
            "Turn to a project topic by its name or ID, creating it when it does not exist. "
            "A topic being created needs a summary, and stands under `parent`, or under the overview topic when "
            "`parent` is not given. "
            "`summary` and `parent` are ignored for a topic that exists, so change them with `update_topic`. "
            "Capture unresolved unknowns as notes before switching away from a topic."
        ),
        started_label="Switching to {topic}",
        finished_label="Switched to {topic}",
        symbol="📑",
    )
    def switch_topic(self, topic: str, parent: str | None = None, summary: str | None = None) -> str:
        value = topic.strip()
        resolved = self.notebook.find_topic(value)
        if resolved is None:
            if any(note.id == value for note in self.notebook.graph.notes):
                raise ValueError(f"Note `{value}` is not a topic.")
            given_summary = _omit_blank(summary)
            if given_summary is None:
                raise ValueError(f"Topic `{value}` does not exist. Give it a summary to create it.")
            given_parent = _omit_blank(parent)
            parent_id = self.initial_topic.id if given_parent is None else self._resolve_topic(given_parent)
            resolved = self.notebook.add_topic(value, parent_id, given_summary)
        elif resolved.id in self.notebook.trashed_topic_ids:
            raise ValueError(f"Topic `{resolved.id}` is trashed. Restore it before switching.")
        self.active_topic_id = resolved.id
        return f"Switched to {resolved.id}."

    @tool(
        "Set a topic's status, and optionally replace its summary, its name, or the topic it stands under.",
        started_label="Updating topic",
        finished_label="Updated topic",
        symbol="📑",
    )
    def update_topic(
        self,
        topic_id: TopicId,
        status: Literal["open", "done"] | None = None,
        summary: str | None = None,
        name: str | None = None,
        parent: str | None = None,
    ) -> str:
        given_parent = _omit_blank(parent)
        topic = self.notebook.update_topic(
            topic_id,
            status=status,
            summary=_omit_blank(summary),
            name=_omit_blank(name),
            parent_id=None if given_parent is None else self._resolve_topic(given_parent),
        )
        return f"Updated {topic.id} ({topic.status})."

    @tool(
        (
            "Read all notes when called without a query. Set `query.text` for fuzzy search, `query.ids` for exact "
            "lookup, `query.topic_ids` to filter by topics and everything under them, or `query.traverse_from` with "
            "`direction` and `depth` for graph traversal."
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
        ("Move notes to another topic, keeping their connections."),
        started_label="Moving notes",
        finished_label="Moved notes",
        symbol="📦",
    )
    def move_notes(self, note_ids: list[NoteId], topic_id: TopicId) -> str:
        return f"Moved notes: {', '.join(self.notebook.move(note_ids, topic_id))}."

    @tool(
        "Delete notes for good, with every connection that touches them. To discard a topic, trash it instead.",
        started_label="Deleting notes",
        finished_label="Deleted notes",
        symbol="✂️",
    )
    def delete_notes(self, note_ids: list[NoteId]) -> str:
        return f"Deleted notes: {', '.join(self.notebook.delete(note_ids))}."

    @tool(
        "Trash topics, with everything under them. The notes stay, and `update_topic` with the status `open` "
        "brings it all back.",
        started_label="Trashing topics",
        finished_label="Trashed topics",
        symbol="🗑️",
    )
    def trash_topics(self, topic_ids: list[TopicId]) -> str:
        self.notebook.trash(topic_ids)
        if self.active_topic_id in self.notebook.trashed_topic_ids:
            self.active_topic_id = self.initial_topic.id
        return f"Trashed topics: {', '.join(topic_ids)}."

    @tool(
        (
            "Create directed, labeled semantic connections between notes, "
            "useful to express relationships and/or hierarchy between them."
        ),
        started_label="Organizing notes",
        finished_label="Organized notes",
        symbol="🖇️",
    )
    def connect_notes(self, connections: list[Connection]) -> str:
        return f"Connected {self.notebook.connect(connections)} relationship(s)."

    @tool(
        "Remove directed, labeled semantic connections between notes.",
        started_label="Reorganizing notes",
        finished_label="Reorganized notes",
        symbol="📎",
    )
    def disconnect_notes(self, connections: list[Connection]) -> str:
        return f"Disconnected {self.notebook.disconnect(connections)} relationship(s)."

    # A model names a topic the way it names one everywhere else, so resolve a name or an ID here and let the
    # notebook see an ID.
    def _resolve_topic(self, value: str) -> str:
        resolved = self.notebook.find_topic(value)
        if resolved is None:
            raise ValueError(f"Unknown topic `{value}`.")
        return resolved.id


# The tools are strict, so a model must send every property. A model that does not want a property sends an empty
# string. Read a blank value as a property the model did not send.
def _omit_blank(value: str | None) -> str | None:
    return (value or "").strip() or None
