import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_RESERVED_RECORD_KEYS = (
    set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())
    | {"message", "asctime"}
)


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_KEYS
        }
        if extras:
            payload.update(extras)

        return json.dumps(payload, default=str)


def configure_logging(log_level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(log_level.upper())
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(StructuredFormatter())
    root.addHandler(handler)

    for noisy_logger in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy_logger).setLevel(log_level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
