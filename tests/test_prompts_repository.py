from importlib.resources import files
from string import Formatter

from jri.core.ai import prompts

PLACEHOLDER_VALUE = "a-test-value"


def read_templates() -> dict[str, str]:
    return {
        item.name.removesuffix(".md"): item.read_text(encoding="utf-8")
        for item in files(prompts).iterdir()
        if item.name.endswith(".md")
    }


# A template declares its own placeholders. Fill whatever it declares, so this reads any template as it ships.
def fill(name: str, raw: str) -> str:
    placeholders = {field for _, field, _, _ in Formatter().parse(raw) if field}
    return prompts.read(name, **dict.fromkeys(placeholders, PLACEHOLDER_VALUE))


def test_reads_a_template_stripped_of_its_surrounding_blank_lines() -> None:
    text = prompts.read("block_notice")

    assert text == text.strip()


# Fill whatever the template declares, so a template that gains a placeholder does not fail this test.
def test_fills_a_template_placeholder_with_its_value() -> None:
    text = fill("architect", read_templates()["architect"])

    assert "{architecture_specs_root}" not in text
    assert PLACEHOLDER_VALUE in text


# Ask every template that documents itself, rather than one template whose comments a rewrite can take away.
def test_drops_a_comment_line_of_a_template() -> None:
    documented = {name: raw for name, raw in read_templates().items() if "<!--" in raw}

    assert documented
    assert [name for name, raw in documented.items() if "<!--" in fill(name, raw)] == []
