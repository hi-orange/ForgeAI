from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.settings import BACKEND_DIR, settings

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _resolve_log_file() -> Path:
    configured = Path(settings.log_file)
    return configured if configured.is_absolute() else BACKEND_DIR / configured


def configure_logging() -> Path:
    """Configure ForgeAI console and rotating UTF-8 file logs once per process."""
    log_file = _resolve_log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    forgeai_logger = logging.getLogger("forgeai")
    if getattr(forgeai_logger, "_forgeai_configured", False):
        return log_file

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    forgeai_logger.setLevel(level)
    forgeai_logger.addHandler(console_handler)
    forgeai_logger.addHandler(file_handler)
    forgeai_logger.propagate = False
    forgeai_logger._forgeai_configured = True  # type: ignore[attr-defined]
    forgeai_logger.info("File logging initialized: %s", log_file)
    return log_file
