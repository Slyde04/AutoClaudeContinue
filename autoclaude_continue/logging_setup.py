"""Logging setup for Claude Auto-Continue."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import default_data_dir


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("autoclaude_continue")
    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    log_dir = default_data_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(file_handler)
    except OSError:
        pass

    # When launched with pythonw.exe there is no console; only add a stream
    # handler when stdout is actually available.
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        logger.addHandler(stream)

    return logger


def log_file_path() -> Path:
    return default_data_dir() / "app.log"
