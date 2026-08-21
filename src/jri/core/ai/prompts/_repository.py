import re
from pathlib import Path

# A line that holds only this markup documents the template beside the text that it explains. It never reaches the
# model.
COMMENT_LINE = re.compile(r"^[ \t]*<!--.*-->[ \t]*\n?", re.MULTILINE)

_directory = Path(__file__).parent


# This repository uses UTF-8. Read a template with this encoding so every platform loads the same text.
def read(name: str, **placeholders: str) -> str:
    raw = _directory.joinpath(f"{name}.md").read_text(encoding="utf-8")
    template = COMMENT_LINE.sub("", raw).strip()
    return template.format(**placeholders)
