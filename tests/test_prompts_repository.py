from importlib.resources import files
from string import Formatter

from jri.core.ai import prompts

COMMENT_MARKUP = "<!--"
PLACEHOLDER_VALUE = "a-test-value"


def count_blank_lines(text: str) -> int:
    return sum(not line.strip() for line in text.splitlines())


# Ask every template that documents itself, rather than one template whose comments a rewrite can take away.
def read_documented_templates() -> dict[str, str]:
    return {name: raw for name, raw in read_templates().items() if COMMENT_MARKUP in raw}


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


def test_drops_a_comment_line_of_a_template() -> None:
    documented = read_documented_templates()

    assert documented
    assert [name for name, raw in documented.items() if COMMENT_MARKUP in fill(name, raw)] == []


# A comment line is never blank, so dropping the whole line changes no blank-line count. Dropping only the markup
# leaves the line, and the model then reads a prompt with a blank line the template does not have.
def test_leaves_no_blank_line_where_a_template_comment_was() -> None:
    documented = read_documented_templates()

    assert documented
    assert {name: count_blank_lines(fill(name, raw)) for name, raw in documented.items()} == {
        name: count_blank_lines(raw.strip()) for name, raw in documented.items()
    }
