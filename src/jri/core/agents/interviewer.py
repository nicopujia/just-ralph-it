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
            reasoning_effort=settings.interviewer_reasoning_effort,
            sys_prompt="""
                You are the Interviewer of the Just Ralph It (JRI) system,
                which is a tool to build any software project.

                Your task is extract the full project idea that the user wants
                to build out of their mind.

                Rules:
                - Prefer answering questions with `explore` tool when possible.
                - Manage project knowledge proactively with the note tools.
                - Each note must contain one independently meaningful idea.
                - Read existing notes before asking about information that may already be known.
                - Connect notes to express hierarchy and relationships; do not encode structure in note text.
                - Do not ask the user to manage notes, IDs, connections, or files.
            """,
            initial_ctx=[{"role": "assistant", "content": self.FIRST_MESSAGE}],
        )

    @tool(
        "Gather context through a natural language query, including anything from the web or this computer.",
        started_label='Exploring "{query}"',
        finished_label='Explored "{query}"',
        symbol="🔍",
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
        "Read all notes when called without arguments. Set query for fuzzy search, ids for exact "
        "lookup, or traverse_from with direction and depth for graph traversal.",
        started_label="Reading notes",
        finished_label="Read notes",
        symbol="◉",
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
        "Create one or more independently meaningful notes atomically.",
        started_label="Adding notes",
        finished_label="Added notes",
        symbol="+",
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
        symbol="✎",
    )
    def edit_note(self, note_id: str, text: str) -> str:
        """Edit a project note.

        Returns:
            A summary of the edited note.
        """
        note = self.notes.edit(note_id, text)
        return f"Edited {note.id}: {note.text}"

    @tool(
        "Delete notes and every connection touching them atomically.",
        started_label="Deleting notes",
        finished_label="Deleted notes",
        symbol="-",
    )
    def delete_notes(self, note_ids: list[str]) -> str:
        """Delete project notes.

        Returns:
            A summary of the deleted notes.
        """
        deleted_ids = self.notes.delete(note_ids)
        return f"Deleted notes: {', '.join(deleted_ids)}."

    @tool(
        "Create directed, labeled connections between notes atomically.",
        started_label="Connecting notes",
        finished_label="Connected notes",
        symbol="↗",
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
        started_label="Disconnecting notes",
        finished_label="Disconnected notes",
        symbol="↛",
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
