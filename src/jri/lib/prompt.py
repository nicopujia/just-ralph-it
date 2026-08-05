import re

from yaml import safe_dump

__all__ = ["render"]

# A block ends at a fence of its own length, and that fence is longer
# than any backtick run the text holds, so nothing the text says can
# close the block quoting it, nor the blocks enclosing that one.
FENCE = "`"
MIN_FENCE_LENGTH = 3
STRUCTURE_INDENTATION = "  "


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
            rendered.append(f"{title}:\n" + "\n".join(STRUCTURE_INDENTATION + line for line in dumped.splitlines()))
    return "\n\n".join(rendered)
