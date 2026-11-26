"""Structured request logs built from selected metadata, without raw log messages."""

from datetime import UTC, datetime
import json
import logging
from time import perf_counter
from uuid import uuid4

from asgiref.sync import iscoroutinefunction
from django.utils.decorators import sync_and_async_middleware

METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"}
EVENTS = {
    "request.complete", "api.exception", "database.failure", "readiness.failure",
    "identity.delivery_failed", "identity.delivery_limited", "identity.failure",
    "storage.failure",
    "outbound.failure",
    "ai.settlement_failed",
    "realtime.failure",
}
logger = logging.getLogger("smackend.requests")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        event = getattr(record, "event", None)
        data = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": event if isinstance(event, str) and event in EVENTS else "log",
        }
        if request := getattr(record, "request", None):
            if request_id := getattr(request, "smackend_request_id", None):
                data["request_id"] = request_id
            method = getattr(request, "method", "")
            data["method"] = method if method in METHODS else "OTHER"
            route = getattr(getattr(request, "resolver_match", None), "route", None)
            if route is not None:
                data["route"] = str(route)
        if isinstance(status := getattr(record, "status_code", None), int):
            data["status"] = status
        if isinstance(duration := getattr(record, "duration_ms", None), (int, float)):
            data["duration_ms"] = round(duration, 2)
        error_type = getattr(record, "error_type", "")
        if isinstance(error_type, str) and error_type.isidentifier():
            data["error_type"] = error_type
        return json.dumps(data, separators=(",", ":"))


def logging_config(level):
    """Return an ordinary Django LOGGING dictionary for apps to extend."""
    handler = {"handlers": ["json"], "level": level, "propagate": False}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"safe_json": {"()": JsonFormatter}},
        "handlers": {"json": {"class": "logging.StreamHandler", "formatter": "safe_json", "level": level}},
        "root": {"handlers": ["json"], "level": level},
        "loggers": {name: handler.copy() for name in ("django", "django.server", "smackend")},
    }


def record_response(request, response, started):
    response["X-Request-ID"] = request.smackend_request_id
    status = response.status_code
    level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
    logger.log(level, "Request complete", extra={
        "event": "request.complete", "request": request, "status_code": status,
        "duration_ms": (perf_counter() - started) * 1000,
    })
    return response


@sync_and_async_middleware
def request_diagnostics(get_response):
    if iscoroutinefunction(get_response):
        async def middleware(request):
            request.smackend_request_id = uuid4().hex
            started = perf_counter()
            return record_response(request, await get_response(request), started)
    else:
        def middleware(request):
            request.smackend_request_id = uuid4().hex
            started = perf_counter()
            return record_response(request, get_response(request), started)
    return middleware
