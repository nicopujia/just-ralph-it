from rich.console import Console

from jri.core.exceptions import JriUnauthenticatedError
from jri.core.service import Service

from .styles import ERROR, MUTED, PRIMARY


class App:
    REPL_PROMPT: str = f"[{PRIMARY}]jri>[/{PRIMARY}] "
    EXIT_MESSAGE: str = f"[{MUTED}]Bye![/{MUTED}]"
    UNAUTHENTICATED_MESSAGE: str = (
        f"[{ERROR}]"
        "Please set JRI_API_KEY to an API key compatible with "
        "JRI_PROVIDER_BASE_URL, which defaults to OpenAI as the provider."
        f"[/{ERROR}]"
    )

    def __init__(
        self,
        console: Console | None = None,
        service: Service | None = None,
    ) -> None:
        self.console: Console = console or Console()
        try:
            self.service: Service = service or Service()
        except JriUnauthenticatedError:
            self.console.print(self.UNAUTHENTICATED_MESSAGE)

    def run(self) -> None:
        while True:
            try:
                self._run_turn()
            except KeyboardInterrupt:
                self._tear_down()
                break

    def _run_turn(self) -> None:
        user_message = self.console.input(self.REPL_PROMPT)
        answer = self.service.send_message(user_message)
        self.console.print(answer)

    def _tear_down(self) -> None:
        self.console.print("\n", self.EXIT_MESSAGE)
