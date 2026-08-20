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
# Six bodies of this size weigh about 60000 tokens together, and the room below leaves the request about 42000.
# A pass thus crosses the mark, and what it frees stops it before the last bodies go.
BODY = "Behavior. " * 3_000
# A body of blanks names a file and carries none of the behavior it names, so the write is refused.
BLANK_BODY = " " * len(BODY)
PATHS = [f"functional/part{number}.md" for number in range(6)]
# Four bodies free enough room, so the pass stops there and the last two stay in the request whole.
COMPACTED_BODIES = 4
# The room this catalog publishes, and the mark JRI takes from it, are what make the pass above cross it.
ROOMY_CATALOG: dict[str, Any] = {"test": {"limit": {"context": 60_000, "input": 52_500, "output": 7_500}}}
# A room no pass of these tests can fill, for a pass that must stand in the history as the model made it.
SPACIOUS_CATALOG: dict[str, Any] = {"test": {"limit": {"input": 10_000_000}}}
# A room this small caps one read at 100 tokens, which two files of a few hundred bytes already pass.
NARROW_CATALOG: dict[str, Any] = {"test": {"limit": {"context": 2_000, "input": 1_000, "output": 1_000}}}
# What JRI leaves where a body stood. It says the project holds the file in full, and how to read it back, so
# the model can never read it as a shorter file than the one it wrote.
WRITTEN_FILE_RECORD = (
    "[JRI took this body out of the message to make room. The project holds the file as you wrote it, in full. "
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


# A pass that answered with one write call per file, under a room no request of it can fill. Its calls stand in
# the history exactly as the model made them.
def build_pass(repository_path: Path, bodies: Mapping[str, str]) -> functional_analyst.FunctionalAnalyst:
    calls = [write_call(number, {path: body}) for number, (path, body) in enumerate(bodies.items())]
    analyst = build_analyst(FakeClient([], parsed=[*calls, NOTHING_LEFT]), repository_path)
    write(analyst)
    return analyst


def read_items(client: FakeClient, kind: str) -> list[dict[str, Any]]:
    return [item for item in cast("list[dict[str, Any]]", client.responses.inputs[-1]) if item.get("type") == kind]


def read_outputs(client: FakeClient) -> list[str]:
    return [str(item["output"]) for item in read_items(client, "function_call_output")]


# The files of every write call the request carries, oldest first.
def read_written_files(writer: SpecsWriter) -> list[dict[str, str]]:
    return [
        file
        for item in cast("list[dict[str, Any]]", writer.history)
        if item.get("name") == "write_specs"
        for file in cast("list[dict[str, str]]", json.loads(str(item["arguments"]))["files"])
    ]


def read_logs(caplog: pytest.LogCaptureFixture, event: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith(event)]


# What JRI weighs the request of one round at: the context of that round, and the tools it offers with it.
def measure_context(writer: SpecsWriter) -> int:
    return estimate_tokens(measure_request(writer.history, [item.definition for item in writer.tools]))


# Publish the room whose mark the given estimate stands exactly on. The mark is a share of the room, so round the
# room up: a room short of the quotient would put the mark one token under the estimate.
def serve_room(monkeypatch: pytest.MonkeyPatch, estimate: int) -> None:
    serve_catalog(monkeypatch, {"test": {"limit": {"input": ceil(estimate / SpecsWriter.INPUT_SHARE)}}})


# One answer no longer has to hold the whole set: a pass writes as many calls as it needs, and each file reaches
# the project as its call arrives.
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


# A write call takes minutes, and this row is what the user sees of it while it runs.
def test_names_the_row_of_a_write_call(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[write_call(0, {PATHS[0]: "# Behavior\n"}), NOTHING_LEFT])

    rows = list(build_analyst(client, tmp_path).write(CONTEXT, Event()))

    assert rows == [
        ToolCallStarted("write-0", "Writing specification files", "✍️"),
        ToolCallFinished("write-0", "Wrote specification files", "done"),
    ]


# A rewind replays the calls it keeps. A replayed write would put back a file that the rewind took away, so this
# call is never replayed.
def test_never_replays_a_write_call(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    analyst = build_analyst(FakeClient([]), tmp_path)
    write_specs = next(item for item in analyst.tools if item.name == "write_specs")

    write_specs.replay(json.dumps({"files": [{"path": PATHS[0], "content": BODY, "summary": "Part 0."}]}))

    assert not (tmp_path / SPECS_DIR / PATHS[0]).exists()


# This definition is the whole account the model gets of the write tool. Without the rule that a call is final
# for the files it names, a pass leaves a file half written for a later call that never comes.
def test_offers_the_model_one_tool_that_writes_specification_files(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    analyst = build_analyst(FakeClient([]), tmp_path)

    write_specs = next(item for item in analyst.tools if item.name == "write_specs")

    assert write_specs.definition == {
        "type": "function",
        "name": "write_specs",
        "description": (
            "Write specification files, each with its complete final content and a one-line summary. "
            "Call this as many times as the set needs, and keep each call small enough to write well. "
            "A call is final for the files it names: no later step fills a file in, and a file left out of every "
            "call keeps the content it already has."
        ),
        "parameters": {
            "$defs": {
                "File": {
                    "additionalProperties": False,
                    "properties": {
                        "path": {"title": "Path", "type": "string"},
                        "content": {"title": "Content", "type": "string"},
                        "summary": {"title": "Summary", "type": "string"},
                    },
                    "required": ["path", "content", "summary"],
                    "title": "File",
                    "type": "object",
                }
            },
            "additionalProperties": False,
            "properties": {"files": {"items": {"$ref": "#/$defs/File"}, "title": "Files", "type": "array"}},
            "required": ["files"],
            "title": "Write_SpecsArguments",
            "type": "object",
        },
        "strict": True,
    }


# A read call names the files it asked the project for, so the user sees which ones a pass read back.
def test_names_the_row_of_a_read_call(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    install_specifications(tmp_path, {PATHS[0]: "# Behavior\n"})
    client = FakeClient([], parsed=[read_call(0, [PATHS[0]]), NOTHING_LEFT])

    rows = list(build_analyst(client, tmp_path).write(CONTEXT, Event()))

    assert rows == [
        ToolCallStarted("read-0", f"Reading ['{PATHS[0]}']", "📖"),
        ToolCallFinished("read-0", f"Read ['{PATHS[0]}']", "done"),
    ]


# A rewind replays the calls it keeps, against the project as it stands once the rewind has taken its turns away.
# A file that a kept read named can be gone by then, so a read is never replayed.
def test_never_replays_a_read_call(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    analyst = build_analyst(FakeClient([]), tmp_path)
    read_specs = next(item for item in analyst.tools if item.name == "read_functional_specs")

    read_specs.replay(json.dumps({"paths": ["functional/gone.md"]}))


# A pass writes in calls of its own, and how many files one of them carried stands nowhere but the log.
def test_logs_the_files_one_write_call_put_in_the_project(
    tmp_path: Path, create_repository: CreateRepository, caplog: pytest.LogCaptureFixture
) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[write_call(0, {PATHS[0]: BODY, PATHS[1]: BODY}), NOTHING_LEFT])

    with caplog.at_level(logging.INFO, logger="jri"):
        write(build_analyst(client, tmp_path))

    assert read_logs(caplog, "specs_call_written") == ["specs_call_written root=functional files=2"]


# Past the mark, the oldest bodies leave the request and a record of the file takes their place. The pass keeps
# the newest bodies, and a body that left never returns, so the request that ends the pass still carries the
# record where the first body stood.
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


# The mark says when a request is too heavy, and a request of exactly that weight is not yet too heavy.
def test_keeps_every_body_when_the_request_weighs_the_mark(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, SPACIOUS_CATALOG)
    analyst = build_pass(tmp_path, dict.fromkeys(PATHS, BODY))
    serve_room(monkeypatch, measure_context(analyst))

    analyst.get_context()

    assert [file["content"] for file in read_written_files(analyst)] == [BODY] * len(PATHS)


# JRI stops taking bodies out as soon as the request is under the lower mark, and a request of exactly that
# weight is under it. The bodies of the newer calls thus stand whole in the request that goes out.
def test_keeps_the_newest_bodies_when_the_request_weighs_the_lower_mark(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, SPACIOUS_CATALOG)
    records = [WRITTEN_FILE_RECORD.format(path=path) for path in PATHS]
    kept = [*records[:2], *([BODY] * (len(PATHS) - 2))]
    # The request a compaction must leave, and the largest file one more call could add to it.
    settled = build_pass(tmp_path, dict(zip(PATHS, kept, strict=True)))
    analyst = build_pass(tmp_path, dict.fromkeys(PATHS, BODY))
    largest = (tmp_path / SPECS_DIR / PATHS[0]).stat().st_size
    serve_room(monkeypatch, measure_context(settled) + estimate_tokens(largest))

    analyst.get_context()

    assert [file["content"] for file in read_written_files(analyst)] == kept


# A compaction takes bodies out of a request the user never sees, so the log is the only account of it: the
# weight the request stood at, and the marks it worked between. The lower mark leaves room for one more file as
# large as the largest the project holds, and a project that holds none leaves the two marks together.
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
    serve_room(monkeypatch, weight - 1)

    with caplog.at_level(logging.INFO, logger="jri"):
        analyst.get_context()

    assert read_logs(caplog, "specs_compaction_started") == [
        f"specs_compaction_started tokens={weight} high={weight - 1} low={weight - 1}"
    ]


# Which body left the request, and what the request weighed once it had, stand nowhere but the log.
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


# A catalog JRI cannot reach publishes no room, and the pass must still write. It works against a room of its own,
# which is wide enough to leave a written body where the model put it.
def test_keeps_a_written_body_when_the_catalog_publishes_no_room(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_outcome(monkeypatch, httpx.ConnectError("connection refused"))
    analyst = build_analyst(FakeClient([], parsed=[write_call(0, {PATHS[0]: BODY}), NOTHING_LEFT]), tmp_path)

    write(analyst)

    assert read_written_files(analyst)[0]["content"] == BODY


# A call whose arguments answer to no schema stands in the history as the model sent it. Compaction leaves those
# alone and takes the bodies of the calls that did write.
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


# A call JRI refused wrote no file, whatever its arguments answered to. The record states that the project holds
# the file in full, so it must never stand where the body of such a call stood.
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


# A compacted call is still a call the model made. It must read back against the schema of the tool it called,
# so the record replaces the body and changes nothing else about the call.
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


# A cut specification reads like a complete one, and the model would design against the part that arrived. The
# refusal names what each file weighs, so the next call can ask for fewer of them.
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


# No smaller request exists for one file, so a call that names one answers with it whatever it weighs.
def test_reads_one_specification_the_cap_alone_would_refuse(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, NARROW_CATALOG)
    install_specifications(tmp_path, {"functional/behavior.md": "# Behavior\n" * 40})
    client = FakeClient([], parsed=[read_call(0, ["functional/behavior.md"]), NOTHING_LEFT])

    write(build_analyst(client, tmp_path))

    assert read_outputs(client) == [
        f"<file>\nfunctional/behavior.md\n</file>\n\n<content>\n{'# Behavior\n' * 40}\n</content>"
    ]
