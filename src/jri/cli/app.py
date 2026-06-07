from rich.console import Console

from jri.core.exceptions import JriConfigurationError
from jri.core.service import Service

from .constants import CONFIG_ERROR_MESSAGE, EXIT_MESSAGE, REPL_PROMPT
from .utils import get_config_error_help_message


class App:
    def __init__(
        self,
        console: Console | None = None,
        service: Service | None = None,
    ) -> None:
        self.console: Console = console or Console()
        try:
            self.service: Service = service or Service()
        except JriConfigurationError as error:
            self.console.print(CONFIG_ERROR_MESSAGE)
            self.console.print(get_config_error_help_message(error))
            raise SystemExit(1) from error

    def run(self) -> None:
        while True:
            try:
                self._run_turn()
            except KeyboardInterrupt:
                self._tear_down()
                break

    def _run_turn(self) -> None:
        user_message = self.console.input(REPL_PROMPT)
        for answer_chunk in self.service.send_message(user_message):
            self.console.out(answer_chunk, end="")
        self.console.print()

    def _tear_down(self) -> None:
        self.console.print()
        self.console.print(EXIT_MESSAGE)
