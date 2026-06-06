"""Tests for lean agent prompt context."""

from pathlib import Path

from jri.core.agents.prompts import (
    BASE_EXPLORER_PROMPT,
    BASE_INTERVIEWER_PROMPT,
    build_interviewer_context,
)


def test_interviewer_prompt_defines_jri_and_tool_roles() -> None:
    """The interviewer knows JRI's product role and narrow tool surface."""
    assert "Just Ralph It" in BASE_INTERVIEWER_PROMPT
    assert "intent extraction" in BASE_INTERVIEWER_PROMPT
    assert "Ralph" in BASE_INTERVIEWER_PROMPT
    assert "confirmed requirements" in BASE_INTERVIEWER_PROMPT
    assert "pending questions" in BASE_INTERVIEWER_PROMPT
    assert "patch_text" in BASE_INTERVIEWER_PROMPT


def test_explorer_prompt_supports_jri_without_inferring_preferences() -> None:
    """The explorer returns evidence, not product decisions."""
    assert "Just Ralph It" in BASE_EXPLORER_PROMPT
    assert "intent extraction" in BASE_EXPLORER_PROMPT
    assert "evidence" in BASE_EXPLORER_PROMPT
    assert "user preference" in BASE_EXPLORER_PROMPT


def test_interviewer_context_reports_missing_state(tmp_path: Path) -> None:
    """Missing scratchpad and specs are compactly represented."""
    context = build_interviewer_context(tmp_path)

    assert "Persistent scratchpad:\n(missing)" in context
    assert "Current specs:\n(none)" in context
    assert "raw JSONL logs" not in context


def test_interviewer_context_includes_scratchpad_and_specs(
    tmp_path: Path,
) -> None:
    """Scratchpad and spec files become lean turn context."""
    specs = tmp_path / ".jri" / "specs"
    specs.mkdir(parents=True)
    (tmp_path / ".jri" / "scratchpad.md").write_text("# Scratchpad\n")
    (specs / "product.md").write_text("# Product\n")

    context = build_interviewer_context(tmp_path)

    assert "# Scratchpad" in context
    assert "--- .jri/specs/product.md ---" in context
    assert "# Product" in context
