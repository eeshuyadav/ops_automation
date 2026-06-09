"""Production-friendly logging for the chargeback automation.

Call setup_logging() once at process startup. After that:
    from logging_setup import log
    log.info("...")
    log.warning("...")
    log.error("...")
    log.exception("unexpected failure")   # includes the traceback

Dual output:
  * logs/chargeback.log  — rotated daily, kept 14 days, with timestamps
  * stdout               — same content, colored-free, for systemd/cron capture

Unhandled exceptions anywhere in the process are also caught and logged
with a full traceback via sys.excepthook.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

log = logging.getLogger("chargeback")


def setup_logging(
    log_dir: str | Path = "logs",
    level: int = logging.INFO,
    filename: str = "chargeback.log",
) -> None:
    """Idempotent — safe to call multiple times."""
    if getattr(setup_logging, "_done", False):
        return

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler: one file per day, keep 14 days.
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / filename,
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    log.setLevel(level)

    # Make Python's default uncaught-exception hook send to our log with
    # full traceback instead of dying silently to stderr.
    def _unhandled(exc_type, exc, tb):  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _unhandled

    setup_logging._done = True  # type: ignore[attr-defined]
    log.info("logging configured — file=%s level=%s",
             log_dir / filename, logging.getLevelName(level))
