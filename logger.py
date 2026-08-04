"""
logger.py
---------
Centralised logging configuration.

Logs are emitted to BOTH stdout (console) and a rolling file (crypto_bot.log).
Format: [%(asctime)s] [%(levelname)s] %(module)s: %(message)s
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import config

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(module)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_CONFIGURED = False


def _configure_root() -> None:
    """Attach console + rotating file handlers to the root logger exactly once."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    root.setLevel(level)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # --- Console handler -------------------------------------------------
    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # --- Rotating file handler ------------------------------------------
    try:
        log_dir = os.path.dirname(os.path.abspath(config.LOG_FILE))
        if log_dir and not os.path.isdir(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        file_handler = RotatingFileHandler(
            config.LOG_FILE,
            maxBytes=5 * 1024 * 1024,  # 5 MB per file
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - filesystem dependent
        root.warning("Could not attach file logging handler (%s). Console only.", exc)

    # Silence noisy third-party loggers.
    for noisy in ("urllib3", "websockets", "asyncio", "alpaca"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """
    Return a configured logger.

    Parameters
    ----------
    name : str
        Usually ``__name__`` of the calling module.
    """
    _configure_root()
    return logging.getLogger(name)
