"""Central logging: console output plus a rotating daily log file.

Call `setup_logging()` once at process start. Logs go to the console (as
before) AND to `LOG_DIR/bot.log`, rotated at midnight, keeping
`LOG_BACKUP_DAYS` days of dated history (e.g. bot.log.2026-07-25).

The file handler uses `delay=True` (the file isn't opened until the first
log line) and is best-effort: if it can't be created — missing folder,
no permission — it warns and continues with console logging. Logging must
never crash the bot.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler

from bot import config

_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger. Idempotent — safe to call more than once."""
    global _configured
    if _configured:
        return
    _configured = True

    fmt = logging.Formatter(_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # Quieten noisy third-party loggers (matches the old basicConfig tweak).
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Rotating daily file — best effort; never crash on failure.
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        log_file = os.path.join(config.LOG_DIR, "bot.log")
        file_handler = TimedRotatingFileHandler(
            log_file,
            when="midnight",
            backupCount=config.LOG_BACKUP_DAYS,
            encoding="utf-8",
            delay=True,  # don't open the file until the first log line
        )
        file_handler.suffix = "%Y-%m-%d"
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        root.info(
            "file logging -> %s (rotates midnight, keeps %d days)",
            log_file, config.LOG_BACKUP_DAYS,
        )
    except OSError as exc:  # covers PermissionError and missing-folder cases
        root.warning("file logging disabled (%s) — console only", exc)
