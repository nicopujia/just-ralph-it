import re
import textwrap

PROSE_LINE_PATTERN = re.compile(
    r"^(?!(\d+[.)]\s|[-*+•]\s|#{1,6}\s|[-*_]{3,}\s*$))",
)


def unwrap_prose(raw: str) -> str:
    """Collapse hard-wrapped prose lines; keep structured blocks as-is.

    Each blank-line-separated block whose lines are all prose (no
    bullets, headings, tables, quotes, or indented code) gets its
    internal newlines joined.  Other blocks pass through unchanged.

    Returns:
        The unwrapped string.
    """
    parsed_blocks: list[str] = []
    for block in textwrap.dedent(raw).split("\n\n"):
        all_prose = all_pipe = all_quote = all_indent = True
        for ln in block.split("\n"):
            if not ln.strip():
                continue
            all_prose &= bool(PROSE_LINE_PATTERN.match(ln))
            all_pipe &= "|" in ln
            all_quote &= ln.startswith(">")
            all_indent &= ln.startswith(("    ", "\t"))
        is_prose = all_prose or all_pipe or all_quote or all_indent
        parsed_blocks.append(block.replace("\n", " ") if is_prose else block)
    return "\n\n".join(parsed_blocks).strip()
