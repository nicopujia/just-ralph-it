import re
import textwrap


def unwrap_prose(raw: str) -> str:
    """Collapse hard-wrapped prose and simple list items.

    Blank-line-separated prose paragraphs are joined onto one line.
    Flat markdown list blocks also have wrapped continuation lines
    folded into their preceding list item. Other structured blocks
    pass through unchanged.

    Examples:
        >>> unwrap_prose('''hello
        ... world''')
        'hello world'
        >>> unwrap_prose('''
        ... - hello,
        ... goodbye
        ... - john
        ... ''')
        '- hello, goodbye\\n- john'
        >>> print(unwrap_prose('''
        ... |  a  |  b  |
        ... | --- | --- |
        ... |  c  |  d  |'''))
        |  a  |  b  |
        | --- | --- |
        |  c  |  d  |
        >>>

    Returns:
        The unwrapped string.
    """
    item_re = re.compile(r"^((?:\d+[.)]|[-*+•])\s+)(.*)$")
    rule_re = re.compile(r"^[-*_]{3,}\s*$")
    table_re = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
    out: list[str] = []

    for block in textwrap.dedent(raw).split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            out.append("")
            continue
        if len(lines) > 1 and table_re.match(lines[1]) and all("|" in line for line in lines):
            out.append(block)
            continue
        if item_re.match(lines[0]) and all(
            item_re.match(line)
            or not (
                re.match(r"^#{1,6}\s", line)
                or rule_re.match(line.strip())
                or line.startswith(("    ", "\t"))
                or line.strip().startswith((">", "|", "```", "~~~"))
            )
            for line in lines[1:]
        ):
            items: list[str] = []
            for line in lines:
                if match := item_re.match(line):
                    items.append(f"{match[1]}{match[2].strip()}")
                else:
                    items[-1] += f" {line.strip()}"
            out.append("\n".join(items))
            continue
        if all(
            not (
                item_re.match(line)
                or re.match(r"^#{1,6}\s", line)
                or rule_re.match(line.strip())
                or line.startswith(("    ", "\t"))
                or line.strip().startswith((">", "|", "```", "~~~"))
            )
            for line in lines
        ):
            out.append(" ".join(line.strip() for line in lines))
            continue
        out.append(block)

    return "\n\n".join(out).strip()
