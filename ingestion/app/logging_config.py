"""Shared structlog configuration.

Per IMPLEMENTATION_PLAN.md §2 Operational standards: JSON lines to stdout via
structlog, with fields `ts`, `level`, `service`, `event`, plus contextual
fields such as `source`, `url`, `duration_ms`. Both Python services
(ingestion, mcp-server) follow this same shape.

Never log secrets or PII (e.g. SYNC_TOKEN, Authorization headers, raw page
bodies) — callers must pass only sanitized context.
"""

from __future__ import annotations

import logging
import sys

import structlog

_CONFIGURED = False


def configure_logging(service: str = "ingestion", level: int = logging.INFO) -> None:
    """Configure structlog to emit JSON lines to stdout.

    Idempotent — safe to call multiple times (e.g. once per module import
    during tests).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", key="ts"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True
    get_logger(service=service).info("logging_configured")


def get_logger(service: str = "ingestion", **initial_context):
    """Return a structlog logger bound with the `service` field."""
    configure_logging(service=service)
    return structlog.get_logger().bind(service=service, **initial_context)
