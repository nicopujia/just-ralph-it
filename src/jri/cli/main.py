"""CLI entrypoint."""

from dataclasses import dataclass, field

from rich.console import Console

PRIMARY_STYLE = "yellow"
MUTED_STYLE = "bright_black"


def main() -> None:
    """Run the CLI."""

    app = App()
    app.run()


@dataclass
class App:
    console: Console = field(default_factory=Console)

    def run(self) -> None:
        while True:
            try:
                self._run_turn()
            except KeyboardInterrupt:
                self._tear_down()
                break

    def _run_turn(self) -> None:
        repl_prompt = f"[{PRIMARY_STYLE}]jri>[/{PRIMARY_STYLE}] "
        user_message = self.console.input(repl_prompt)
        self.console.print(user_message)

    def _tear_down(self) -> None:
        self.console.print("\nBye!", style=MUTED_STYLE)
