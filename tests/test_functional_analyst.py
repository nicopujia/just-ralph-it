from pathlib import Path
from threading import Event
from typing import cast

from jri.core.ai import ReasoningDelta, functional_analyst
from jri.core.specs import Specs
from jri.lib import git
from tests.conftest import CreateRepository
from tests.doubles.openai import FakeClient, call, reply, response, thought
from tests.doubles.settings import build_settings

# This is a first pass: the project holds no accepted baseline and no specifications,
# so it receives no diff, no index, and no feedback.
CONTEXT = functional_analyst.Input(notebook="Deploy from the main branch.")
SPECIFICATIONS = functional_analyst.Specifications(
    files=[
        functional_analyst.File(
            path="functional/behavior.md", content="# Behavior\n", summary="How the product behaves."
        )
    ],
    deleted_paths=[],
    unresolved=[],
)


def build_analyst(
    client: FakeClient, repository_path: Path, *, changed: bool = False, existing: bool = False, feedback: bool = False
) -> functional_analyst.FunctionalAnalyst:
    return functional_analyst.FunctionalAnalyst(
        build_settings(client), git.Repository(repository_path), changed=changed, existing=existing, feedback=feedback
    )


def read_request(client: FakeClient) -> str:
    return str(cast("list[dict[str, object]]", client.responses.inputs[-1])[-1]["content"])


def read_instructions(client: FakeClient) -> str:
    return str(cast("list[dict[str, object]]", client.responses.inputs[-1])[0]["content"])


def write(
    analyst: functional_analyst.FunctionalAnalyst, context: functional_analyst.Input
) -> tuple[list[ReasoningDelta], "functional_analyst.Specifications | None"]:
    call = analyst.write(context, Event())
    thoughts: list[ReasoningDelta] = []
    while True:
        try:
            event = next(call)
            if isinstance(event, ReasoningDelta):
                thoughts.append(event)
        except StopIteration as stop:
            return thoughts, cast("functional_analyst.Specifications | None", stop.value)


def test_writes_the_specification_files(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[SPECIFICATIONS])

    assert write(build_analyst(client, tmp_path), CONTEXT)[1] == SPECIFICATIONS


# Each set of rules speaks about input. A first pass has no notebook diff, no specification index, and no round to
# answer, so every set would describe what the pass never receives.
def test_keeps_the_diff_index_and_feedback_rules_out_of_a_first_pass(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[SPECIFICATIONS])

    write(build_analyst(client, tmp_path), CONTEXT)

    instructions = read_instructions(client)
    assert functional_analyst.FunctionalAnalyst.DIFF_PROMPT not in instructions
    assert functional_analyst.FunctionalAnalyst.EXISTING_PROMPT not in instructions
    assert functional_analyst.FunctionalAnalyst.FEEDBACK_PROMPT not in instructions


def test_sends_the_index_rules_with_the_specifications_they_speak_about(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[SPECIFICATIONS])

    write(
        build_analyst(client, tmp_path, existing=True),
        CONTEXT.model_copy(update={"current_specs_index": "functional/behavior.md: How the product behaves."}),
    )

    instructions = read_instructions(client)
    assert functional_analyst.FunctionalAnalyst.EXISTING_PROMPT in instructions
    assert functional_analyst.FunctionalAnalyst.FEEDBACK_PROMPT not in instructions


def test_sends_the_diff_rules_with_the_diff_they_speak_about(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[SPECIFICATIONS])

    write(
        build_analyst(client, tmp_path, changed=True),
        CONTEXT.model_copy(update={"notebook_diff": "+Deploy from the main branch."}),
    )

    assert functional_analyst.FunctionalAnalyst.DIFF_PROMPT in read_instructions(client)
    assert "<notebook_diff_from_accepted_baseline>\n+Deploy from the main branch." in read_request(client)


def test_sends_the_feedback_rules_with_the_feedback_they_speak_about(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[SPECIFICATIONS])

    write(
        build_analyst(client, tmp_path, feedback=True),
        CONTEXT.model_copy(update={"architect_feedback": ["Unclear export."]}),
    )

    assert functional_analyst.FunctionalAnalyst.FEEDBACK_PROMPT in read_instructions(client)


def test_streams_the_thinking_of_a_call_before_the_specifications_it_wrote(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    client = FakeClient(
        [], parsed=[[thought("Weighing the totals."), *response(reply(SPECIFICATIONS.model_dump_json()))]]
    )

    thoughts, result = write(build_analyst(client, tmp_path), CONTEXT)

    assert thoughts == [ReasoningDelta("Weighing the totals.")]
    assert result == SPECIFICATIONS


# The files and the questions travel together, so a pass reports both without choosing between them.
def test_reports_the_questions_beside_the_specifications_it_wrote(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    written = SPECIFICATIONS.model_copy(update={"unresolved": ["JSON or plain text?"]})
    client = FakeClient([], parsed=[written])

    assert write(build_analyst(client, tmp_path), CONTEXT)[1] == written


def test_asks_for_a_first_draft_from_the_notebook_alone(tmp_path: Path, create_repository: CreateRepository) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[SPECIFICATIONS])

    write(build_analyst(client, tmp_path), CONTEXT)

    request = read_request(client)
    assert "<current_notebook>\nDeploy from the main branch.\n</current_notebook>" in request
    assert "<notebook_diff_from_accepted_baseline>" not in request
    assert "<current_functional_specifications_index>" not in request
    assert "<architect_feedback>" not in request


def test_revises_the_specifications_as_they_stand_against_the_architect_feedback(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path)
    client = FakeClient([], parsed=[SPECIFICATIONS])
    context = CONTEXT.model_copy(
        update={
            "current_specs_index": "functional/behavior.md: How the product behaves.",
            "architect_feedback": ["Undefined totals."],
        }
    )

    write(build_analyst(client, tmp_path, existing=True, feedback=True), context)

    request = read_request(client)
    assert "<current_functional_specifications_index>\nfunctional/behavior.md: How the product behaves." in request
    assert "<architect_feedback>\n  - Undefined totals." in request


# The index carries only a summary. A pass that judges a file relevant reads its full body with the tool instead
# of guessing it from the summary alone.
def test_reads_the_full_body_of_a_specification_it_judges_relevant(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    specification = tmp_path / ".jri" / "specs" / "functional" / "behavior.md"
    specification.parent.mkdir(parents=True)
    specification.write_text(Specs.format(SPECIFICATIONS.files[0]), encoding="utf-8")
    client = FakeClient(
        [], parsed=[response(call("read", "read_functional_specs", paths=["functional/behavior.md"])), SPECIFICATIONS]
    )

    analyst = functional_analyst.FunctionalAnalyst(
        build_settings(client), repository, changed=False, existing=True, feedback=False
    )

    result = write(analyst, CONTEXT)[1]

    assert result == SPECIFICATIONS
    assert "# Behavior" in str(client.responses.inputs[-1])
