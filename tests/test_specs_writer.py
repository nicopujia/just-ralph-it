import json
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

from jri.core.ai import ReasoningDelta, functional_analyst
from jri.core.specs import Specs
from jri.lib import git
from tests.conftest import CreateRepository
from tests.doubles.models_dot_dev import serve_catalog
from tests.doubles.openai import FakeClient, Round, call, response
from tests.doubles.settings import build_settings

CONTEXT = functional_analyst.Input(notebook="Report the totals.")
NOTHING_LEFT = functional_analyst.Specifications(deleted_paths=[], unresolved=[])
# Six bodies of this size weigh about 60000 tokens together, and the room below leaves the request about 42000.
# A pass thus crosses the mark, and what it frees stops it before the last bodies go.
BODY = "Behavior. " * 3_000
PATHS = [f"functional/part{number}.md" for number in range(6)]
# The room this catalog publishes, and the mark JRI takes from it, are what make the pass above cross it.
ROOMY_CATALOG: dict[str, Any] = {"test": {"limit": {"context": 60_000, "input": 52_500, "output": 7_500}}}
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


def write_call(number: int, files: dict[str, str]) -> Round:
    written = [{"path": path, "content": content, "summary": f"Part {number}."} for path, content in files.items()]
    return response(call(f"write-{number}", "write_functional_specs", files=written))


def read_call(number: int, paths: list[str]) -> Round:
    return response(call(f"read-{number}", "read_functional_specs", paths=paths))


def read_items(client: FakeClient, kind: str) -> list[dict[str, Any]]:
    return [item for item in cast("list[dict[str, Any]]", client.responses.inputs[-1]) if item.get("type") == kind]


def read_outputs(client: FakeClient) -> list[str]:
    return [str(item["output"]) for item in read_items(client, "function_call_output")]


def read_written_files(client: FakeClient) -> list[dict[str, str]]:
    return [
        file
        for item in read_items(client, "function_call")
        if item["name"] == "write_functional_specs"
        for file in cast("list[dict[str, str]]", json.loads(str(item["arguments"]))["files"])
    ]


def write(analyst: functional_analyst.FunctionalAnalyst) -> "functional_analyst.Specifications | None":
    events = analyst.write(CONTEXT, Event())
    while True:
        try:
            event = next(events)
            assert isinstance(event, ReasoningDelta) or event.call_id
        except StopIteration as stop:
            return cast("functional_analyst.Specifications | None", stop.value)


def install_specifications(repository_path: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        specification = repository_path / ".jri" / "specs" / path
        specification.parent.mkdir(parents=True, exist_ok=True)
        specification.write_text(content, encoding="utf-8", newline="")


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
    assert Specs.read(repository, ".jri/specs/functional") == {
        ".jri/specs/functional/behavior.md": b"---\nsummary: Part 0.\n---\n\n# Behavior\n",
        ".jri/specs/functional/exports.md": b"---\nsummary: Part 1.\n---\n\n# Exports\n",
        ".jri/specs/functional/limits.md": b"---\nsummary: Part 1.\n---\n\n# Limits\n",
    }


# A file with a summary and no body is a stub for work that no later pass comes back to do. The model hears about
# it while it can still write the file, and nothing lands under that path.
@pytest.mark.parametrize("content", ["", "   \n\n"], ids=["empty", "blank"])
def test_refuses_a_file_that_carries_no_behavior(
    content: str, tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[write_call(0, {"functional/behavior.md": content}), NOTHING_LEFT])
    analyst = build_analyst(client, tmp_path)

    write(analyst)

    assert analyst.written_paths == set()
    assert any("carries none" in output for output in read_outputs(client))
    assert not (tmp_path / ".jri/specs/functional/behavior.md").exists()


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

    assert write(build_analyst(client, tmp_path)) == NOTHING_LEFT

    written = read_written_files(client)
    assert written[0]["content"] == WRITTEN_FILE_RECORD.format(path=PATHS[0])
    assert written[-1]["content"] == BODY
    assert (tmp_path / ".jri/specs" / PATHS[0]).read_text().endswith(BODY)


# A call that JRI refused stands in the history with the arguments the model sent, which answer to no schema.
# Compaction leaves those alone and takes the bodies of the calls that did write.
def test_compacts_a_pass_that_also_made_a_call_jri_refused(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    serve_catalog(monkeypatch, ROOMY_CATALOG)
    malformed = response(call("write-9", "write_functional_specs", files=[{"path": PATHS[0], "content": BODY}]))
    client = FakeClient(
        [], parsed=[malformed, *(write_call(number, {path: BODY}) for number, path in enumerate(PATHS)), NOTHING_LEFT]
    )

    assert write(build_analyst(client, tmp_path)) == NOTHING_LEFT

    written = read_written_files(client)
    assert written[0]["content"] == BODY
    assert written[1]["content"] == WRITTEN_FILE_RECORD.format(path=PATHS[0])
    assert written[-1]["content"] == BODY


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

    write(build_analyst(client, tmp_path))

    calls = [item for item in read_items(client, "function_call") if item["name"] == "write_functional_specs"]
    assert [sorted(json.loads(str(item["arguments"]))) for item in calls] == [["files"] for _ in calls]
    assert all(sorted(file) == ["content", "path", "summary"] for file in read_written_files(client))


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
