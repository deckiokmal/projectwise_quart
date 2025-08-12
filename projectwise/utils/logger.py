# projectwise/utils/logger.py
from __future__ import annotations

import logging
import sys
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

try:
    from quart import current_app
except ImportError:
    current_app = None


def get_logger(name: str) -> logging.Logger:
    """
    Logger hybrid:
    - Level & format diambil dari config Quart jika ada, atau default jika tidak.
    - Output ke console.
    - Output ke file (rotasi harian, backup 90 hari).
    """
    logger = logging.getLogger(name)

    # Default setting
    log_level = "INFO"
    log_format = "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"
    log_retention = 90

    # Ambil dari Quart config jika ada
    if current_app:
        try:
            log_level = current_app.config.get("LOG_LEVEL", log_level)
            log_format = current_app.config.get("LOG_FORMAT", log_format)
            log_retention = current_app.config.get("LOG_RETENTION", log_retention)
        except RuntimeError:
            pass  # current_app dipanggil di luar context

    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.propagate = False

    if not logger.handlers:
        # === Console handler ===
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        console_handler.setFormatter(logging.Formatter(log_format))
        logger.addHandler(console_handler)

        # === File handler ===
        log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_name = name.split(".")[-1] + ".log"
        log_file = log_dir / file_name

        file_handler = TimedRotatingFileHandler(
            filename=log_file,
            when="midnight",
            interval=1,
            backupCount=log_retention,  # type: ignore
            encoding="utf-8",
        )
        file_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        file_handler.setFormatter(logging.Formatter(log_format))
        logger.addHandler(file_handler)

        logger.debug(f"[Logger] Initialized '{name}', log file → {log_file}")

    return logger
