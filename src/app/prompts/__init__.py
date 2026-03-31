from pathlib import Path

_DIR = Path(__file__).parent

RALPH_SYSTEM_PROMPT: str = (_DIR / "ralph.xml").read_text()
RALPHY_SYSTEM_PROMPT: str = (_DIR / "ralphy.xml").read_text()
