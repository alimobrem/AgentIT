"""Structured JSON logging configuration for AgentIT."""
from __future__ import annotations

import logging
import os
import sys


def configure_logging() -> None:
    """Configure structured logging. JSON in production, human-readable in dev."""
    log_format = os.environ.get("AGENTIT_LOG_FORMAT", "json")
    log_level = os.environ.get("AGENTIT_LOG_LEVEL", "INFO").upper()

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Remove existing handlers
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)

    if log_format == "json":
        from pythonjsonlogger.json import JsonFormatter
        formatter = JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("kafka").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
