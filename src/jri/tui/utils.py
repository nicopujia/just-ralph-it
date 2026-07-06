import platform
import subprocess

from . import constants as c


def detect_system_theme() -> str:
    if platform.system() != "Darwin":
        return c.THEME_DEFAULT

    result = subprocess.run(
        ["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"],
        capture_output=True,
        text=True,
        check=False,
    )

    return c.THEME_DARK if result.stdout.strip() == "Dark" else c.THEME_LIGHT
