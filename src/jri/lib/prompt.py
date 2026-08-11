import re

from yaml import safe_dump

__all__ = ["quote", "render", "truncate"]

# A block ends with a fence of the same length. This fence is longer than
# each backtick run in the text. The text cannot close this block or a
# block that contains it.
FENCE = "`"
# CommonMark uses these fence characters, this maximum indentation, and
# these line breaks. Other rules can close a block that the text keeps
# open or keep open a block that the text closes.
FENCE_CHARACTERS = frozenset({FENCE, "~"})
MARKDOWN_LINE_BREAK = re.compile(r"\r\n|[\r\n]")
MAX_FENCE_INDENTATION = 3
MIN_FENCE_LENGTH = 3
STRUCTURE_INDENTATION = "  "
# The serializer writes these line breaks without an escape and then adds
# its indentation. Add the block indentation too. Otherwise, a value line
# can have the indentation of an entry and parse as an entry.
YAML_LINE_BREAK = re.compile(r"[\n\x85\u2028\u2029]")


def quote(text: str) -> str:
    runs: list[str] = re.findall(f"{FENCE}+", text)
    fence = FENCE * max(MIN_FENCE_LENGTH, max((len(run) for run in runs), default=0) + 1)
    return f"{fence}\n{text}\n{fence}"


def render(**blocks: str | list[str] | dict[str, str] | None) -> str:
    rendered: list[str] = []
    for label, value in blocks.items():
        if value is None:
            continue
        title = label.replace("_", " ").capitalize()
        if isinstance(value, str):
            rendered.append(f"{title}:\n{quote(value)}")
        else:
            # Convert structured data to YAML. An item cannot create a
            # sibling item, so no Markdown fence is required.
            dumped = safe_dump(value, sort_keys=False, allow_unicode=True, width=10**9)
            indented = YAML_LINE_BREAK.sub(f"\\g<0>{STRUCTURE_INDENTATION}", dumped.removesuffix("\n"))
            rendered.append(f"{title}:\n{STRUCTURE_INDENTATION}{indented}")
    return "\n\n".join(rendered)


def truncate(text: str, length: int) -> str:
    if len(text) <= length:
        return text
    cut = length
    closing = _close_block(text[:cut])
    # Include the closing fence in the length limit. A shorter cut can end
    # in a block with a different fence, so check its length at each cut.
    while cut and cut + len(closing) > length:
        cut = max(0, length - len(closing))
        closing = _close_block(text[:cut])
    return text[:cut] + closing


def _close_block(text: str) -> str:
    fence = ""
    for line in MARKDOWN_LINE_BREAK.split(text):
        margin = line.lstrip(" ")
        if margin[:1] not in FENCE_CHARACTERS:
            continue
        character = margin[0]
        run = len(margin) - len(margin.lstrip(character))
        if run < MIN_FENCE_LENGTH:
            continue
        indentation = len(line) - len(margin)
        rest = margin[run:]
        if not fence:
            # With no open block, an indented fence can open a document
            # block or close a list or quote block. The line alone does
            # not identify its use. Do not close text after a line with
            # more than one meaning. A closing fence at a cut can otherwise
            # open a block and change the text that follows.
            if indentation:
                return ""
            # A backtick fence cannot have a backtick in its info string.
            # A partial run at a cut can open a block and does not show
            # that it is part of a longer run.
            if character == FENCE and FENCE in rest:
                continue
            fence = character * run
        # A block ends with its fence character, at least its fence
        # length, valid fence indentation, and only spaces or tabs after
        # it. This keeps only one block open at a time.
        elif (
            indentation <= MAX_FENCE_INDENTATION
            and character == fence[0]
            and run >= len(fence)
            and not rest.strip(" \t")
        ):
            fence = ""
    return f"\n{fence}" if fence else ""
