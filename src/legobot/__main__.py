"""Entry point: `python -m legobot`."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from .app import main_async
from .config import Config, ConfigError


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # httpx logs every request at INFO, which is noise at a 30-minute cadence
    # multiplied by 12 pages per scan.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> int:
    configure_logging()
    log = logging.getLogger("legobot")

    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2

    log.info(
        "starting: every %d min, %s-%s %s, db=%s",
        config.poll_interval_minutes,
        config.active_start.strftime("%H:%M"),
        config.active_end.strftime("%H:%M"),
        config.tz_name,
        config.db_path,
    )

    try:
        asyncio.run(main_async(config))
    except KeyboardInterrupt:
        log.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
