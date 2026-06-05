"""Command-line interface client.

This package is no more than a thin wrapper
around the SDK provided by `jri.core`, containing only logic
related to the user interface in the terminal.
"""

from .main import main

__all__ = ["main"]
