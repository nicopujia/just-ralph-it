import json
import logging
from collections.abc import Mapping
from math import ceil
from pathlib import Path
from threading import Event
from typing import Any, cast

import httpx
import pytest

from jri.core.ai import ToolCallFinished, ToolCallStarted, functional_analyst
from jri.core.ai.specs_writer import SpecsWriter
from jri.core.paths import SPECS_DIR
from jri.core.specs import Specs
from jri.lib import git
from jri.lib.context import estimate_tokens, measure_request
from tests.conftest import CreateRepository
from tests.doubles.agents import drain
from tests.doubles.models_dot_dev import serve_catalog, serve_outcome
from tests.doubles.openai import FakeClient, Round, call, response
from tests.doubles.settings import build_settings
from tests.doubles.specs import install_specifications

CONTEXT = functional_analyst.Input(notebook="Report the totals.")
NOTHING_LEFT = functional_analyst.Specifications(deleted_paths=[], unresolved=[])
# One body of this size weighs 10000 tokens. Six of them together go above the mark that the room below sets.
# The room that the first bodies make brings the request under the lower mark before the last bodies go.
BODY = "Behavior. " * 3_000
# A body of blanks names a file, but it gives none of the behavior of that file. JRI refuses the write.
BLANK_BODY = " " * len(BODY)
PATHS = [f"functional/part{number}.md" for number in range(6)]
# Three bodies make enough room. The pass stops there, and the last three bodies stay whole in the request.
COMPACTED_BODIES = 3
# This catalog publishes a room that puts the marks at 42000 and 31500 tokens. The pass above goes over the
# first mark, and it then comes back under the second mark.
ROOMY_CATALOG: dict[str, Any] = {"test": {"limit": {"context": 60_000, "input": 52_500, "output": 7_500}}}
# No pass of these tests can fill this room. The history keeps each call as the model made it.
SPACIOUS_CATALOG: dict[str, Any] = {"test": {"limit": {"input": 10_000_000}}}
# A room this small limits one read to 100 tokens. Two files of a few hundred bytes are already above it.
NARROW_CATALOG: dict[str, Any] = {"test": {"limit": {"context": 2_000, "input": 1_000, "output": 1_000}}}
# JRI puts this text in the place of a body. It says that the project holds the full file, and it says how to
# read the file back. The model then never reads it as a shorter file than the file that it wrote.
WRITTEN_FILE_RECORD = (
    "[This body was taken out of the message to make room. The project holds the file as you wrote it, in full. "
    "Call `read_functional_specs` with `{path}` to read it back.]"
)


def build_analyst(client: FakeClient, repository_path: Path) -> functional_analyst.FunctionalAnalyst:
    return functional_analyst.FunctionalAnalyst(
        build_settings(client), git.Repository(repository_path), changed=False, existing=True, feedback=False
    )


def write_call(number: int, files: Mapping[str, str]) -> Round:
    written = [{"path": path, "content": content, "summary": f"Part {number}."} for path, content in files.items()]
    return response(call(f"write-{number}", "write_specs", files=written))


def read_call(number: int, paths: list[str]) -> Round:
    return response(call(f"read-{number}", "read_functional_specs", paths=paths))


def write(analyst: functional_analyst.FunctionalAnalyst) -> "functional_analyst.Specifications | None":
    return drain(analyst.write(CONTEXT, Event()))[1]


# This pass answers with one write call for each file, in a room that no request of it can fill. The history
# keeps its calls exactly as the model made them.
def build_pass(repository_path: Path, bodies: Mapping[str, str]) -> functional_analyst.FunctionalAnalyst:
    calls = [write_call(number, {path: body}) for number, (path, body) in enumerate(bodies.items())]
    analyst = build_analyst(FakeClient([], parsed=[*calls, NOTHING_LEFT]), repository_path)
    write(analyst)
    return analyst


def read_items(client: FakeClient, kind: str) -> list[dict[str, Any]]:
    return [item for item in cast("list[dict[str, Any]]", client.responses.inputs[-1]) if item.get("type") == kind]


def read_outputs(client: FakeClient) -> list[str]:
    return [str(item["output"]) for item in read_items(client, "function_call_output")]


