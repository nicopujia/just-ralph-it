import re

from yaml import safe_dump

__all__ = ["quote", "render", "truncate"]

# A block ends with the tag that opened it. This tag is absent from the
# text. The text cannot close this block or a block that contains it.
TAG = re.compile(r"<([a-z][a-z0-9_]*(?:-[0-9]+)?)>")
# A text takes a tag when it holds one of the two delimiters of that
# tag. A suffix of more than nine digits gives a number that a text
# cannot make JRI count to, so a tag with such a suffix stays free.
TAG_SUFFIX = r"(?:-([0-9]{1,9}))?"
STRUCTURE_INDENTATION = "  "
# The serializer writes these line breaks without an escape and then adds
# its indentation. Add the block indentation too. Otherwise, a value line
# can have the indentation of an entry and parse as an entry.
YAML_LINE_BREAK = re.compile(r"[\n\x85\u2028\u2029]")


def quote(text: str, name: str) -> str:
    # Read the taken tags in one pass. A search of the text for each tag
    # lets the text take one more tag for each search and make the time
    # grow with the square of the length.
    taken = {int(match[1] or 0) for match in re.finditer(rf"</?{name}{TAG_SUFFIX}>", text)}
    index = 0
    # Take a tag that the text does not hold a delimiter of. The text has
    # a limited length, so a free tag is always available.
    while index in taken:
        index += 1
    tag = f"{name}-{index}" if index else name
    return f"<{tag}>\n{text}\n</{tag}>"


def render(**blocks: str | list[str] | dict[str, str] | None) -> str:
    rendered: list[str] = []
    for label, value in blocks.items():
        if value is None:
            continue
        if isinstance(value, str):
            rendered.append(quote(value, label))
        else:
            # Convert structured data to YAML. An item cannot create a
            # sibling item, so only the block tags are required.
            dumped = safe_dump(value, sort_keys=False, allow_unicode=True, width=10**9)
            indented = YAML_LINE_BREAK.sub(rf"\g<0>{STRUCTURE_INDENTATION}", dumped.removesuffix("\n"))
            rendered.append(quote(f"{STRUCTURE_INDENTATION}{indented}", label))
    return "\n\n".join(rendered)


def truncate(text: str, length: int) -> str:
    if len(text) <= length:
        return text
    cut = length
    closing = _close_block(text[:cut])
    # Include the closing tag in the length limit. A shorter cut can end
    # in a block with a different tag, so check its length at each cut.
    while cut and cut + len(closing) > length:
        cut = max(0, length - len(closing))
        closing = _close_block(text[:cut])
    return text[:cut] + closing


def _close_block(text: str) -> str:
    tag = ""
    for line in text.split("\n"):
        if not tag:
            # A tag stands alone on its line. A cut can leave a partial
            # tag, which opens no block.
            opening = TAG.fullmatch(line)
            if opening:
                tag = opening[1]
        elif line == f"</{tag}>":
            tag = ""
    return f"\n</{tag}>" if tag else ""
