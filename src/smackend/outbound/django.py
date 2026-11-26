from importlib.util import find_spec
import json
from math import ceil
import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections


def configure(target, environment, *, production):
    if find_spec("httpx") is None:
        raise ImproperlyConfigured("Install the Smackend http extra before enabling outbound HTTP.")
    from .client import Provider

    try:
        raw = json.loads(environment.get("SMACKEND_HTTP_PROVIDERS", "{}"))
        if not isinstance(raw, dict) or len(raw) > 50:
            raise ValueError
    except (TypeError, ValueError):
        raise ImproperlyConfigured("SMACKEND_HTTP_PROVIDERS must be a JSON object of named providers.") from None
    providers = {}
    for name, entry in raw.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or not isinstance(entry, dict):
            raise ImproperlyConfigured("Use lowercase provider names and objects in SMACKEND_HTTP_PROVIDERS.")
        options = entry.copy()
        credentials = options.pop("credentials", {})
        if "headers" in options or not isinstance(credentials, dict):
            raise ImproperlyConfigured(f"Provider {name} must reference header credentials through environment names.")
        headers = {}
        for header, reference in credentials.items():
            if (not isinstance(reference, dict) or set(reference) - {"env", "prefix"}
                    or not isinstance(reference.get("env"), str)
                    or not re.fullmatch(r"[A-Z_][A-Z_0-9]{0,80}", reference["env"])
                    or not isinstance(reference.get("prefix", ""), str)):
                raise ImproperlyConfigured(f"Provider {name} contains an invalid credential reference.")
            key = reference["env"]
            value = environment.get(key)
            if not value:
                raise ImproperlyConfigured(f"Provider {name} is missing credential variable {key}.")
            headers[header] = reference.get("prefix", "") + value
        if production and options.get("allow_http"):
            raise ImproperlyConfigured(f"Provider {name} must use HTTPS in production.")
        try:
            providers[name] = Provider(**options, headers=headers)
        except (TypeError, ValueError):
            raise ImproperlyConfigured(f"Provider {name} has an invalid origin, header, or resource limit.") from None
    target["SMACKEND_OUTBOUND_PROVIDERS"] = providers
    # HTTPX normally logs the full URL. Keep it away from the app's logging handlers.
    for name in ("httpx", "httpcore"):
        target["LOGGING"]["loggers"][name] = {"level": "WARNING", "propagate": True}


def provider(name):
    return settings.SMACKEND_OUTBOUND_PROVIDERS[name]


def install_http_errors(api):
    from smackend.errors import error_response

    from .client import ProviderFailure

    @api.exception_handler(ProviderFailure)
    def provider_error(request, exc):
        status, message = {
            "provider_timeout": (504, "The upstream service timed out."),
            "provider_busy": (503, "The upstream service is busy."),
            "rate_limited": (503, "The upstream service is rate limited."),
        }.get(exc.code, (502, "The upstream service could not complete the request."))
        headers = {"Retry-After": str(ceil(exc.retry_after))} if exc.retry_after is not None else None
        return error_response(request, status, code=exc.code, message=message, headers=headers)


def release_django_connections():
    """Call in the same synchronous unit of work that acquired the connections."""
    database_connections = connections.all(initialized_only=True)
    if any(connection.in_atomic_block for connection in database_connections):
        raise RuntimeError("Finish the transaction before calling external services.")
    for connection in database_connections:
        connection.close()
