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
"""

import logging
import logging.config

from app.core.settings import Settings

SECURITY_LOGGER = "app.security"


def configure_logging(settings: Settings) -> None:
    handlers: dict[str, dict[str, object]] = {
        "console": {
            "level": settings.log_level,
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "verbose",
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
                "formatter": "verbose",
            }

        handlers["error_file"] = rotating_file("error.log", "ERROR")
        handlers["security_file"] = rotating_file("security.log", "WARNING")
        root_handlers.append("error_file")

        # Security events are also written to their own file, so the security
        # record survives independently of rotation on the main stream.
        loggers[SECURITY_LOGGER]["handlers"] = ["security_file"]

        # Unhandled 500s are logged by uvicorn, whose loggers don't propagate
        # to the root logger; attach the error file so the traceback is kept.
        loggers["uvicorn.error"] = {"handlers": ["error_file"], "level": "INFO", "propagate": True}

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "verbose": {
                    "format": "{levelname} {asctime} [{process:d}] {name} {message}",
                    "style": "{",
                },
            },
            "handlers": handlers,
            "root": {"handlers": root_handlers, "level": settings.log_level},
            "loggers": loggers,
        }
    )
