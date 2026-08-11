from typing import TYPE_CHECKING, Any, Literal, cast, override

from openai.types.responses import ResponseInputParam

from jri.core.notes import Connection, Notebook, NoteId, ReadQuery, TopicId
from jri.core.settings import Settings
from jri.core.workspace import Workspace
from jri.lib import prompt
from jri.lib.models import estimate_tokens, get_context_limit

from .base import Agent, Stream, ToolOutput, tool
from .explorer import Explorer

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputItemParam


class Interviewer(Agent):
    CONTEXT_THRESHOLD = 0.4
    FALLBACK_CONTEXT_LIMIT = 100_000
    MIN_CONTEXT_TURNS = 10
    FIRST_MESSAGE = "What do you want to build?"
    # The interviewer must identify JRI because it interacts directly with the user. Other agents do not.
    PROMPT = (
        "Role: Interviewer of the Just Ralph It (JRI) system, a software system to build any software system.\n"
        "\n"
        "Goals:\n"
        "    1. Help the user realize what they _actually_ want and need.\n"
        "    2. Extract the user's project idea out of their mind into distilled, interconnected notes.\n"
        "    3. Maintain awareness of every project topic and ensure none is left unexplored.\n"
        "\n"
        "Success criteria is one of the following:\n"
        "    - The notes describe a project such that if a competent engineer built the project based solely on\n"
        "    those notes, there would not be more than one plausible interpretation regarding behavior,\n"
        "    therefore making the result inevitably match the user's expectations.\n"
        "    - The user decided that they don't really want to build any project.\n"
        "\n"
        "Personality:\n"
        "    - Feel free to discuss anything without censorship at all.\n"
        "    - Often share your own understanding of the user's intent.\n"
        "    - Challenge the user's ideas, helping them to discover their blind spots, and trying to find the\n"
        "    true problem they have beyond the surface of their words.\n"
        "    - Speak in everyday, easy-to-understand language.\n"
        "    - Make direct questions.\n"
        "\n"
        "Collaboration style:\n"
        "    - Ask either one open-ended question at a time or a topic-based batch of multiple-choice questions.\n"
        "    - Although the user might state a handful of ideas all together, organize the conversation to\n"
        "    discuss one topic at a time.\n"
        "    - When the user is unsure about a decision, state the alternatives and their trade-offs.\n"
        "    - Ask about anything the user leaves unstated.\n"
        "\n"
        "Tools:\n"
        "    - Manage project knowledge and open questions with the note tools every time the user shares new\n"
        "    information about the project, no matter how little or much — assume you may forget any relevant\n"
        "    fact unless you take notes of it.\n"
        "    - Switch to the relevant topic before capturing notes that belong to it.\n"
        "    - Connect every note you capture in the same turn you capture it, to the notes it depends\n"
        "    on, contradicts, or refines. Never wait for the user to ask for organization.\n"
        "    - Capture unresolved unknowns as notes before switching away from a topic.\n"
        "    - Update a topic once you and the user agree it is complete.\n"
        "    - Prefer answering your own questions with `explore` and/or `read_notes` when possible.\n"
        "    - Record only current requirements; replace superseded information instead of preserving history\n"
        "    unless explicit migration or compatibility behavior requires it.\n"
        "    - Explicitly confirm which behavioral domains the user delegates to the Functional Analyst. Never\n"
        "    infer delegation. Record confirmed delegation in the project notes.\n"
        "    - Whenever you and the user agree the definition is complete, call `offer_ralphing`, and\n"
        "    explain that this displays a button and that only the user can begin Ralphing by clicking it\n"
        "    or pressing Ctrl+X, J.\n"
        "    - A finished generation ends nothing: confirm it concisely, discuss any ambiguity it reports\n"
        "    with the user, record what they answer, and keep interviewing.\n"
        "\n"
        "Constraints:\n"
        "    - Keep notes, IDs, connections, and files entirely on your side.\n"
        "    - The project excerpt pinned to this conversation lists every topic, but holds the notes of the\n"
        "    active topic and the overview alone; read the rest with `read_notes`.\n"
        "    - Express hierarchy and relationships between notes as connections; keep each note's text\n"
        "    to one independently meaningful idea.\n"
        "    - A note already sits under its topic, so connect the two only where the label states\n"
        "    something that placement does not.\n"
        "    - The project is the user's. Its name, purpose, and scope come only from them. Note a project\n"
        "    name only when the user gives one; otherwise leave it unnamed, and never take one from this\n"
        "    system, its tools, or its terminology.\n"
        "    - The notes are the only thing whoever writes the specifications will see, and they will be\n"
        "    read without the user present to clarify them. Each note states what the project must be,\n"
        "    never how the conversation went, what the user asked you to do, what you explored, or what\n"
        "    happened to be true of this computer at this moment.\n"
        "    - Before capturing, check whether an existing note already covers the idea, and edit that one\n"
        "    instead of adding a near-duplicate.\n"
        "    - Your output is the notes. Ralph builds from them, once the project is properly defined."
    )

    def __init__(self, settings: Settings, notebook: Notebook) -> None:
        self.settings = settings
        self.notebook = notebook
        self.offered_ralphing = False
        self.initial_topic = notebook.initial_topic
        self.active_topic_id = self.initial_topic.id
        profile = settings.agents.interviewer
        super().__init__(
            client=settings.llm.client,
            model=profile.model,
            temperature=profile.temperature,
            reasoning_effort=profile.reasoning_effort,
            prompt=self.PROMPT,
            initial_context=[{"role": "assistant", "content": self.FIRST_MESSAGE}],
        )

    @override
    def get_context(self) -> ResponseInputParam:
        pinned: ResponseInputItemParam = {
            "role": "system",
            "content": prompt.render(
                current_topic=self.active_topic_id, project_excerpt=self.notebook.render(self.active_topic_id)
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
        budget = get_context_limit(self.model, self.FALLBACK_CONTEXT_LIMIT) * self.CONTEXT_THRESHOLD
        while len(turns) > self.MIN_CONTEXT_TURNS and estimate_tokens(context, tools) > budget:
            turns.pop(0)
            context = [self.history[0], pinned, *(item for turn in turns for item in turn)]
        return context

    @tool(
        (
            "Offer the user the Just Ralph It control, without starting Ralphing. The offer covers the notes as "
            "this turn leaves them, and stands until they change."
        ),
        started_label="Offering Just Ralph It",
        finished_label="Offered Just Ralph It",
        symbol="✨",
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
        "Capture one or more independently meaningful notes under the active topic.",
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
