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
    assert "must call finalize_specs" in BASE_INTERVIEWER_PROMPT
    assert "just_ralph_it" not in BASE_INTERVIEWER_PROMPT


def test_interviewer_prompt_omits_tool_implementation_details() -> None:
    """The prompt does not teach storage paths or patch mechanics."""
    prompt = BASE_INTERVIEWER_PROMPT

    assert ".jri" not in prompt
    assert ".jri/specs" not in prompt
    assert "full path" not in prompt.lower()
    assert "absolute path" not in prompt.lower()
    assert "patch_text" not in prompt
    assert "*** Begin Patch" not in prompt
    assert "*** End Patch" not in prompt


def test_interviewer_prompt_keeps_exact_specs_hidden_by_default() -> None:
    """The interviewer does not show exact specs unless the user asks."""
    prompt = BASE_INTERVIEWER_PROMPT.lower()

    assert "do not show exact specs" in prompt
    assert "unless the user asks" in prompt


def test_interviewer_prompt_supports_non_software_truth_seeking() -> None:
    """The interviewer keeps helping when the topic is not software."""
    prompt = BASE_INTERVIEWER_PROMPT.lower()

    assert "something other than software" in prompt
    assert "keep the conversation going" in prompt
    assert "find the truth" in prompt
    assert "challenge assumptions directly" in prompt


def test_interviewer_prompt_prefers_simple_clear_language() -> None:
    """The interviewer should use plain language."""
    prompt = BASE_INTERVIEWER_PROMPT.lower()

    assert "simple, clear, easy-to-understand language" in prompt


def test_explorer_prompt_supports_jri_without_inferring_preferences() -> None:
    """The explorer returns evidence, not product decisions."""
    assert "Just Ralph It" in BASE_EXPLORER_PROMPT
    assert "intent extraction" in BASE_EXPLORER_PROMPT
    assert "evidence" in BASE_EXPLORER_PROMPT
    assert "user preference" in BASE_EXPLORER_PROMPT


def test_interviewer_context_reports_missing_state(tmp_path: Path) -> None:
    """Missing scratchpad and specs are compactly represented."""
    context = build_interviewer_context(tmp_path)

    assert "Current notes:\n(missing)" in context
    assert "Current specs:\n(none)" in context
    assert "raw JSONL logs" not in context


def test_interviewer_context_includes_scratchpad_and_specs(
    tmp_path: Path,
) -> None:
    """Notes and specs become context without exposing storage paths."""
    specs = tmp_path / ".jri" / "specs"
    specs.mkdir(parents=True)
    (tmp_path / ".jri" / "scratchpad.md").write_text("# Scratchpad\n")
    (specs / "product.md").write_text("# Product\n")

    context = build_interviewer_context(tmp_path)

    assert "Current notes:\n# Scratchpad" in context
    assert "Spec: product\n# Product" in context
    assert "# Product" in context
    assert str(tmp_path) not in context
    assert ".jri" not in context
    assert ".jri/specs" not in context
    assert "---" not in context
