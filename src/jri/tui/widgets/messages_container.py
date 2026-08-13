from collections.abc import Awaitable, Callable
from typing import override

from textual import events
from textual.containers import VerticalScroll

from jri.tui import styles


class MessagesContainer(VerticalScroll):
    # Tab moves between the message input and the buttons in the turns.
    # A scroll area that accepts focus becomes an unwanted stop.
    # The reader scrolls with the mouse and with the anchor that follows a reply.
    can_focus = False

    def __init__(self, on_scroll: Callable[[], None], on_top: Callable[[], Awaitable[None]]) -> None:
        super().__init__(id=styles.MESSAGES_CONTAINER_ID)
        self.on_scroll = on_scroll
        self.on_top = on_top

    @override
    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if old_value > 0 and new_value <= 0:
            self._load_older()

    @override
    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if not event.ctrl and not event.shift:
            self.on_scroll()
        super()._on_mouse_scroll_up(event)
        if not event.ctrl and not event.shift and self.scroll_target_y <= 0:
            self._load_older()

    @override
    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if not event.ctrl and not event.shift:
            self.on_scroll()
        super()._on_mouse_scroll_down(event)

    def _load_older(self) -> None:
        self.run_worker(self.on_top(), group="history")
