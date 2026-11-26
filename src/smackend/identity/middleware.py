"""Bound admission and serialize writes to the same browser's pending flows."""

from hashlib import sha256
import json
import logging

from allauth.usersessions import app_settings as session_settings
from allauth.usersessions.middleware import UserSessionsMiddleware
from asgiref.sync import async_to_sync, iscoroutinefunction, sync_to_async
from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.http import HttpResponse
from django.utils.decorators import sync_and_async_middleware

from smackend.errors import error_response

from .identifiers import canonical_email, canonical_username, is_e164
from .rate_limit import spend, client_address


logger = logging.getLogger("smackend.identity")
BROWSER = "/identity/browser/v1/"
CALLBACK = "/identity/google/login/callback/"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DELIVERY_ENTRY = {"auth/signup", "auth/code/request", "auth/password/request", "account/email", "account/phone"}
CHALLENGES = {
    "auth/email/verify", "auth/phone/verify", "auth/password/reset", "auth/code/confirm",
    "auth/reauthenticate", "auth/reauthenticate/code/confirm", "account/password/change",
}
IDENTIFIER_FIELDS = {
    "username": canonical_username,
    "email": canonical_email,
    "phone": lambda value: value if is_e164(value) else "",
}


def identity_error(request, status, code, message, **headers):
    return error_response(request, status, code=code, message=message, headers=headers)


def identity_path(path):
    if path.startswith(BROWSER):
        return path.removeprefix(BROWSER)
    api_prefix = getattr(settings, "SMACKEND_API_PREFIX", "/api").rstrip("/")
    return "@profile" if path.startswith(api_prefix + "/account/") else None


def allowed(endpoint, method, profile):
    if endpoint in {"auth/provider/token", "auth/provider/signup"}:
        return False
    if endpoint == "auth/signup" and not profile.allow_signup:
        return False
    if endpoint in {"auth/password/request", "auth/password/reset"}:
        return profile.reset and method not in SAFE_METHODS
    if endpoint == "account/password/change":
        return bool(profile.password_login)
    if endpoint in {"account/email", "auth/email/verify", "auth/email/verify/resend"}:
        return "email" in profile.fields and (endpoint == "account/email" or method not in SAFE_METHODS)
    if endpoint in {"account/phone", "auth/phone/verify", "auth/phone/verify/resend"}:
        return "phone" in profile.fields
    if endpoint in {"auth/code/request", "auth/code/confirm", "auth/code/resend"}:
        return bool(profile.otp)
    return True


def payload_for(request):
    if len(request.body) > 16_384:
        raise RequestDataTooBig
    data = json.loads(request.body) if request.body else {}
    if not isinstance(data, dict):
        raise ValueError
    return data


def submitted_identifiers(payload):
    for name, normalize in IDENTIFIER_FIELDS.items():
        value = payload.get(name)
        if isinstance(value, str) and 0 < len(value) <= 254:
            if normalized := normalize(value):
                yield name, normalized


def serialize_session(request, get_response):
    key = request.COOKIES.get(settings.SESSION_COOKIE_NAME)
    if not key or len(key) != 32 or not key.isascii() or not key.isalnum():
        return get_response(request)
    digest = sha256(f"smackend.identity.session:{key}".encode()).digest()
    lock_id = int.from_bytes(digest[:8], "big", signed=True)
    # This is outside SessionMiddleware, so Django reads and saves the session under
    # the same lock. The try-lock bounds waiting and works across Uvicorn workers.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [lock_id])
            if not cursor.fetchone()[0]:
                return identity_error(request, 409, "request_in_progress", "Another account request is in progress.")
        response = get_response(request)
        if response.status_code >= 500:
            transaction.set_rollback(True)
        return response


