import logging

from jri.core.service import Service

from .app import App
from .utils import get_settings_or_print_error

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings_or_print_error()
    service = Service(settings)
    app = App(service)
    logger.info("started")
    try:
        app.run()
    except BaseException:
        logger.exception("failed")
        raise
    finally:
        logger.info("finished")
        logging.shutdown()


if __name__ == "__main__":
    main()
