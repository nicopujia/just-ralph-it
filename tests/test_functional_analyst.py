from threading import Event
from typing import cast

from jri.core.ai import ReasoningDelta, functional_analyst
from tests.doubles.openai import FakeClient, reply, response, thought
from tests.doubles.settings import build_settings

# This is a first pass: the project holds no specifications, so it receives no tree and no feedback.
CONTEXT = functional_analyst.Input(
    notebook="Deploy from the main branch.", notebook_diff="+Deploy from the main branch."
)
SPECIFICATIONS = functional_analyst.Specifications(
    files=[functional_analyst.File(path="functional/behavior.md", content="# Behavior\n")],
    deleted_paths=[],
    unresolved=[],
)


def build_analyst(client: FakeClient) -> functional_analyst.FunctionalAnalyst:
    return functional_analyst.FunctionalAnalyst(build_settings(client))


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
            thoughts.append(next(call))
        except StopIteration as stop:
            return thoughts, cast("functional_analyst.Specifications | None", stop.value)


def test_writes_the_specification_files() -> None:
    client = FakeClient([], parsed=[SPECIFICATIONS])

    assert write(build_analyst(client), CONTEXT)[1] == SPECIFICATIONS


# Each set of rules speaks about input. A first pass has neither a specification tree nor a round to answer,
# so both sets would describe what the pass never receives.
def test_keeps_the_tree_and_feedback_rules_out_of_a_first_pass() -> None:
    client = FakeClient([], parsed=[SPECIFICATIONS])

    write(build_analyst(client), CONTEXT)

    instructions = read_instructions(client)
    assert functional_analyst.FunctionalAnalyst.EXISTING_PROMPT not in instructions
    assert functional_analyst.FunctionalAnalyst.FEEDBACK_PROMPT not in instructions


def test_sends_the_tree_rules_with_the_specifications_they_speak_about() -> None:
    client = FakeClient([], parsed=[SPECIFICATIONS])

    write(build_analyst(client), CONTEXT.model_copy(update={"current_specs": "File: functional/behavior.md"}))

    instructions = read_instructions(client)
    assert functional_analyst.FunctionalAnalyst.EXISTING_PROMPT in instructions
    assert functional_analyst.FunctionalAnalyst.FEEDBACK_PROMPT not in instructions


def test_sends_the_feedback_rules_with_the_feedback_they_speak_about() -> None:
    client = FakeClient([], parsed=[SPECIFICATIONS])

    write(build_analyst(client), CONTEXT.model_copy(update={"architect_feedback": ["Unclear export."]}))

    assert functional_analyst.FunctionalAnalyst.FEEDBACK_PROMPT in read_instructions(client)


def test_streams_the_thinking_of_a_call_before_the_specifications_it_wrote() -> None:
    client = FakeClient(
        [], parsed=[[thought("Weighing the totals."), *response(reply(SPECIFICATIONS.model_dump_json()))]]
    )

    thoughts, result = write(build_analyst(client), CONTEXT)

    assert thoughts == [ReasoningDelta("Weighing the totals.")]
    assert result == SPECIFICATIONS


# The files and the questions travel together, so a pass reports both without choosing between them.
def test_reports_the_questions_beside_the_specifications_it_wrote() -> None:
    written = SPECIFICATIONS.model_copy(update={"unresolved": ["JSON or plain text?"]})
    client = FakeClient([], parsed=[written])

    assert write(build_analyst(client), CONTEXT)[1] == written


def test_asks_for_a_first_draft_from_the_notebook_alone() -> None:
    client = FakeClient([], parsed=[SPECIFICATIONS])

    write(build_analyst(client), CONTEXT)

    request = read_request(client)
    assert "<current_notebook>\nDeploy from the main branch.\n</current_notebook>" in request
    assert "<current_functional_specifications>" not in request
    assert "<architect_feedback>" not in request


def test_revises_the_specifications_as_they_stand_against_the_architect_feedback() -> None:
    client = FakeClient([], parsed=[SPECIFICATIONS])
    context = CONTEXT.model_copy(
        update={"current_specs": "File: functional/behavior.md", "architect_feedback": ["Undefined totals."]}
    )

    write(build_analyst(client), context)

    request = read_request(client)
    assert (
        "<current_functional_specifications>\nFile: functional/behavior.md\n</current_functional_specifications>"
        in request
    )
    assert "<architect_feedback>\n  - Undefined totals." in request
    assert "<rejected_functional_draft>" not in request
    assert "<accepted_functional_specifications>" not in request
