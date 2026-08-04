from pathlib import Path
from typing import cast

from jri.core.ai import functional_analyst
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings

CONTEXT = functional_analyst.Input(
    notebook="Deploy from the main branch.", notebook_diff="+Deploy from the main branch.", accepted_specs="(empty)"
)
PATCH = functional_analyst.Patch(outcome="specification_patch", patch="diff --git a/functional/x.md b/x.md\n")


def build_analyst(path: Path, client: FakeClient) -> functional_analyst.FunctionalAnalyst:
    return functional_analyst.FunctionalAnalyst(build_settings(path, client))


def read_request(client: FakeClient) -> str:
    return str(cast("list[dict[str, object]]", client.responses.inputs[-1])[-1]["content"])


def test_writes_a_specification_patch(tmp_path: Path) -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=PATCH)])

    assert build_analyst(tmp_path, client).write(CONTEXT) == PATCH


def test_reports_ambiguities_instead_of_a_patch(tmp_path: Path) -> None:
    ambiguities = functional_analyst.Ambiguities(outcome="ambiguities", ambiguities=["JSON or plain text?"])
    client = FakeClient([], parsed=[functional_analyst.Output(result=ambiguities)])

    assert build_analyst(tmp_path, client).write(CONTEXT) == ambiguities


def test_asks_for_a_first_draft_from_the_notebook_alone(tmp_path: Path) -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=PATCH)])

    build_analyst(tmp_path, client).write(CONTEXT)

    request = read_request(client)
    assert "Current notebook:\nDeploy from the main branch." in request
    assert "Rejected functional draft:" not in request


def test_revises_against_the_rejected_draft_and_the_architect_feedback(tmp_path: Path) -> None:
    client = FakeClient([], parsed=[functional_analyst.Output(result=PATCH)])
    context = CONTEXT.model_copy(
        update={"rejected_specs": "File: functional/behavior.md", "architect_feedback": "- Undefined totals."}
    )

    build_analyst(tmp_path, client).write(context)

    request = read_request(client)
    assert "Rejected functional draft:\nFile: functional/behavior.md" in request
    assert "Architect feedback:\n- Undefined totals." in request
