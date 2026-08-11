from jri.core.ai import prompts


def test_reads_a_template_stripped_of_its_surrounding_blank_lines() -> None:
    text = prompts.read("block_notice")

    assert text == text.strip()


def test_fills_a_template_placeholder_with_its_value() -> None:
    text = prompts.read("architect", architecture_specs_root="a-test-root")

    assert "{architecture_specs_root}" not in text
    assert "a-test-root" in text


def test_drops_a_comment_line_of_a_template() -> None:
    text = prompts.read("interviewer")

    assert "<!--" not in text
    assert text.startswith("Role: Interviewer")
