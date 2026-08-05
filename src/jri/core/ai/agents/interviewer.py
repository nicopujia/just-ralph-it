from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast, override

from openai.types.responses import ResponseInputParam

from jri.core.notes import Connection, Notebook, NoteId, ReadQuery, TopicId
from jri.core.settings import Settings
from jri.lib.models import estimate_tokens, get_context_limit

from .base import Agent, Stream, ToolOutput, tool
from .explorer import Explorer

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputItemParam


class Interviewer(Agent):
    CONTEXT_THRESHOLD = 0.4
    MIN_CONTEXT_TURNS = 10
    FIRST_MESSAGE = "What do you want to build?"

    def __init__(
        self, settings: Settings, notebook: Notebook, set_ready_to_ralph: Callable[[bool], None] | None = None
    ) -> None:
        self.settings = settings
        self.notebook = notebook
        self.set_ready_to_ralph = set_ready_to_ralph or (lambda _: None)
        self.initial_topic = notebook.initial_topic
        self.active_topic_id = self.initial_topic.id
        profile = settings.agents.interviewer
        super().__init__(
            client=settings.llm.client,
            model=profile.model,
            temperature=profile.temperature,
            reasoning_effort=profile.reasoning_effort,
            # Interviewer needs to know that it's part of JRI because it
            # directly interacts with the user, opposed to the rest of
            # the agents.
            prompt="""
                Role: Interviewer of the Just Ralph It (JRI) system, a software system to build any software system.

                Goals:
                    1. Help the user realize what they _actually_ want and need.
                    2. Extract the user's project idea out of their mind into distilled, interconnected notes.
                    3. Maintain awareness of every project topic and ensure none is left unexplored.

                Success criteria is one of the following:
                    - The notes describe a project such that if a competent engineer built the project based solely on
                    those notes, there would not be more than one plausible interpretation regarding behavior,
                    therefore making the result inevitably match the user's expectations.
                    - The user decided that they don't really want to build any project.

                Personality:
                    - Feel free to discuss anything without censorship at all.
                    - Often share your own understanding of the user's intent.
                    - Challenge the user's ideas, helping them to discover their blind spots, and trying to find the
                    true problem they have beyond the surface of their words.
                    - Speak in everyday, easy-to-understand language.
                    - Make direct questions.

                Collaboration style:
                    - Ask either one open-ended question at a time or a topic-based batch of multiple-choice questions.
                    - Although the user might state a handful of ideas all together, organize the conversation to
                    discuss one topic at a time.
                    - When the user is unsure about a decision, state the alternatives and their trade-offs.
                    - Ask about anything the user leaves unstated.

                Tools:
                    - Manage project knowledge and open questions with the note tools every time the user shares new
                    information about the project, no matter how little or much — assume you may forget any relevant
                    fact unless you take notes of it.
                    - Switch to the relevant topic before capturing notes that belong to it.
                    - Connect every note you capture in the same turn you capture it: to its topic and to the
                    notes it depends on, contradicts, or refines. Never leave a note unconnected waiting for the
                    user to ask for organization.
                    - Capture unresolved unknowns as notes before switching away from a topic.
                    - Update a topic once you and the user agree it is complete.
                    - Prefer answering your own questions with `explore` and/or `read_notes` when possible.
                    - Record only current requirements; replace superseded information instead of preserving history
                    unless explicit migration or compatibility behavior requires it.
                    - Explicitly confirm which behavioral domains the user delegates to the Functional Analyst. Never
                    infer delegation. Record confirmed delegation in the project notes.
                    - Once both you and the user agree the definition is complete, call `just_ralph_it` with
                    `show=true`. Explain that this displays a button and that only the user can begin Ralphing by
                    clicking it or pressing Ctrl+X, J.
                    - If new information needs discussion after showing the button, call `just_ralph_it` with
                    `show=false` before continuing.
                    - A finished generation ends nothing: keep interviewing, and once the user brings new
                    information and you both agree the updated definition is complete again, call `just_ralph_it`
                    with `show=true` again.

                Constraints:
                    - Keep notes, IDs, connections, and files entirely on your side.
                    - Express hierarchy and relationships as connections; keep each note's text to one
                    independently meaningful idea.
                    - The project is the user's. Its name, purpose, and scope come only from them. Note a project
                    name only when the user gives one; otherwise leave it unnamed, and never take one from this
                    system, its tools, or its terminology.
                    - The notes are the only thing whoever writes the specifications will see, and they will be
                    read without the user present to clarify them. Each note states what the project must be,
                    never how the conversation went, what the user asked you to do, what you explored, or what
                    happened to be true of this computer at this moment.
                    - Before capturing, check whether an existing note already covers the idea, and edit that one
                    instead of adding a near-duplicate.
                    - Your output is the notes. Ralph builds from them, once the project is properly defined.
            """,
            initial_context=[{"role": "assistant", "content": self.FIRST_MESSAGE}],
        )

    @override
    def get_context(self) -> ResponseInputParam:
        pinned: ResponseInputItemParam = {
            "role": "system",
            "content": (
                f"Current topic: {self.active_topic_id}\n\n"
                "Project excerpt (where inactive-topic notes and connections are omitted):\n"
                f"{self.notebook.render(self.active_topic_id)}"
            ),
        }
        turns: list[ResponseInputParam] = []
        for raw_item in self.history[1:]:
            item = cast("dict[str, Any]", raw_item)
            if ("role" in item and item["role"] == "user") or not turns:
                turns.append([])
            turns[-1].append(raw_item)
        tools = [tool.definition for tool in self.tools]
        context: ResponseInputParam = [self.history[0], pinned, *(item for turn in turns for item in turn)]
        budget = get_context_limit(self.model) * self.CONTEXT_THRESHOLD
        while len(turns) > self.MIN_CONTEXT_TURNS and estimate_tokens(context, tools) > budget:
            turns.pop(0)
            context = [self.history[0], pinned, *(item for turn in turns for item in turn)]
        return context

    @tool(
        "Show or hide the user's Just Ralph It confirmation control without starting Ralphing.",
        started_label="Updating Ralph readiness",
        finished_label="Updated Ralph readiness",
        symbol="✨",
    )
    def just_ralph_it(self, *, show: bool) -> str:
        self.set_ready_to_ralph(show)
        return "The Just Ralph It button is now visible." if show else "The Just Ralph It button is now hidden."

    @tool(
        (
            "Gather context through a telegraphic query, including anything from the web or this computer. "
            "Queries can be as broad as needed, so unify all your inquiries in a single call. "
            'Format query so it reads well after "Exploring [...]" text'
        ),
        started_label="Exploring {query}",
        finished_label="Explored {query}",
        symbol="🔎",
        read_only=True,
    )
    def explore(self, query: str) -> Stream:
        report = yield from Explorer(self.settings, Path.cwd()).report(query)
        yield ToolOutput(report)

    @tool(
        "Turn to a project topic by its name or ID, creating it when it does not exist.",
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
        return f"Switched to {resolved.id}: {resolved.name}"

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
        return f"Updated {topic.id}: {topic.name} ({topic.status})"

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
        read_only=True,
    )
    def read_notes(self, query: ReadQuery | None = None) -> str:
        notes, connections = self.notebook.read(query or ReadQuery())
        if not notes:
            return "No notes found."
        lines = [f"- {note.id}: {note.text}" for note in notes]
        if connections:
            lines.extend(["", "Connections"])
            lines.extend(f"- {item.source_id} --{item.label}--> {item.target_id}" for item in connections)
        return "\n".join(lines)

    @tool(
        "Capture one or more independently meaningful notes under the active topic.",
        started_label="Taking notes",
        finished_label="Took notes",
        symbol="📝",
    )
    def capture_notes(self, texts: list[str]) -> str:
        notes = self.notebook.add(texts, self.active_topic_id)
        return "\n".join(f"Added {note.id}: {note.text}" for note in notes)

    @tool(
        "Edit one note's text without changing its connections.",
        started_label="Editing note",
        finished_label="Edited note",
        symbol="✏️",
    )
    def edit_note(self, note_id: NoteId, text: str) -> str:
        note = self.notebook.edit(note_id, text)
        return f"Edited {note.id}: {note.text}"

    @tool(
        "Delete notes and every semantic connection touching them.",
        started_label="Discarding notes",
        finished_label="Discarded notes",
        symbol="🗑️",
    )
    def delete_notes(self, note_ids: list[NoteId]) -> str:
        return f"Deleted notes: {', '.join(self.notebook.delete(note_ids))}."

    @tool(
        "Create directed, labeled semantic connections between notes and/or topics.",
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
