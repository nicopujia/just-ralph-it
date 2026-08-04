"""Read the appearance the operating system is set to."""

import platform
import subprocess
from typing import Literal

__all__ = ["Appearance", "read_appearance"]

type Appearance = Literal["dark", "light"]

DARWIN_COMMAND = ("/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle")


def read_appearance() -> Appearance:
    """Read whether the system is set to a dark or light appearance.

    Returns:
        The system appearance, defaulting to dark where the operating
        system does not report one.
    """

    if platform.system() != "Darwin":
        return "dark"
    result = subprocess.run(DARWIN_COMMAND, capture_output=True, text=True, check=False)
    return "dark" if result.stdout.strip() == "Dark" else "light"
