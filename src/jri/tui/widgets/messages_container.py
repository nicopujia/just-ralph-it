from collections.abc import Callable
from typing import override

from textual import events
from textual.containers import VerticalScroll

from jri.tui import constants as c


class MessagesContainer(VerticalScroll):
    """Render messages and report manual vertical scrolling."""

    def __init__(self, on_scroll: Callable[[], None]) -> None:
        super().__init__(id=c.MESSAGES_CONTAINER_ID)
        self.on_scroll = on_scroll

    @override
    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if not event.ctrl and not event.shift:
            self.on_scroll()
        super()._on_mouse_scroll_up(event)

    @override
    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if not event.ctrl and not event.shift:
            self.on_scroll()
        super()._on_mouse_scroll_down(event)
