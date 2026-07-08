from jri.core.service import Service

from .app import App
from .utils import get_settings_or_print_error


def main() -> None:
    settings = get_settings_or_print_error()
    service = Service(settings)
    app = App(service)
    app.run()


if __name__ == "__main__":
    main()
