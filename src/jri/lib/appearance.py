"""Read the appearance the operating system is set to."""

import platform
import subprocess
from typing import Literal

__all__ = ["Appearance", "read"]

type Appearance = Literal["dark", "light"]

DARWIN_COMMAND = ("/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle")


def read() -> Appearance | None:
    """Read whether the system is set to a dark or light appearance.

    Returns:
        The system appearance, or nothing where the operating system
        does not report one, leaving the choice to the caller.
    """

    if platform.system() != "Darwin":
        return None
    result = subprocess.run(DARWIN_COMMAND, capture_output=True, text=True, check=False)
    return "dark" if result.stdout.strip() == "Dark" else "light"
