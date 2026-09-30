"""Run the API:  python -m app.api"""

from __future__ import annotations

import logging
import sys

import uvicorn
from pydantic import ValidationError

from app.config import Settings, configure_logging
from app.db import check_database, create_db_engine


def main() -> int:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2

    configure_logging(settings.numeric_log_level, service="api")
    logger = logging.getLogger("app.api")

    engine = create_db_engine(settings.database_url)
    try:
        check_database(engine)
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1
    finally:
        engine.dispose()

    logger.info("starting API", extra={"host": settings.api_host, "port": settings.api_port})
    uvicorn.run(
        "app.api.main:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # logging is configured above
        access_log=False,  # our middleware writes the access log
        timeout_graceful_shutdown=15,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
