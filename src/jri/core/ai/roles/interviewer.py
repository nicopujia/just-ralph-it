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

from .explorer import Exploration, Explorer

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputItemParam


class Interviewer(Agent):
    CONTEXT_THRESHOLD = 0.4
    # A drop takes the request down to this share of the limit, and not to the threshold above it. It thus frees
    # more than a third of the budget at one time. The interview fills that space again over many turns, and each
    # of those turns starts with the same bytes as the turn before it, which the provider serves from its cache.
    # A drop that stopped at the threshold would free one turn, and the next turn would drop again.
    CONTEXT_TARGET = 0.25
    FALLBACK_CONTEXT_LIMIT = 100_000
    MIN_CONTEXT_TURNS = 10
    FIRST_MESSAGE = "What do you want to make?"
    # This record replaces an exploration report. No tool can read an exploration again. The record tells the
    # model that the report is gone, and that only the summary below it is left.
    EXPLORATION_RECORD = (
        "[This exploration report was taken out of the message to make room. Nothing holds it now, and the "
        "summary below is all that is left of it.]"
    )
    EXCERPT_SCOPE = prompts.read("interviewer_excerpt_scope")

    def __init__(self, settings: Settings, notebook: Notebook) -> None:
        self.settings = settings
        self.notebook = notebook
        self.offered_ralphing = False
        self.generated_project = False
        self.initial_topic = notebook.initial_topic
        self.active_topic_id = self.initial_topic.id
        self.dropped_turns = 0
        super().__init__(
            client=settings.llm.client,
            profile=settings.agents.interviewer,
            prompt=prompts.read("interviewer"),
            initial_context=[{"role": "assistant", "content": self.FIRST_MESSAGE}],
        )

    @override
    def get_context(self) -> ResponseInputParam:
        self._summarize_explorations()
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
        limit = get_limit(self.profile.model, self.FALLBACK_CONTEXT_LIMIT)
        # A rewind takes turns out of the history, and the count of dropped ones can then stand past its end.
        # Bring the count back to what the shorter history holds.
        self.dropped_turns = min(self.dropped_turns, max(len(turns) - self.MIN_CONTEXT_TURNS, 0))
        # Weigh each turn once, and take the weight of a dropped turn off the total. Weighing the whole context
        # again for each dropped turn would make this grow with the square of the interview length.
        weights = [sum(measure_item(item) for item in turn) for turn in turns]
        total = measure_request([self.history[0], pinned], tools) + sum(weights[self.dropped_turns :])
        # A turn that stays dropped keeps the start of each later request the same as the start of this one.
        if estimate_tokens(total) > limit * self.CONTEXT_THRESHOLD:
            while (
                len(turns) - self.dropped_turns > self.MIN_CONTEXT_TURNS
                and estimate_tokens(total) > limit * self.CONTEXT_TARGET
            ):
                total -= weights[self.dropped_turns]
                self.dropped_turns += 1
        # The excerpt stands last, after the turns. It changes each time a note changes, and everything behind a
        # changed item is new bytes that no cache holds. Behind it there is now nothing.
        return [self.history[0], *(item for turn in turns[self.dropped_turns :] for item in turn), pinned]

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
        exploration = yield from Explorer(self.settings, Workspace.find().root).report(query)
        # An exploration that the user stopped has no result. It is the same as an exploration that found
        # nothing.
        exploration = exploration if exploration is not None else Exploration(report="", summary="", remaining="")
        if not exploration.report:
            yield ToolOutput("Exploration produced no report.", "empty")
            return
        # A model creates this report from web content. Quote it because JRI text can follow a long report.
        # The summary replaces the report in a turn when the full report no longer fits.
        yield ToolOutput(prompt.render(exploration_report=exploration.report), summary=exploration.summary)

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

    # Ten reports at the size limit of a tool output are too large for the interview. A drop of all the turns
    # cannot free sufficient space. A report that stays whole also pushes the interview out of the request.
    # Each recorded exploration but the newest becomes its summary, and the newest stays whole at any size.
    # JRI replaces the report in the history, before it measures the request. A replaced report can then never
    # come back, and turns drop only if the request is still too large. Each later request repeats these same
    # bytes, which the provider gives from its cache.
    def _summarize_explorations(self) -> None:
        explorations = [
            item
            for item in cast("list[dict[str, Any]]", self.history)
            if item.get("type") == "function_call_output" and item.get("call_id") in self.output_summaries
        ]
        # The summary comes from a model, so quote it. A later round renders it again and gets the same bytes.
        # It then replaces a replaced report with the same record.
        for item in explorations[:-1]:
            summary = self.output_summaries[cast("str", item["call_id"])]
            item["output"] = f"{self.EXPLORATION_RECORD}\n\n{prompt.render(exploration_summary=summary)}"

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
