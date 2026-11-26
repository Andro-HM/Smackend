"""Safe JSON failures for native Ninja operations and the surrounding API URL space."""

from http import HTTPStatus
import logging

from asgiref.sync import iscoroutinefunction
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import Http404, JsonResponse
from django.utils.decorators import sync_and_async_middleware
from ninja import Schema
from ninja.errors import HttpError, Throttled, ValidationError

from .database import DATABASE_ERRORS, database_error

logger = logging.getLogger("smackend.api")
STATUS_CODES = {
    400: "bad_request", 401: "unauthenticated", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 409: "conflict", 422: "validation_error", 429: "rate_limited",
    500: "internal_error", 503: "service_unavailable", 504: "timeout",
}
INPUT_SOURCES = {"body", "path", "query", "header", "cookie", "form", "file"}
BODY_HEADERS = {"content-type", "content-length", "content-encoding", "content-range", "etag", "last-modified"}


class ErrorBody(Schema):
    code: str
    message: str
    request_id: str | None = None
    issues: list[str] | None = None


class APIError(Exception):
    """An intentional public application error. Supply client-safe text and headers."""

    def __init__(self, status, code, public_message, *, headers=None):
        if not 400 <= status <= 599:
            raise ValueError("APIError requires a 4xx or 5xx status")
        super().__init__(code)
        self.status = status
        self.code = code
        self.public_message = public_message
        self.headers = headers


def error_response(request, status, *, code=None, message=None, headers=None, issues=None, readiness=False):
    if message is None:
        try:
            message = HTTPStatus(status).phrase + "."
        except ValueError:
            message = "Request failed."
    body = {"code": code or STATUS_CODES.get(status, "http_error"), "message": message}
    if request_id := getattr(request, "smackend_request_id", None):
        body["request_id"] = request_id
    if issues is not None:
        body["issues"] = issues
    if readiness:
        body["status"] = "unavailable"
    response = JsonResponse(body, status=status, headers={
        key: value for key, value in (headers or {}).items() if key.lower() not in BODY_HEADERS
    })
    response["Cache-Control"] = "no-store"
    response._smackend_error = True
    return response


def validation_failure(request, exc):
    # Nested locations and messages may include keys or values supplied by the client.
    sources = set()
    for error in exc.errors:
        location = error.get("loc")
        if location and isinstance(location[0], str) and location[0] in INPUT_SOURCES:
            sources.add(location[0])
    return error_response(request, 422, message="Request validation failed.", issues=sorted(sources))


def unexpected_failure(request, exc):
    logger.error("Unhandled API exception", extra={
        "event": "api.exception", "error_type": type(exc).__name__, "request": request, "status_code": 500,
    })
    return error_response(request, 500)


def database_failure(request, exc):
    status, code = database_error(exc)
    if status == 500:
        return unexpected_failure(request, exc)
    logger.warning("Database operation failed", extra={
        "event": "database.failure", "error_type": type(exc).__name__, "request": request, "status_code": status,
    })
    return error_response(request, status, code=code)


def http_failure(request, exc):
    headers = None
    if isinstance(exc, Throttled) and exc.wait is not None:
        headers = {"Retry-After": str(exc.wait)}
    return error_response(request, exc.status_code, headers=headers)


def install_error_handlers(api):
    """Register defaults on a native NinjaAPI. Applications may override individual handlers."""
    handlers = {
        Exception: unexpected_failure,
        APIError: lambda request, exc: error_response(
            request, exc.status, code=exc.code, message=exc.public_message, headers=exc.headers,
        ),
        ValidationError: validation_failure,
        Http404: lambda request, exc: error_response(request, 404),
        PermissionDenied: lambda request, exc: error_response(request, 403),
        HttpError: http_failure,
        TimeoutError: lambda request, exc: error_response(request, 504),
        **dict.fromkeys((*DATABASE_ERRORS, IntegrityError), database_failure),
    }
    for exception, handler in handlers.items():
        api.add_exception_handler(exception, handler)


def normalize_error(request, response):
    prefix = getattr(settings, "SMACKEND_API_PREFIX", "/api").rstrip("/")
    path = request.path_info
    if getattr(request, "smackend_upload_rejected", False):
        return error_response(request, 413, code="upload_limit", message="The upload exceeds the allowed size or count.")
    if (path == prefix or path.startswith(prefix + "/")) and response.status_code >= 400 and not getattr(
        response, "_smackend_error", False
    ):
        safe = error_response(request, response.status_code, headers=response.headers)
        safe.cookies = response.cookies
        return safe
    return response


@sync_and_async_middleware
def api_error_responses(get_response):
    """Cover Django responses outside Ninja operation exception handlers."""
    if iscoroutinefunction(get_response):
        async def middleware(request):
            return normalize_error(request, await get_response(request))
    else:
        def middleware(request):
            return normalize_error(request, get_response(request))
    return middleware
