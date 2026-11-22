"""
Logging setup.

Two sinks, deliberately:

* **stdout** is the primary sink: whatever supervises the process (Docker,
  systemd, a PaaS) captures and rotates it. Run production Python with
  PYTHONUNBUFFERED=1 so lines arrive as they are emitted.
* **files** are the durable secondary sink, production only, for the two
  low-volume, high-value streams (errors and security events), so they outlive
  whatever window the supervisor's log capture keeps.

Loggers reach these handlers through the **root** logger. Get one with
``logging.getLogger(__name__)`` anywhere under ``app``; security events
(failed logins, token reuse, permission denials) go to ``app.security``.

Two formats, deliberately:

* In development, a line a person reads.
* In production, one JSON object per line. Whatever collects the logs can
  then filter on a field (every line of one request, every security event)
  without guessing at the layout of a sentence.

Every line carries the id of the request it was written for (see
``core.request_id``), or ``-`` outside a request.
"""

import json
import logging
import logging.config
from datetime import UTC, datetime
from typing import override

from app.core.request_id import request_id
from app.core.settings import Settings

SECURITY_LOGGER = "app.security"


class RequestIdFilter(logging.Filter):
    """Put the current request's id on every record, for the formatters to print."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        # `json.dumps` escapes line breaks, so a message can never be made to
        # look like two entries, whatever a user managed to get into it.
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(settings: Settings) -> None:
    formatter = "verbose" if settings.debug else "json"
    handlers: dict[str, dict[str, object]] = {
        "console": {
            "level": settings.log_level,
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": formatter,
            "filters": ["request_id"],
        },
    }
    root_handlers = ["console"]

    # Declared with no handlers of their own: records reach the root handlers
    # by propagation, so each entry only sets a level.
    loggers: dict[str, dict[str, object]] = {
        "app": {"level": settings.log_level, "propagate": True},
        SECURITY_LOGGER: {"level": "WARNING", "propagate": True},
    }

    if not settings.debug:
        settings.log_dir.mkdir(parents=True, exist_ok=True)

        def rotating_file(filename: str, level: str) -> dict[str, object]:
            """Build a size-rotating file handler config for one log stream."""
            return {
                "level": level,
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(settings.log_dir / filename),
                "maxBytes": settings.log_max_bytes,
                "backupCount": settings.log_backup_count,
                "encoding": "utf-8",
                # Open on first write, so a process that never errors leaves no
                # empty error log.
                "delay": True,
                "formatter": formatter,
                "filters": ["request_id"],
            }

        handlers["error_file"] = rotating_file("error.log", "ERROR")
        handlers["security_file"] = rotating_file("security.log", "WARNING")
        root_handlers.append("error_file")

        # Security events are also written to their own file, so the security
        # record survives independently of rotation on the main stream.
        loggers[SECURITY_LOGGER]["handlers"] = ["security_file"]

        # The server's own lines (startup, one per request, its errors) are
        # written by uvicorn through handlers of its own, as plain text. Take
        # those handlers away and let the records travel up to ours, so that
        # everything on the console is JSON and errors reach the error file.
        for server_logger in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            loggers[server_logger] = {"handlers": [], "level": "INFO", "propagate": True}

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": RequestIdFilter}},
            "formatters": {
                "verbose": {
                    "format": "{levelname} {asctime} [{process:d}] [{request_id}] {name} {message}",
                    "style": "{",
                    # For a record that reaches this formatter through a
                    # handler without the filter: a dash, not an error.
                    "defaults": {"request_id": "-"},
                },
                "json": {"()": JsonFormatter},
            },
            "handlers": handlers,
            "root": {"handlers": root_handlers, "level": settings.log_level},
            "loggers": loggers,
        }
    )
