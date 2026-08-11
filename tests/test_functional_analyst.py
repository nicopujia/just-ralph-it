from threading import Event
from typing import cast

from jri.core.ai import ReasoningDelta, functional_analyst
from tests.doubles.openai import FakeClient, reply, response, thought
from tests.doubles.settings import build_settings

CONTEXT = functional_analyst.Input(
    notebook="Deploy from the main branch.", notebook_diff="+Deploy from the main branch.", current_specs="(empty)"
)
SPECIFICATIONS = functional_analyst.Specifications(
    outcome="specifications",
    files=[functional_analyst.File(path="functional/behavior.md", content="# Behavior\n")],
    deleted_paths=[],
)


def build_analyst(client: FakeClient) -> functional_analyst.FunctionalAnalyst:
    return functional_analyst.FunctionalAnalyst(build_settings(client))


def read_request(client: FakeClient) -> str:
    return str(cast("list[dict[str, object]]", client.responses.inputs[-1])[-1]["content"])


def write(
    analyst: functional_analyst.FunctionalAnalyst, context: functional_analyst.Input
) -> tuple[list[ReasoningDelta], "functional_analyst.Result | None"]:
    call = analyst.write(context, Event())
    thoughts: list[ReasoningDelta] = []
    while True:
        try:
            thoughts.append(next(call))
        except StopIteration as stop:
            return thoughts, cast("functional_analyst.Result | None", stop.value)


def test_writes_the_specification_files() -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=SPECIFICATIONS)])

    assert write(build_analyst(client), CONTEXT)[1] == SPECIFICATIONS


# Check the behavior in `test_streams_the_thinking_of_a_call_before_the_specifications_it_wrote`.
# Check the behavior in `test_streams_the_thinking_of_a_call_before_the_specifications_it_wrote`.
def test_streams_the_thinking_of_a_call_before_the_specifications_it_wrote() -> None:
    client = FakeClient(
        [],
        parsed=[
            [
                thought("Weighing the totals."),
                *response(reply(functional_analyst.Output(result=SPECIFICATIONS).model_dump_json())),
            ]
        ],
    )

    thoughts, result = write(build_analyst(client), CONTEXT)

    assert thoughts == [ReasoningDelta("Weighing the totals.")]
    assert result == SPECIFICATIONS


def test_reports_ambiguities_instead_of_specifications() -> None:
    ambiguities = functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=["JSON or plain text?"])
    client = FakeClient([], parsed=[functional_analyst.Output(result=ambiguities)])

    assert write(build_analyst(client), CONTEXT)[1] == ambiguities


def test_asks_for_a_first_draft_from_the_notebook_alone() -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=SPECIFICATIONS)])

    write(build_analyst(client), CONTEXT)

    request = read_request(client)
    assert "Current notebook:\n```\nDeploy from the main branch.\n```" in request
    assert "Architect feedback:" not in request


# Check the behavior in `test_revises_the_specifications_as_they_stand_against_the_architect_feedback`.
# Check the behavior in `test_revises_the_specifications_as_they_stand_against_the_architect_feedback`.
# Check the behavior in `test_revises_the_specifications_as_they_stand_against_the_architect_feedback`.
def test_revises_the_specifications_as_they_stand_against_the_architect_feedback() -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=SPECIFICATIONS)])
    context = CONTEXT.model_copy(
        update={"current_specs": "File: functional/behavior.md", "architect_feedback": ["Undefined totals."]}
    )

    write(build_analyst(client), context)

    request = read_request(client)
    assert "Current functional specifications:\n```\nFile: functional/behavior.md\n```" in request
    assert "Architect feedback:\n  - Undefined totals." in request
    assert "Rejected functional draft:" not in request
    assert "Accepted functional specifications:" not in request


# Check the behavior in `test_gates_every_escalation_on_what_the_notebook_delegated`.
# Check the behavior in `test_gates_every_escalation_on_what_the_notebook_delegated`.
# Check the behavior in `test_gates_every_escalation_on_what_the_notebook_delegated`.
# Check the behavior in `test_gates_every_escalation_on_what_the_notebook_delegated`.
def test_gates_every_escalation_on_what_the_notebook_delegated() -> None:
    prompt = build_analyst(FakeClient([])).runner.prompt.replace("\n      ", " ")

    escalations = [rule for rule in prompt.split("\n    - ") if "ambiguities" in rule]

    assert escalations
    assert [rule for rule in escalations if "the notebook has not delegated" not in rule] == []
