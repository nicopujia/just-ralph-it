import json
import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast, override

from openai.types.responses import ResponseInputParam
from pydantic import ValidationError

from jri.core.paths import FUNCTIONAL_SPECS_ROOT
from jri.core.specs import File, Specs
from jri.lib import git
from jri.lib.context import estimate_tokens, measure_item, measure_request
from jri.lib.models_dot_dev import get_input_room

from .agent import Agent
from .tool import Invocation, tool

logger = logging.getLogger(__name__)


# An agent that writes specification files into a repository, one call at a time. Each call is a response of its
# own, so the pass sets the limit on the files that it writes, and one answer does not.
@dataclass
class SpecsWriter(Agent):
    # Compact after the request uses this share of the input room. The rest of that room holds the answer that the
    # request asks for, and the reasoning that the model keeps while it writes.
    INPUT_SHARE: ClassVar[float] = 0.8
    # Compact back to this share, and not to just below the share above. The room between the two shares holds many
    # more calls. One compaction helps the rounds that follow it, and not only the round that started it.
    LOW_SHARE: ClassVar[float] = 0.6
    # A pass measures its request against this room when the catalog gives no limit for the model.
    FALLBACK_INPUT_ROOM: ClassVar[int] = 100_000
    # One batched read answers with at most this share of that room. The text that a read adds stays for all the
    # pass. A written body does not stay, because compaction can remove it.
    READ_SHARE: ClassVar[float] = 0.1
    # This text replaces a written body. It says where the file is and how to read it. The model reads its own
    # call back, and it must never read this text as the file that it wrote.
    WRITTEN_FILE_RECORD: ClassVar[str] = (
        "[This body was taken out of the message to make room. The project holds the file as you wrote it, "
        "in full. Call `{tool}` with `{path}` to read it back.]"
    )

    repository: git.Repository
    specs_root: str
    read_tool: str

    written_paths: set[str] = field(default_factory=set)

    # Compact before each round, because this is the context that the round about to start sends.
    @override
    def get_context(self) -> ResponseInputParam:
        self._compact()
        return self.history

    # Put each file on disk as it arrives. The pass then reads back what the project holds, and not a copy of it
    # that this conversation keeps.
    @tool(
        "Write specification files, each with its complete final content and a one-line summary.",
        started_label="Writing specification files",
        finished_label="Wrote specification files",
        symbol="✍️",
        replayed=False,
    )
    def write_specs(self, files: list[File]) -> str:
        Specs.write(self.repository, {file.path: Specs.format(file) for file in files}, (), self.specs_root)
        self.written_paths.update(file.path for file in files)
        return f"Wrote {', '.join(sorted(file.path for file in files))}."

    @tool(
        "Read the full, current body of existing functional specification files, named as the index shows them.",
        started_label="Reading {paths}",
        finished_label="Read {paths}",
        symbol="📖",
        replayed=False,
    )
    def read_functional_specs(self, paths: list[str]) -> str:
        return self.read_specs(paths, FUNCTIONAL_SPECS_ROOT)

    def read_specs(self, paths: list[str], model_root: str) -> str:
        room = get_input_room(self.profile.model, self.FALLBACK_INPUT_ROOM)
        # The tool loop cuts an output that is longer than its own limit, and a cut specification reads like a
        # complete one. Refuse below that limit, because JRI must refuse before the loop cuts.
        cap = min(int(room * self.READ_SHARE), estimate_tokens(Invocation.MAX_OUTPUT_LENGTH))
        return Specs.read_selected(self.repository, model_root, paths, cap)

    # A written body stays in the request in full while the request fits. When the request is too large, take the
    # oldest bodies out, oldest first, until the request is below the lower share. A body never comes back in a
    # pass, so every later request repeats the bytes of this one, which the provider serves from its cache.
    def _compact(self) -> None:
        tools = self.get_tools()
        size = measure_request(self.history, [item.definition for item in tools])
        room = get_input_room(self.profile.model, self.FALLBACK_INPUT_ROOM)
        high = int(room * self.INPUT_SHARE)
        if estimate_tokens(size) <= high:
            return
        write = next(item for item in tools if item.name == self.write_specs.__name__)
        low = int(room * self.LOW_SHARE)
        logger.info("specs_compaction_started tokens=%d high=%d low=%d", estimate_tokens(size), high, low)
        for raw_item in self.history:
            item = cast("dict[str, Any]", raw_item)
            if item.get("name") != write.name:
                continue
            try:
                arguments = write.arguments_model.model_validate_json(cast("str", item["arguments"]))
            # A call that answers to no schema wrote nothing. Leave the arguments the model sent.
            except ValidationError:
                continue
            payload = arguments.model_dump()
            weight = measure_item(item)
            for file in cast("list[dict[str, str]]", payload["files"]):
                # A call that JRI refused wrote no file either, whatever its arguments held. The record below
                # states that the project holds the file, so only a path the project took can carry it.
                if file["path"] not in self.written_paths:
                    continue
                record = self.WRITTEN_FILE_RECORD.format(tool=self.read_tool, path=file["path"])
                if file["content"] == record:
                    continue
                file["content"] = record
                item["arguments"] = json.dumps(payload)
                updated = measure_item(item)
                size -= weight - updated
                weight = updated
                logger.info("specs_body_compacted path=%s tokens=%d", file["path"], estimate_tokens(size))
                if estimate_tokens(size) <= low:
                    return