def _sync_guard(get_response):
    def middleware(request):
        path = request.path_info
        endpoint = identity_path(path)
        callback = path == CALLBACK
        if path.startswith("/identity/google/") and not callback:
            return identity_error(request, 404, "not_found", "Not found.")
        if endpoint is None and not callback:
            return get_response(request)
        profile = settings.SMACKEND_IDENTITY_PROFILE
        if endpoint and not allowed(endpoint, request.method, profile):
            return identity_error(request, 404, "not_found", "Not found.")
        if not callback and request.method in SAFE_METHODS:
            return get_response(request)
        try:
            retry = spend("global", "") or spend("ip", client_address(request))
            body_gated = endpoint == "auth/login" or endpoint in DELIVERY_ENTRY
            if not retry and body_gated:
                data = payload_for(request)
                identifiers = list(submitted_identifiers(data))
                permitted = profile.password_login if endpoint == "auth/login" else profile.otp
                if endpoint in {"auth/login", "auth/code/request"} and (
                    len(identifiers) != 1 or identifiers[0][0] not in permitted
                ):
                    return identity_error(request, 400, "invalid_input", "Supply one permitted sign-in identifier.")
                for field, value in identifiers:
                    if endpoint == "auth/login":
                        retry = spend("password", f"{field}:{value}")
                    elif field != "username":
                        retry = spend("delivery", f"{field}:{value}")
                    if retry:
                        break
            if not retry and endpoint in CHALLENGES:
                cookie = request.COOKIES.get(settings.SESSION_COOKIE_NAME, "")[:40]
                retry = spend("challenge", cookie or client_address(request))
            if retry:
                return identity_error(request, 429, "rate_limited", "Try again later.", **{"Retry-After": str(retry)})
            response = get_response(request) if callback else serialize_session(request, get_response)
        except (ValueError, RequestDataTooBig):
            return identity_error(request, 400, "invalid_input", "Provide a valid account request.")
        except DatabaseError as exc:
            logger.error("Account operation failed", extra={
                "event": "identity.failure", "error_type": type(exc).__name__, "request": request,
            })
            return identity_error(request, 503, "identity_unavailable", "Account services are temporarily unavailable.")
        if response.status_code >= 500:
            return identity_error(request, 503, "identity_unavailable", "Account services are temporarily unavailable.")
        response["Cache-Control"] = "no-store"
        return response

    def exception_response(request, exc):
        if identity_path(request.path_info) is None and request.path_info != CALLBACK:
            return None
        if isinstance(exc, IntegrityError):
            logger.warning("Conflicting identity change", extra={"event": "identity.failure", "request": request})
            return identity_error(request, 409, "account_conflict", "The account change could not be completed.")
        if isinstance(exc, DatabaseError):
            logger.error("Account database operation failed", extra={
                "event": "identity.failure", "error_type": type(exc).__name__, "request": request,
            })
            return identity_error(request, 503, "identity_unavailable", "Account services are temporarily unavailable.")
        return None

    middleware.process_exception = exception_response
    return middleware


@sync_and_async_middleware
def auth_guard(get_response):
    if not iscoroutinefunction(get_response):
        return _sync_guard(get_response)
    guarded = _sync_guard(async_to_sync(get_response))
    call_guard = sync_to_async(guarded)

    async def middleware(request):
        if request.path_info.startswith("/identity/google/") or identity_path(request.path_info) is not None:
            # Keep admission and any enclosing session transaction in the same
            # thread as the synchronous Django session and allauth operations.
            return await call_guard(request)
        return await get_response(request)

    middleware.process_exception = guarded.process_exception
    return middleware


@sync_and_async_middleware
def track_sessions(get_response):
    if not iscoroutinefunction(get_response):
        return UserSessionsMiddleware(get_response)
    track = sync_to_async(UserSessionsMiddleware(async_to_sync(get_response)))

    async def middleware(request):
        if session_settings.TRACK_ACTIVITY:
            return await track(request)
        # Allauth still records sessions on login via its signal handlers.
        return await get_response(request)

    return middleware
