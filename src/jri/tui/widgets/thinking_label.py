from textual.content import Content
from textual.widgets import Static

from jri.tui import copy, styles


class ThinkingLabel(Static):
    # A bright point moves along the label. Each letter takes the shade for its distance from that point.
    # The terminal supplies these grays. The label agrees with the colors of its window, light or dark.
    SHADES = ("bold", "", "dim")
    # The bright point moves past the last letter before it starts again. This is the dark interval between
    # two sweeps.
    GAP = 4
    STEP = 0.1

    def __init__(self, *, is_stopping: bool = False) -> None:
        super().__init__(classes=styles.THINKING_LABEL_CLASSES)
        self.label = copy.INTERVIEWER_STOPPING if is_stopping else copy.INTERVIEWER_THINKING
        self.crest = 0
        self.sweep_timer = None

    def on_mount(self) -> None:
        self.update_copy()
        self.sweep_timer = self.set_interval(self.STEP, self.advance_sweep)

    def on_unmount(self) -> None:
        if self.sweep_timer is not None:
            self.sweep_timer.stop()

    def advance_sweep(self) -> None:
        self.crest = (self.crest + 1) % (len(self.label) + self.GAP)
        self.update_copy()

    def mark_stopping(self) -> None:
        self.label = copy.INTERVIEWER_STOPPING
        self.update_copy()

    def update_copy(self) -> None:
        self.update(
            Content.assemble(
                *(
                    (letter, self.SHADES[min(abs(index - self.crest), len(self.SHADES) - 1)])
                    for index, letter in enumerate(self.label)
                )
            )
        )
