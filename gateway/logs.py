"""Logging setup: secret redaction for every log record, plus the write audit log."""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime

REDACTED = "***"
audit_logger = logging.getLogger("gateway.audit")


class SecretRegistry:
    """Known secret values. Any log message containing one is redacted at record creation."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def add(self, *values: str | None) -> None:
        with self._lock:
            # Very short values would redact innocent text; real secrets are long.
            self._values.update(v for v in values if v and len(v) >= 6)

    def redact(self, text: str) -> str:
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value in text:
                text = text.replace(value, REDACTED)
        return text


secret_registry = SecretRegistry()
_factory_installed = False


def install_redaction() -> None:
    """Wrap the LogRecord factory so redaction applies regardless of logger or handler."""
    global _factory_installed
    if _factory_installed:
        return
    base_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs) -> logging.LogRecord:
        record = base_factory(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:
            return record
        redacted = secret_registry.redact(message)
        if redacted != message or record.args:
            record.msg, record.args = redacted, None
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


def configure_logging(level: str = "INFO") -> None:
    install_redaction()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request line at INFO; keep the upstream chatter out.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def audit_write(
    *,
    endpoint: str,
    session_id: str | None,
    key_id: str | None,
    backend: str | None,
    status_code: int,
) -> None:
    """One JSON line per write. Never pass request bodies here."""
    audit_logger.info(
        json.dumps(
            {
                "time": datetime.now(UTC).isoformat(),
                "endpoint": endpoint,
                "sessionId": session_id,
                "keyId": key_id,
                "backend": backend,
                "status": status_code,
                "result": "ok" if status_code < 400 else "error",
            }
        )
    )
