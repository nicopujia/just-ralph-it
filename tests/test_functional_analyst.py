from threading import Event
from typing import cast

from jri.core.ai import functional_analyst
from tests.doubles.openai import FakeClient
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


def test_writes_the_specification_files() -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=SPECIFICATIONS)])

    assert build_analyst(client).write(CONTEXT, Event()) == SPECIFICATIONS


def test_reports_ambiguities_instead_of_specifications() -> None:
    ambiguities = functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=["JSON or plain text?"])
    client = FakeClient([], parsed=[functional_analyst.Output(result=ambiguities)])

    assert build_analyst(client).write(CONTEXT, Event()) == ambiguities


def test_asks_for_a_first_draft_from_the_notebook_alone() -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=SPECIFICATIONS)])

    build_analyst(client).write(CONTEXT, Event())

    request = read_request(client)
    assert "Current notebook:\n```\nDeploy from the main branch.\n```" in request
    assert "Architect feedback:" not in request


# The analyst is asked to change the specifications it is shown, so
# there is no second copy of them for a file it leaves out to fall back
# to, and no draft named as rejected when it is the one being kept.
def test_revises_the_specifications_as_they_stand_against_the_architect_feedback() -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=SPECIFICATIONS)])
    context = CONTEXT.model_copy(
        update={"current_specs": "File: functional/behavior.md", "architect_feedback": ["Undefined totals."]}
    )

    build_analyst(client).write(context, Event())

    request = read_request(client)
    assert "Current functional specifications:\n```\nFile: functional/behavior.md\n```" in request
    assert "Architect feedback:\n  - Undefined totals." in request
    assert "Rejected functional draft:" not in request
    assert "Accepted functional specifications:" not in request


# A model copies the tail of the rule it followed into the
# specification it writes, so every rule that can return `ambiguities`
# states the delegation gate itself: a qualifier standing elsewhere in
# the prompt does not travel with the copy.
def test_gates_every_escalation_on_what_the_notebook_delegated() -> None:
    prompt = build_analyst(FakeClient([])).runner.prompt.replace("\n      ", " ")

    escalations = [rule for rule in prompt.split("\n    - ") if "ambiguities" in rule]

    assert escalations
    assert [rule for rule in escalations if "the notebook has not delegated" not in rule] == []
