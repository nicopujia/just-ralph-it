from collections.abc import Generator
from typing import Any, cast, override

from openai.types.responses import ResponseInputItemParam, ResponseInputParam

from jri.core.notes import Connection, Note, Notebook, ReadQuery
from jri.core.settings import Settings
from jri.lib.models import estimate_tokens, get_context_limit

from .explorer import Explorer
from .shared import Agent, TextDelta, ToolCallFinished, ToolCallStarted, ToolOutput, tool


class Interviewer(Agent):
    """Agent that interviews the user to extract a project idea."""

    CONTEXT_THRESHOLD = 0.4
    FIRST_MESSAGE = "What do you want to build?"
    INITIAL_TOPIC_NAME = "Project overview"
    TOPIC_SUFFIXES = (": open topic", ": done", ": trashed")

    def __init__(self, settings: Settings, notebook: Notebook) -> None:
        self.settings = settings
        self.notebook = notebook
        self.explorer: Explorer
        initial_topic_note = next(
            (
                note
                for note in self.notebook.graph.notes
                if self._extract_topic_name(note.text).casefold() == self.INITIAL_TOPIC_NAME.casefold()
            ),
            None,
        )
        self.initial_topic = initial_topic_note or self.notebook.add([f"{self.INITIAL_TOPIC_NAME}: open topic"])[0]
        super().__init__(
            client=settings.llm_client,
            model=settings.interviewer_model,
            temperature=settings.interviewer_temperature,
            reasoning_effort=settings.interviewer_reasoning_effort,
            sys_prompt="""
                Role: Interviewer of the Just Ralph It (JRI) system, a software system to build any software system.

                Goals:
                    1. Help the user realize what they _actually_ want and need.
                    2. Extract the user's project idea out of their mind into distilled, interconnected notes.
                    3. Maintain awareness of every project topic and ensure none is left unexplored.

                Success criteria is one of the following:
                    - The notes describe a project such that if a competent engineer built the project based solely on
                    the those notes, there would not be more than one plausible interpretation regarding behavior,
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
                    - If the user is not sure about a decision, state alternatives and their trade-offs, not opinions.
                    - Don't make assumptions.

                Tools:
                    - Manage project knowledge and open questions with the note tools every time the user shares new
                    information about the project, no matter how little or much — assume you may forget any relevant
                    fact unless you take notes of it.
                    - Switch to the relevant topic before capturing notes that belong to it.
                    - Capture unresolved unknowns as notes before switching away from a topic.
                    - When you and the user agree a topic is complete, edit its topic note accordingly.
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

    @override
    def get_context(self) -> ResponseInputParam:
        """Return the topic-aware context sent to the model.

        Returns:
            The topic-aware conversation context.

        """

        history = self.history
        topics = [
            note for note in self.notebook.graph.notes if self._is_topic(note) and not note.text.endswith(": trashed")
        ]
        active = self._get_active_topic()
        lines = [f"- {topic.id}: {topic.text}{' (active)' if topic.id == active.id else ''}" for topic in topics]
        pinned_topics = [self.initial_topic]
        if active.id != self.initial_topic.id:
            pinned_topics.append(active)
        for topic in pinned_topics:
            note_ids = {
                connection.target_id
                for connection in self.notebook.graph.connections
                if connection.source_id == topic.id and connection.label == "contains"
            }
            notes = [note for note in self.notebook.graph.notes if note.id in note_ids]
            if notes:
                lines.extend(["", f"{self._extract_topic_name(topic.text)} notes"])
                lines.extend(f"- {note.id}: {note.text}" for note in notes)
        pinned: ResponseInputItemParam = {"role": "system", "content": "Topic index:\n" + "\n".join(lines)}
        turns: list[ResponseInputParam] = []
        for raw_item in history[1:]:
            item = cast("dict[str, Any]", raw_item)
            if ("role" in item and item["role"] == "user") or not turns:
                turns.append([])
            turns[-1].append(raw_item)
        tools = [tool.definition for tool in self.tools]
        context: ResponseInputParam = [history[0], pinned, *(item for turn in turns for item in turn)]
        budget = get_context_limit(self.model) * self.CONTEXT_THRESHOLD
        while len(turns) > 1 and estimate_tokens(context, tools) > budget:
            turns.pop(0)
            context = [history[0], pinned, *(item for turn in turns for item in turn)]
        return context

    @tool(
        (
            "Gather context through a telegraphic query, including anything from the web or this computer."
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
        "Turn to a project topic by its name or existing topic note ID.",
        started_label="Switching to {topic}",
        finished_label="Switched to {topic}",
        symbol="📑",
    )
    def switch_topic(self, topic: str) -> str:
        """Switch to a project topic.

        Returns:
            The resolved topic ID and text.

        Raises:
            ValueError: If the topic is blank, invalid, or trashed.
        """

        value = topic.strip()
        if not value:
            raise ValueError("Topic name cannot be blank.")
        by_id = {note.id: note for note in self.notebook.graph.notes}
        if value in by_id:
            resolved = by_id[value]
            if not self._is_topic(resolved):
                raise ValueError(f"Note `{value}` is not a topic.")
        else:
            resolved = next(
                (
                    item
                    for item in self.notebook.graph.notes
                    if self._is_topic(item) and self._extract_topic_name(item.text).casefold() == value.casefold()
                ),
                None,
            )
            if resolved is None:
                resolved = self.notebook.add([f"{value}: open topic"])[0]
        if resolved.text.endswith(": trashed"):
            raise ValueError(f"Topic `{resolved.id}` is trashed. Edit it to restore it before switching.")
        return f"Switched to {resolved.id}: {resolved.text}"

    @tool(
        (
            "Read all notes when called without a query. Set `query.text` for fuzzy search, `query.ids` for exact "
            "lookup, or `query.traverse_from` with `direction` and `depth` for graph traversal."
        ),
        started_label="Reading notes",
        finished_label="Read notes",
        symbol="📖",
        strict=False,
    )
    def read_notes(self, query: ReadQuery | None = None) -> str:
        """Read relevant project notes.

        Returns:
            Matching notes and connections.
        """
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
        """Capture project notes under the active topic.

        Returns:
            A summary containing the new IDs.
        """
        notes = self.notebook.add(texts)
        topic = self._get_active_topic()
        self.notebook.connect([Connection(source_id=topic.id, target_id=note.id, label="contains") for note in notes])
        return "\n".join(f"Added {note.id}: {note.text}" for note in notes)

    @tool(
        "Edit one note's text without changing its connections.",
        started_label="Editing note",
        finished_label="Edited note",
        symbol="✏️",
    )
    def edit_note(self, note_id: str, text: str) -> str:
        """Edit a project note.

        Returns:
            A summary of the edited note.

        Raises:
            ValueError: If a topic would lose its status or the initial
                topic would be trashed.
        """
        note = next((note for note in self.notebook.graph.notes if note.id == note_id), None)
        if note is not None and self._is_topic(note) and not text.endswith(self.TOPIC_SUFFIXES):
            raise ValueError(f"Topic `{note_id}` must remain open, done, or trashed.")
        if note_id == self.initial_topic.id and text.endswith(": trashed"):
            raise ValueError(f"The initial topic `{self.initial_topic.id}` cannot be trashed.")
        note = self.notebook.edit(note_id, text)
        return f"Edited {note.id}: {note.text}"

    @tool(
        "Trash topic notes while preserving them, or delete regular notes and every connection touching them.",
        started_label="Discarding notes",
        finished_label="Discarded notes",
        symbol="🗑️",
    )
    def delete_notes(self, note_ids: list[str]) -> str:
        """Trash topic notes or delete regular notes.

        Returns:
            A summary of the deleted notes.

        Raises:
            ValueError: If the IDs are invalid or include the initial
                topic.
        """
        if not note_ids or len(note_ids) != len(set(note_ids)):
            raise ValueError("Provide one or more unique note IDs.")
        graph = self.notebook.graph.model_copy(deep=True)
        by_id = {note.id: note for note in graph.notes}
        unknown = set(note_ids) - by_id.keys()
        if unknown:
            raise ValueError(f"Unknown note `{min(unknown)}`.")
        if self.initial_topic.id in note_ids:
            raise ValueError(f"The initial topic `{self.initial_topic.id}` cannot be trashed.")

        trashed_ids = [note_id for note_id in note_ids if self._is_topic(by_id[note_id])]
        deleted_ids = [note_id for note_id in note_ids if note_id not in trashed_ids]
        for note_id in trashed_ids:
            note = by_id[note_id]
            note.text = f"{self._extract_topic_name(note.text)}: trashed"
        deleted = set(deleted_ids)
        graph.notes = [note for note in graph.notes if note.id not in deleted]
        graph.connections = [
            item for item in graph.connections if item.source_id not in deleted and item.target_id not in deleted
        ]
        self.notebook.restore(graph)

        summaries: list[str] = []
        if trashed_ids:
            summaries.append(f"Trashed topics: {', '.join(trashed_ids)}.")
        if deleted_ids:
            summaries.append(f"Deleted notes: {', '.join(deleted_ids)}.")
        return "\n".join(summaries)

    @tool(
        "Create directed, labeled connections between notes.",
        started_label="Organizing notes",
        finished_label="Organized notes",
        symbol="🖇️",
    )
    def connect_notes(self, connections: list[Connection]) -> str:
        """Connect project notes.

        Returns:
            The number of relationships created.
        """
        count = self.notebook.connect(connections)
        return f"Connected {count} relationship(s)."

    @tool(
        "Remove directed, labeled connections between notes.",
        started_label="Reorganizing notes",
        finished_label="Reorganized notes",
        symbol="📎",
    )
    def disconnect_notes(self, connections: list[Connection]) -> str:
        """Disconnect project notes.

        Returns:
            The number of relationships removed.
        """
        count = self.notebook.disconnect(connections)
        return f"Disconnected {count} relationship(s)."

    def _get_active_topic(self) -> Note:
        outputs: dict[str, str] = {}
        for raw_item in self.history:
            item = cast("dict[str, Any]", raw_item)
            if item.get("type") == "function_call_output" and isinstance(item["output"], str):
                outputs[item["call_id"]] = item["output"]
        by_id = {note.id: note for note in self.notebook.graph.notes}
        for raw_item in reversed(self.history):
            item = cast("dict[str, Any]", raw_item)
            if item.get("type") == "function_call" and item["name"] == "switch_topic" and item["call_id"] in outputs:
                topic_id = outputs[item["call_id"]].partition(":")[0].removeprefix("Switched to ")
                if topic := by_id.get(topic_id):
                    return self.initial_topic if topic.text.endswith(": trashed") else topic
        return self.initial_topic

    @classmethod
    def _is_topic(cls, note: Note) -> bool:
        return note.text.endswith(cls.TOPIC_SUFFIXES)

    @classmethod
    def _extract_topic_name(cls, text: str) -> str:
        return text.rsplit(":", 1)[0] if text.endswith(cls.TOPIC_SUFFIXES) else text
