import logging

from .bot import TelegramBot
from .config import Settings
from .core import Core
from .llm import ClaudeLLM
from .roles import build_role
from .storage import Storage


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    core = Core(
        settings=settings,
        storage=Storage(settings.db_path),
        llm=ClaudeLLM(settings.model, settings.effort),
        role=build_role(settings.role, settings),
    )
    TelegramBot(settings, core).run()


if __name__ == "__main__":
    main()
