import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast, override

from openai.types.responses import ResponseInputParam
from pydantic import BaseModel, ConfigDict, ValidationError

from jri.core.ai.agent import Agent
from jri.core.ai.tool import Invocation
from jri.core.paths import SPECS_DIR
from jri.core.specs import File, Specs
from jri.lib import git
from jri.lib.context import estimate_tokens, measure_item, measure_request
from jri.lib.models_dot_dev import get_input_room

logger = logging.getLogger(__name__)


# The arguments of one write call. A compacted call carries this same shape, so the provider still reads it
# against the schema of the tool that the model called.
class WrittenSpecs(BaseModel):
    files: list[File]

    model_config = ConfigDict(extra="forbid")


# An agent that writes specification files into a repository, one call at a time. Each call is a response of its
# own, so the set a pass writes is bounded by the pass, and not by what one answer can hold.
@dataclass(kw_only=True)
class SpecsWriter(Agent):
    # Compact past this share of the room the model reads. The rest of that room holds the answer that the
    # request asks for, and the reasoning the model keeps while it writes.
    INPUT_SHARE: ClassVar[float] = 0.8
    # One batched read answers with at most this share of that room. What a read brings in stays for the whole
    # pass, unlike a written body, which compaction can take back out.
    READ_SHARE: ClassVar[float] = 0.1
    FALLBACK_INPUT_ROOM: ClassVar[int] = 100_000
    # This stands where a written body stood. It says where the file is and how to read it, because the model
    # reads its own call back and must never read this as the file it wrote.
    WRITTEN_FILE_RECORD: ClassVar[str] = (
        "[JRI took this body out of the message to make room. The project holds the file as you wrote it, "
        "in full. Call `{tool}` with `{path}` to read it back.]"
    )

    repository: git.Repository
    specs_root: str
    write_tool: str
    read_tool: str

    written_paths: set[str] = field(init=False, default_factory=set)

    # Compact before each round, because this is the context that the round about to start sends.
    @override
    def get_context(self) -> ResponseInputParam:
        self._compact()
        return self.history

    # Put each file on disk as it arrives. The pass then reads back what the project holds, and not a copy of it
    # that this conversation keeps.
    def write_specs(self, files: list[File]) -> str:
        Specs.write(self.repository, {file.path: Specs.format(file) for file in files}, (), self.specs_root)
        self.written_paths.update(file.path for file in files)
        logger.info("specs_call_written root=%s files=%d", self.specs_root, len(files))
        return f"Wrote {', '.join(sorted(file.path for file in files))}."

    def read_specs(self, paths: list[str], model_root: str) -> str:
        return Specs.read_selected(self.repository, model_root, paths, self._read_cap())

    # A written body stays in the request in full while the request fits. Past the mark, take the oldest bodies
    # out, oldest first, until the request is under the lower mark. A body never comes back within a pass, so
    # every later request repeats the bytes of this one, which the provider serves from its cache.
    def _compact(self) -> None:
        tools = [item.definition for item in self.get_tools()]
        size = measure_request(self.history, tools)
        high = int(get_input_room(self.profile.model, self.FALLBACK_INPUT_ROOM) * self.INPUT_SHARE)
        if estimate_tokens(size) <= high:
            return
        # One more call can add a file as large as the largest one the project holds, so leave that much room
        # under the mark. A constant here would leave too little for a project of large files and too much for
        # a project of small ones.
        low = high - estimate_tokens(self._measure_largest_specification())
        logger.info("specs_compaction_started tokens=%d high=%d low=%d", estimate_tokens(size), high, low)
        for raw_item in self.history:
            item = cast("dict[str, Any]", raw_item)
            if item.get("name") != self.write_tool:
                continue
            try:
                written = WrittenSpecs.model_validate_json(cast("str", item["arguments"]), strict=True)
            # A call that JRI refused stands in the history with the arguments the model sent. Leave those.
            except ValidationError:
                continue
            weight = measure_item(item)
            for file in written.files:
                record = self._record_written_file(file.path)
                if file.content == record:
                    continue
                file.content = record
                item["arguments"] = written.model_dump_json()
                updated = measure_item(item)
                size -= weight - updated
                weight = updated
                logger.info("specs_body_compacted path=%s tokens=%d", file.path, estimate_tokens(size))
                if estimate_tokens(size) <= low:
                    return

    def _measure_largest_specification(self) -> int:
        return max((len(content) for content in Specs.read(self.repository, SPECS_DIR).values()), default=0)

    def _read_cap(self) -> int:
        room = get_input_room(self.profile.model, self.FALLBACK_INPUT_ROOM)
        # The tool loop cuts an output past its own limit, and a cut specification reads like a complete one.
        # Refuse under that limit, so the refusal happens before any cut can.
        return min(int(room * self.READ_SHARE), estimate_tokens(Invocation.MAX_OUTPUT_LENGTH))

    def _record_written_file(self, path: str) -> str:
        return self.WRITTEN_FILE_RECORD.format(tool=self.read_tool, path=path)
