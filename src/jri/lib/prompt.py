import re

from yaml import safe_dump

__all__ = ["render", "truncate"]

# A block ends at a fence of its own length, and that fence is longer
# than any backtick run the text holds, so nothing the text says can
# close the block quoting it, nor the blocks enclosing that one.
FENCE = "`"
MIN_FENCE_LENGTH = 3
STRUCTURE_INDENTATION = "  "
# The breaks the serializer writes, and no others: str.splitlines()
# also ends a line on \v, \f and the separators YAML holds inside a
# scalar, and drops the character it ended on, so a break the text
# itself carries would reach the model folded into a space.
YAML_LINE_BREAK = re.compile(r"[\n\x85\u2028\u2029]")


def render(**blocks: str | list[str] | dict[str, str] | None) -> str:
    rendered: list[str] = []
    for label, value in blocks.items():
        if value is None:
            continue
        title = label.replace("_", " ").capitalize()
        if isinstance(value, str):
            runs: list[str] = re.findall(f"{FENCE}+", value)
            fence = FENCE * max(MIN_FENCE_LENGTH, max((len(run) for run in runs), default=0) + 1)
            rendered.append(f"{title}:\n{fence}\n{value}\n{fence}")
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
    for line in text.split("\n"):
        # A block ends at the first run of its own fence or longer,
        # whatever the runs inside it look like, so one block is open
        # at a time -- and the half a run a cut leaves behind opens
        # one, nothing telling a reader it is half of anything.
        if line.strip(FENCE) or len(line) < MIN_FENCE_LENGTH:
            continue
        if not fence:
            fence = line
        elif len(line) >= len(fence):
            fence = ""
    return f"\n{fence}" if fence else ""