# Read the files of every write call in the request, oldest first.
def read_written_files(writer: SpecsWriter) -> list[dict[str, str]]:
    return [
        file
        for item in cast("list[dict[str, Any]]", writer.history)
        if item.get("name") == "write_specs"
        for file in cast("list[dict[str, str]]", json.loads(str(item["arguments"]))["files"])
    ]


def read_logs(caplog: pytest.LogCaptureFixture, event: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith(event)]


# Weigh the request of one round as JRI does. That request holds the context of the round and the tools that
# JRI offers with the context.
def measure_context(writer: SpecsWriter) -> int:
    return estimate_tokens(measure_request(writer.history, [item.definition for item in writer.tools]))


# Publish the room whose mark is exactly the given estimate. A mark is a share of the room, so round the room
# up. A room below the quotient puts the mark one token under the estimate.
def serve_room(monkeypatch: pytest.MonkeyPatch, estimate: int, share: float = SpecsWriter.INPUT_SHARE) -> int:
    room = ceil(estimate / share)
    serve_catalog(monkeypatch, {"test": {"limit": {"input": room}}})
    return room


# One answer does not have to hold all the files. A pass makes as many calls as it needs, and each file reaches
# the project when its call arrives.
def test_writes_every_file_of_a_pass_that_wrote_them_in_several_calls(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    client = FakeClient(
        [],
        parsed=[
            write_call(0, {"functional/behavior.md": "# Behavior\n"}),
            write_call(1, {"functional/exports.md": "# Exports\n", "functional/limits.md": "# Limits\n"}),
            NOTHING_LEFT,
        ],
    )
    analyst = build_analyst(client, tmp_path)

    assert write(analyst) == NOTHING_LEFT

    assert analyst.written_paths == {"functional/behavior.md", "functional/exports.md", "functional/limits.md"}
    assert read_outputs(client) == [
        "Wrote functional/behavior.md.",
        "Wrote functional/exports.md, functional/limits.md.",
    ]
    assert Specs.read(repository, ".jri/specs/functional") == {
        ".jri/specs/functional/behavior.md": b"---\nsummary: Part 0.\n---\n\n# Behavior\n",
        ".jri/specs/functional/exports.md": b"---\nsummary: Part 1.\n---\n\n# Exports\n",
        ".jri/specs/functional/limits.md": b"---\nsummary: Part 1.\n---\n\n# Limits\n",
    }


# A write call takes minutes. This row is what the user sees while the call runs.
def test_names_the_row_of_a_write_call(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[write_call(0, {PATHS[0]: "# Behavior\n"}), NOTHING_LEFT])

    rows = list(build_analyst(client, tmp_path).write(CONTEXT, Event()))

    assert rows == [
        ToolCallStarted("write-0", "Writing specification files", "✍️"),
        ToolCallFinished("write-0", "Wrote specification files", "done"),
    ]


# A rewind replays the calls that it keeps. A replayed write puts back a file that the rewind removed, so JRI
# never replays a write call.
def test_never_replays_a_write_call(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    analyst = build_analyst(FakeClient([]), tmp_path)
    write_specs = next(item for item in analyst.tools if item.name == "write_specs")

    write_specs.replay(json.dumps({"files": [{"path": PATHS[0], "content": BODY, "summary": "Part 0."}]}))

    assert not (tmp_path / SPECS_DIR / PATHS[0]).exists()


# These descriptions are all that the model reads about the two tools that a pass calls.
def test_offers_the_model_tools_that_write_and_read_specification_files(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)

    analyst = build_analyst(FakeClient([]), tmp_path)

    assert {item.name: item.definition.get("description") for item in analyst.tools} == {
        "write_specs": "Write specification files, each with its complete final content and a one-line summary.",
        "read_functional_specs": (
            "Read the full, current body of existing functional specification files, named as the index shows them."
        ),
    }


# A read call names the files that it asks the project for. The user sees which files a pass reads back.
def test_names_the_row_of_a_read_call(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    install_specifications(tmp_path, {PATHS[0]: "# Behavior\n"})
    client = FakeClient([], parsed=[read_call(0, [PATHS[0]]), NOTHING_LEFT])

    rows = list(build_analyst(client, tmp_path).write(CONTEXT, Event()))

    assert rows == [
        ToolCallStarted("read-0", f"Reading ['{PATHS[0]}']", "📖"),
        ToolCallFinished("read-0", f"Read ['{PATHS[0]}']", "done"),
    ]


# A rewind replays the calls that it keeps, against the project that stays after the rewind removes its turns.
# A file that a kept read names can be absent then. JRI never replays a read call.
def test_never_replays_a_read_call(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    analyst = build_analyst(FakeClient([]), tmp_path)
    read_specs = next(item for item in analyst.tools if item.name == "read_functional_specs")

    read_specs.replay(json.dumps({"paths": ["functional/gone.md"]}))


# Above the mark, the oldest bodies leave the request, and a record of the file takes the place of each one. The
# pass keeps the newest bodies. A body that left never comes back. The last request of the pass still holds
# the record in the place of the first body.
def test_takes_the_oldest_bodies_out_of_a_pass_that_passes_the_mark(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, ROOMY_CATALOG)
    client = FakeClient(
        [], parsed=[*(write_call(number, {path: BODY}) for number, path in enumerate(PATHS)), NOTHING_LEFT]
    )
    analyst = build_analyst(client, tmp_path)

    assert write(analyst) == NOTHING_LEFT

    assert [file["content"] for file in read_written_files(analyst)] == [
        *(WRITTEN_FILE_RECORD.format(path=path) for path in PATHS[:COMPACTED_BODIES]),
        *([BODY] * (len(PATHS) - COMPACTED_BODIES)),
    ]
    assert (tmp_path / SPECS_DIR / PATHS[0]).read_text().endswith(BODY)


# The mark says when a request is too heavy. A request of exactly that weight is not yet too heavy.
def test_keeps_every_body_when_the_request_weighs_the_mark(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, SPACIOUS_CATALOG)
    analyst = build_pass(tmp_path, dict.fromkeys(PATHS, BODY))
    serve_room(monkeypatch, measure_context(analyst))

    analyst.get_context()

    assert [file["content"] for file in read_written_files(analyst)] == [BODY] * len(PATHS)


# JRI takes no more bodies out when the request comes under the lower mark. A request of exactly that weight is
# under the mark. The bodies of the newer calls stay whole in the request that goes out.
def test_keeps_the_newest_bodies_when_the_request_weighs_the_lower_mark(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, SPACIOUS_CATALOG)
    records = [WRITTEN_FILE_RECORD.format(path=path) for path in PATHS]
    kept = [*records[:2], *([BODY] * (len(PATHS) - 2))]
    # A compaction must leave this request, and the lower mark is exactly its weight.
    settled = build_pass(tmp_path, dict(zip(PATHS, kept, strict=True)))
    analyst = build_pass(tmp_path, dict.fromkeys(PATHS, BODY))
    serve_room(monkeypatch, measure_context(settled), SpecsWriter.LOW_SHARE)

    analyst.get_context()

    assert [file["content"] for file in read_written_files(analyst)] == kept


# A compaction takes bodies out of a request that the user never sees. Only the log tells what happened:
# the weight of the request, and the two marks that the compaction works between. A mark is a share of the room
# of the model.
def test_logs_the_marks_a_compaction_works_between(
    tmp_path: Path,
    create_repository: CreateRepository,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, SPACIOUS_CATALOG)
    analyst = build_pass(tmp_path, {})
    weight = measure_context(analyst)
    room = serve_room(monkeypatch, weight - 1)

    with caplog.at_level(logging.INFO, logger="jri"):
        analyst.get_context()

    low = int(room * SpecsWriter.LOW_SHARE)
    assert read_logs(caplog, "specs_compaction_started") == [
        f"specs_compaction_started tokens={weight} high={weight - 1} low={low}"
    ]


# Only the log names the body that left the request, and the weight of the request after it left.
def test_logs_each_body_a_compaction_took_out(
    tmp_path: Path,
    create_repository: CreateRepository,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, SPACIOUS_CATALOG)
    analyst = build_pass(tmp_path, {PATHS[0]: BODY})
    serve_room(monkeypatch, measure_context(analyst) - 1)

    with caplog.at_level(logging.INFO, logger="jri"):
        analyst.get_context()

    assert read_logs(caplog, "specs_body_compacted") == [
        f"specs_body_compacted path={PATHS[0]} tokens={measure_context(analyst)}"
    ]


# A catalog that JRI cannot reach publishes no room, and the pass must still write. The pass then uses a room of
# its own, which is large enough to keep a written body in its place.
def test_keeps_a_written_body_when_the_catalog_publishes_no_room(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_outcome(monkeypatch, httpx.ConnectError("connection refused"))
    analyst = build_analyst(FakeClient([], parsed=[write_call(0, {PATHS[0]: BODY}), NOTHING_LEFT]), tmp_path)

    write(analyst)

    assert read_written_files(analyst)[0]["content"] == BODY


# The history keeps a call whose arguments answer to no schema as the model sent it. A compaction does not change
# such a call, and it takes only the bodies of the calls that wrote a file.
def test_compacts_a_pass_that_also_made_a_call_that_answers_to_no_schema(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, ROOMY_CATALOG)
    malformed = response(call("write-9", "write_specs", files=[{"path": PATHS[0], "content": BODY}]))
    client = FakeClient(
        [], parsed=[malformed, *(write_call(number, {path: BODY}) for number, path in enumerate(PATHS)), NOTHING_LEFT]
    )
    analyst = build_analyst(client, tmp_path)

    assert write(analyst) == NOTHING_LEFT

    written = read_written_files(analyst)
    assert written[0]["content"] == BODY
    assert written[1]["content"] == WRITTEN_FILE_RECORD.format(path=PATHS[0])
    assert written[-1]["content"] == BODY


# A call that JRI refused wrote no file, whatever its arguments answered to. The record says that the project
# holds the full file, so the record must never replace the body of such a call.
def test_compacts_a_pass_that_also_made_a_call_jri_refused(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, ROOMY_CATALOG)
    refused = write_call(9, {"functional/stub.md": BLANK_BODY})
    client = FakeClient(
        [], parsed=[refused, *(write_call(number, {path: BODY}) for number, path in enumerate(PATHS)), NOTHING_LEFT]
    )
    analyst = build_analyst(client, tmp_path)

    assert write(analyst) == NOTHING_LEFT

    written = read_written_files(analyst)
    assert written[0]["content"] == BLANK_BODY
    assert written[1]["content"] == WRITTEN_FILE_RECORD.format(path=PATHS[0])
    assert not (tmp_path / SPECS_DIR / "functional/stub.md").exists()


# A compacted call is still a call that the model made. It must answer to the schema of the tool that it called.
# The record replaces the body and changes nothing else in the call.
def test_leaves_a_compacted_call_in_the_shape_the_tool_takes(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, ROOMY_CATALOG)
    client = FakeClient(
        [], parsed=[*(write_call(number, {path: BODY}) for number, path in enumerate(PATHS)), NOTHING_LEFT]
    )
    analyst = build_analyst(client, tmp_path)

    write(analyst)

    calls = [item for item in read_items(client, "function_call") if item["name"] == "write_specs"]
    assert [sorted(json.loads(str(item["arguments"]))) for item in calls] == [["files"] for _ in calls]
    assert all(sorted(file) == ["content", "path", "summary"] for file in read_written_files(analyst))


# A specification that JRI cuts reads like a complete one, and the model designs against the part that arrived.
# The refusal gives the weight of each file, and the next call can then ask for fewer files.
def test_refuses_a_batch_of_reads_over_the_cap_instead_of_cutting_it(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, NARROW_CATALOG)
    install_specifications(tmp_path, {"functional/behavior.md": "# Behavior\n" * 40, "functional/exports.md": "E\n"})
    client = FakeClient([], parsed=[read_call(0, ["functional/behavior.md", "functional/exports.md"]), NOTHING_LEFT])

    write(build_analyst(client, tmp_path))

    refusal = read_outputs(client)[0]
    assert "over the 100 tokens one call answers with" in refusal
    assert "functional/behavior.md (147), functional/exports.md (1)" in refusal
    assert "# Behavior" not in refusal
