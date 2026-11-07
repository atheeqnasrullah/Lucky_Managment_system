"""Structured JSON logging with PII redaction and trace-id propagation."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")

# 10–15 digits with optional single space/dash separators (E.164-ish); dates have only 8 digits.
_PHONE_RE = re.compile(r"(?<![\w-])\+?\d(?:[\s-]?\d){9,14}(?![\w-])")


def redact(text: str) -> str:
    """Mask phone-number-like sequences, keeping the last 4 digits."""
    return _PHONE_RE.sub(lambda m: "***" + re.sub(r"\D", "", m.group())[-4:], text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "trace_id": trace_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update({k: redact(str(v)) for k, v in extra.items()})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_extra(**fields: object) -> dict:
    return {"extra_fields": fields}
