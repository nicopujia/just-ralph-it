from collections.abc import Generator
from typing import Any, Literal, cast, override

from openai.types.responses import ResponseInputItemParam, ResponseInputParam

from jri.core.notes import Connection, Notebook, NoteId, ReadQuery, TopicId
from jri.core.settings import Settings
from jri.lib.models import estimate_tokens, get_context_limit

from .explorer import Explorer
from .shared import Agent, TextDelta, ToolCallFinished, ToolCallStarted, ToolOutput, tool


class Interviewer(Agent):
    """Agent that interviews the user to extract a project idea."""

    CONTEXT_THRESHOLD = 0.4
    MIN_CONTEXT_TURNS = 10
    FIRST_MESSAGE = "What do you want to build?"

    def __init__(self, settings: Settings, notebook: Notebook) -> None:
        self.settings = settings
        self.notebook = notebook
        self.explorer: Explorer
        self.initial_topic = next(topic for topic in self.notebook.graph.topics if topic.id == "t1")
        self.active_topic_id = self.initial_topic.id
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
                    - When you and the user agree a topic is complete, update its status and summary accordingly.
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
        topics = [topic for topic in self.notebook.graph.topics if topic.status != "trashed"]
        active = next(topic for topic in topics if topic.id == self.active_topic_id)
        lines = [
            f"- {topic.id}: {topic.name} ({topic.status})"
            f"{f'; {topic.summary}' if topic.summary else ''}"
            f"{' (current)' if topic.id == active.id else ''}"
            for topic in topics
        ]
        pinned_topics = [self.initial_topic]
        if active.id != self.initial_topic.id:
            pinned_topics.append(active)
        for topic in pinned_topics:
            notes = [note for note in self.notebook.graph.notes if note.topic_id == topic.id]
            if notes:
                lines.extend(["", f"{topic.name} notes"])
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
        while len(turns) > self.MIN_CONTEXT_TURNS and estimate_tokens(context, tools) > budget:
            turns.pop(0)
            context = [history[0], pinned, *(item for turn in turns for item in turn)]
        return context

    @tool(
        (
            "Gather context through a telegraphic query, including anything from the web or this computer. "
            "Queries can be as broad as needed, so unify all your inquiries in a single call. "
            'Format query so it reads well after "Exploring [...]" text'
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
        "Turn to a project topic by its name or ID, creating it when it does not exist.",
        started_label="Switching to {topic}",
        finished_label="Switched to {topic}",
        symbol="📑",
    )
    def switch_topic(self, topic: str) -> str:
        """Switch to a project topic.

        Returns:
            The resolved topic ID and name.

        Raises:
            ValueError: If the topic is blank, invalid, or trashed.
        """

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
        """Update a topic's status and optional summary.

        Returns:
            A summary of the updated topic.

        Raises:
            ValueError: If the overview topic would be trashed.
        """
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
        notes = self.notebook.add(texts, self.active_topic_id)
        return "\n".join(f"Added {note.id}: {note.text}" for note in notes)

    @tool(
        "Edit one note's text without changing its connections.",
        started_label="Editing note",
        finished_label="Edited note",
        symbol="✏️",
    )
    def edit_note(self, note_id: NoteId, text: str) -> str:
        """Edit a project note.

        Returns:
            A summary of the edited note.
        """
        note = self.notebook.edit(note_id, text)
        return f"Edited {note.id}: {note.text}"

    @tool(
        "Delete notes and every semantic connection touching them.",
        started_label="Discarding notes",
        finished_label="Discarded notes",
        symbol="🗑️",
    )
    def delete_notes(self, note_ids: list[NoteId]) -> str:
        """Delete project notes.

        Returns:
            A summary of the deleted notes.
        """
        return f"Deleted notes: {', '.join(self.notebook.delete(note_ids))}."

    @tool(
        "Create directed, labeled semantic connections between notes and/or topics.",
        started_label="Organizing notes",
        finished_label="Organized notes",
        symbol="🖇️",
    )
    def connect_notes(self, connections: list[Connection]) -> str:
        """Connect project notes and topics.

        Returns:
            The number of relationships created.
        """
        return f"Connected {self.notebook.connect(connections)} relationship(s)."

    @tool(
        "Remove directed, labeled semantic connections between notes and/or topics.",
        started_label="Reorganizing notes",
        finished_label="Reorganized notes",
        symbol="📎",
    )
    def disconnect_notes(self, connections: list[Connection]) -> str:
        """Disconnect project notes and topics.

        Returns:
            The number of relationships removed.
        """
        return f"Disconnected {self.notebook.disconnect(connections)} relationship(s)."
