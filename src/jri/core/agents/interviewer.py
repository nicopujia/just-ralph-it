from collections.abc import Generator
from typing import TYPE_CHECKING, Literal, override

from jri.core.notes import ConnectionInput, Notes
from jri.core.settings import Settings

from .explorer import Explorer
from .shared import Agent, TextDelta, ToolCallFinished, ToolCallStarted, ToolOutput, tool

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputItemParam


class Interviewer(Agent):
    """Agent that interviews the user to extract a project idea."""

    FIRST_MESSAGE = "What do you want to build?"

    def __init__(self, settings: Settings, notes: Notes) -> None:
        self.settings = settings
        self.notes = notes
        self.explorer: Explorer
        self.explorations: dict[str, list[ResponseInputItemParam]] = {}
        super().__init__(
            client=settings.llm_client,
            model=settings.interviewer_model,
            temperature=settings.interviewer_temperature,
            reasoning_effort=settings.interviewer_reasoning_effort,
            sys_prompt="""
                Role: Interviewer of the Just Ralph It (JRI) system, a tool to build any software project.

                Goals:
                    1. Help the user realize what they _actually_ want and need.
                    2. Extract the user's project idea out of their mind into distilled, interconnected notes.

                Success criteria is one of the following:
                    - The notes describe a project such that if a competent engineer built the project based solely on
                    the those notes, there would not be more than one plausible interpretation regarding behavior,
                    therefore making the result inevitably match the user's expectations.
                    - The user decided that they don't really want to build any project.

                Personality:
                    - Often share your own understanding of the user's intent.
                    - Challenge the user's ideas, helping them to discover their blind spots, and trying to find the
                    true problem they have beyond the surface of their words.
                    - Make direct questions.

                Collaboration style:
                    - Ask either one open-ended question at a time or a topic-based batch of multiple-choice questions.
                    - Although the user might state a handful of ideas all together, organize the conversation to
                    discuss one topic at a time. Also take note of the questions you can think of after the user shares
                    their ideas, so you can make them later.
                    - If the user is not sure about a decision, state alternatives and their trade-offs, not opinions.
                    - Don't make assumptions.

                Tools:
                    - Manage project knowledge and open questions proactively with the note tools.
                    - Assume you may forget any relevant fact unless you take notes of it.
                    - Prefer answering your own questions with `explore` and/or `read_notes` when possible.

                Constraints:
                    - Don't ask the user to manage notes, IDs, connections, or files.
                    - Each note must contain one independently meaningful idea.
                    - Connect notes to express hierarchy and relationships; do not encode structure in note text.
                    - You don't build. Ralph does, and it does so only after the project is properly defined.

                Stop rule: both you and the user agree that there is nothing relevant left to discuss.
            """,
            initial_ctx=[{"role": "assistant", "content": self.FIRST_MESSAGE}],
        )

    @tool(
        (
            "Gather context through a free-form query, including anything from the web or this computer."
            "Queries can be as broad as needed, so unify all your inquiries in a single call."
        ),
        started_label="Exploring {query}",
        finished_label="Explored {query}",
        symbol="🔎",
    )
    def explore(self, query: str) -> Generator[ToolCallStarted | ToolCallFinished | ToolOutput]:
        """Gather extra context for the user request.

        Yields:
            Explorer tool events followed by its final text output.
        """

        self.explorer = Explorer(self.settings)
        latest_output: list[str] = []
        for event in self.explorer.send_message(query):
            match event:
                case ToolCallStarted():
                    latest_output.clear()
                    yield event
                case ToolCallFinished():
                    yield event
                case TextDelta():
                    latest_output.append(event.text)
        yield ToolOutput("".join(latest_output))

    @tool(
        "Read all notes when called without arguments. Set `query` for fuzzy search, `ids` for exact "
        "lookup, or `traverse_from` with `direction` and `depth` for graph traversal.",
        started_label="Reading notes",
        finished_label="Read notes",
        symbol="📖",
        strict=False,
    )
    def read_notes(
        self,
        query: str | None = None,
        ids: list[str] | None = None,
        traverse_from: list[str] | None = None,
        direction: Literal["outgoing", "incoming", "both"] | None = None,
        depth: int | None = None,
    ) -> str:
        """Read relevant project notes.

        Returns:
            Matching notes and connections.
        """
        notes, connections = self.notes.read(query, ids, traverse_from, direction, depth)
        if not notes:
            return "No notes found."
        lines = [f"- {note.id}: {note.text}" for note in notes]
        if connections:
            lines.extend(["", "Connections"])
            lines.extend(f"- {item.source_id} --{item.label}--> {item.target_id}" for item in connections)
        return "\n".join(lines)

    @tool(
        "Create one or more independently meaningful notes.",
        started_label="Taking notes",
        finished_label="Took notes",
        symbol="📝",
    )
    def add_notes(self, texts: list[str]) -> str:
        """Add project notes.

        Returns:
            A summary containing the new IDs.
        """
        return "\n".join(f"Added {note.id}: {note.text}" for note in self.notes.add(texts))

    @tool(
        "Edit one note's text without changing its connections.",
        started_label="Editing note {note_id}",
        finished_label="Edited note {note_id}",
        symbol="✏️",
    )
    def edit_note(self, note_id: str, text: str) -> str:
        """Edit a project note.

        Returns:
            A summary of the edited note.
        """
        note = self.notes.edit(note_id, text)
        return f"Edited {note.id}: {note.text}"

    @tool(
        "Delete notes and every connection touching them.",
        started_label="Discarding notes",
        finished_label="Discarded notes",
        symbol="🗑️",
    )
    def delete_notes(self, note_ids: list[str]) -> str:
        """Delete project notes.

        Returns:
            A summary of the deleted notes.
        """
        deleted_ids = self.notes.delete(note_ids)
        return f"Deleted notes: {', '.join(deleted_ids)}."

    @tool(
        "Create directed, labeled connections between notes.",
        started_label="Organizing notes",
        finished_label="Organized notes",
        symbol="🖇️",
    )
    def connect_notes(self, connections: list[ConnectionInput]) -> str:
        """Connect project notes.

        Returns:
            The number of relationships created.
        """
        count = self.notes.connect(connections)
        return f"Connected {count} relationship(s)."

    @tool(
        "Remove directed, labeled connections between notes atomically.",
        started_label="Reorganizing notes",
        finished_label="Reorganized notes",
        symbol="📎",
    )
    def disconnect_notes(self, connections: list[ConnectionInput]) -> str:
        """Disconnect project notes.

        Returns:
            The number of relationships removed.
        """
        count = self.notes.disconnect(connections)
        return f"Disconnected {count} relationship(s)."

    @override
    def _after_tool_call(self, call_id: str, name: str) -> None:
        if name == "explore":
            self.explorations[call_id] = self.explorer.ctx
