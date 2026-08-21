from importlib.resources import files
from string import Formatter

from jri.core.ai import prompts

COMMENT_MARKUP = "<!--"
PLACEHOLDER_VALUE = "a-test-value"


def count_blank_lines(text: str) -> int:
    return sum(not line.strip() for line in text.splitlines())


# Use every template that has comments, and not one template by name. A rewrite can remove the comments from
# that one template.
def read_documented_templates() -> dict[str, str]:
    return {name: raw for name, raw in read_templates().items() if COMMENT_MARKUP in raw}


def read_templates() -> dict[str, str]:
    return {
        item.name.removesuffix(".md"): item.read_text(encoding="utf-8")
        for item in files(prompts).iterdir()
        if item.name.endswith(".md")
    }


# A template declares its own placeholders. Fill each placeholder that it declares, so this function reads any
# template in the form that JRI ships.
def fill(name: str, raw: str) -> str:
    placeholders = {field for _, field, _, _ in Formatter().parse(raw) if field}
    return prompts.read(name, **dict.fromkeys(placeholders, PLACEHOLDER_VALUE))


def test_reads_a_template_stripped_of_its_surrounding_blank_lines() -> None:
    text = prompts.read("block_notice")

    assert text == text.strip()


# Fill each placeholder that the template declares. A template that gets a new placeholder then does not make
# this test fail.
def test_fills_a_template_placeholder_with_its_value() -> None:
    text = fill("architect", read_templates()["architect"])

    assert "{architecture_specs_root}" not in text
    assert PLACEHOLDER_VALUE in text


def test_drops_a_comment_line_of_a_template() -> None:
    documented = read_documented_templates()

    assert documented
    assert [name for name, raw in documented.items() if COMMENT_MARKUP in fill(name, raw)] == []


# A comment line is never blank. If JRI removes the full line, the number of blank lines does not change. If JRI
# removes only the markup, the empty line stays. The model then reads a blank line that the template does not have.
def test_leaves_no_blank_line_where_a_template_comment_was() -> None:
    documented = read_documented_templates()

    assert documented
    assert {name: count_blank_lines(fill(name, raw)) for name, raw in documented.items()} == {
        name: count_blank_lines(raw.strip()) for name, raw in documented.items()
    }
