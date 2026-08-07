from threading import Event
from typing import cast

from jri.core.ai import functional_analyst
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings

CONTEXT = functional_analyst.Input(
    notebook="Deploy from the main branch.", notebook_diff="+Deploy from the main branch.", accepted_specs="(empty)"
)
PATCH = functional_analyst.Patch(outcome="specification_patch", patch="diff --git a/functional/x.md b/x.md\n")


def build_analyst(client: FakeClient) -> functional_analyst.FunctionalAnalyst:
    return functional_analyst.FunctionalAnalyst(build_settings(client))


def read_request(client: FakeClient) -> str:
    return str(cast("list[dict[str, object]]", client.responses.inputs[-1])[-1]["content"])


def test_writes_a_specification_patch() -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=PATCH)])

    assert build_analyst(client).write(CONTEXT, Event()) == PATCH


def test_reports_ambiguities_instead_of_a_patch() -> None:
    ambiguities = functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=["JSON or plain text?"])
    client = FakeClient([], parsed=[functional_analyst.Output(result=ambiguities)])

    assert build_analyst(client).write(CONTEXT, Event()) == ambiguities


def test_asks_for_a_first_draft_from_the_notebook_alone() -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=PATCH)])

    build_analyst(client).write(CONTEXT, Event())

    request = read_request(client)
    assert "Current notebook:\n```\nDeploy from the main branch.\n```" in request
    assert "Rejected functional draft:" not in request


def test_revises_against_the_rejected_draft_and_the_architect_feedback() -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=PATCH)])
    context = CONTEXT.model_copy(
        update={"rejected_specs": "File: functional/behavior.md", "architect_feedback": ["Undefined totals."]}
    )

    build_analyst(client).write(context, Event())

    request = read_request(client)
    assert "Rejected functional draft:\n```\nFile: functional/behavior.md\n```" in request
    assert "Architect feedback:\n  - Undefined totals." in request


# A model copies the tail of the rule it followed into the
# specification it writes, so every rule that can return `ambiguities`
# states the delegation gate itself: a qualifier standing elsewhere in
# the prompt does not travel with the copy.
def test_gates_every_escalation_on_what_the_notebook_delegated() -> None:
    prompt = build_analyst(FakeClient([])).runner.prompt.replace("\n      ", " ")

    escalations = [rule for rule in prompt.split("\n    - ") if "ambiguities" in rule]

    assert escalations
    assert [rule for rule in escalations if "the notebook has not delegated" not in rule] == []
