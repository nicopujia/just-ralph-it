import re

from yaml import safe_dump

__all__ = ["quote", "render", "truncate"]

# A block ends at a fence of its own length, and that fence is longer
# than any backtick run the text holds, so nothing the text says can
# close the block quoting it, nor the blocks enclosing that one.
FENCE = "`"
# The characters CommonMark opens a fence with, the most spaces it
# lets one be indented by, and the breaks it ends a line on: a fence
# read by any other rule closes a block the text had left open, or
# leaves open one the text had closed.
FENCE_CHARACTERS = frozenset({FENCE, "~"})
MARKDOWN_LINE_BREAK = re.compile(r"\r\n|[\r\n]")
MAX_FENCE_INDENTATION = 3
MIN_FENCE_LENGTH = 3
STRUCTURE_INDENTATION = "  "
# The breaks the serializer writes raw, escaping every other one, and
# after which it writes its own indentation: the block's indentation
# goes there too, or a line a value's own break opens sits at the depth
# an entry does and reads as one.
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
            # A set quotes itself through the serializer the notebook
            # already writes with, so no item can forge a sibling and
            # no fence has to stand between them.
            dumped = safe_dump(value, sort_keys=False, allow_unicode=True, width=10**9)
            indented = YAML_LINE_BREAK.sub(f"\\g<0>{STRUCTURE_INDENTATION}", dumped.removesuffix("\n"))
            rendered.append(f"{title}:\n{STRUCTURE_INDENTATION}{indented}")
    return "\n\n".join(rendered)


def truncate(text: str, length: int) -> str:
    if len(text) <= length:
        return text
    cut = length
    closing = _close_block(text[:cut])
    # The fence a cut has to end with is part of the budget rather
    # than an addition to it, and a shorter cut can end in a block of
    # another fence, so the room it takes is measured at every cut.
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
            # An indented fence met with no block open either opens one
            # of the document's or closes one a list item or a quote
            # opened at the indentation it holds its content at, and
            # the line alone does not say which. Nothing past a line
            # that reads both ways can be closed: a fence ending a cut
            # where the text had closed its block opens a block, which
            # is the very thing ending a cut is meant to spare the
            # sentence that follows.
            if indentation:
                return ""
            # A backtick fence carries no backtick in the info string it
            # opens with -- and the half a run a cut leaves behind opens
            # a block, nothing telling a reader it is half of anything.
            if character == FENCE and FENCE in rest:
                continue
            fence = character * run
        # A block ends at a run of its own character, its own length or
        # longer, indented no further than a fence may be and followed
        # by nothing but spaces and tabs, so one block is open at a time
        # whatever the runs inside it look like.
        elif (
            indentation <= MAX_FENCE_INDENTATION
            and character == fence[0]
            and run >= len(fence)
            and not rest.strip(" \t")
        ):
            fence = ""
    return f"\n{fence}" if fence else ""
