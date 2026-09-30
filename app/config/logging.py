"""Structured (JSON-lines) logging shared by the API and the worker."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Attributes every LogRecord has; anything else was passed through ``extra=`` and is emitted.
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName", "color_message"}


class JsonFormatter(logging.Formatter):
    def __init__(self, static_fields: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._static = static_fields or {}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **self._static,
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str | int = "INFO", *, service: str = "app") -> None:
    """Route all logging (including uvicorn's) to stdout as JSON lines."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter({"service": service, "pid": os.getpid()}))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # Requests are logged by our own middleware; silence uvicorn's duplicate access log.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
