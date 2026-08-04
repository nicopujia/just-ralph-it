from pathlib import Path

from jri.core.settings import Settings
from jri.core.workspace import Installation, Workspace


def install_workspace(path: Path, *, force: bool = False) -> Installation:
    return Workspace(path).install(Settings.render_config(), force=force)
