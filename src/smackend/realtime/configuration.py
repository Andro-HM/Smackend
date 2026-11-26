from importlib.util import find_spec
import re
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


def integer(environment, name, default, maximum):
    value = environment.get(name, str(default))
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= maximum:
        raise ImproperlyConfigured(f"{name} must be an integer from 1 to {maximum}.")
    return int(value)


def configure(settings, environment, *, production):
    if find_spec("channels") is None or find_spec("websockets") is None:
        raise ImproperlyConfigured("Install the Smackend realtime extra before enabling WebSockets.")
    defaults = "" if production else "http://127.0.0.1:8000,http://localhost:8000"
    origins = [value.strip() for value in environment.get("SMACKEND_REALTIME_ORIGINS", defaults).split(",")]
    try:
        for origin in origins:
            url = urlsplit(origin)
            if (url.scheme not in ({"https"} if production else {"http", "https"}) or not url.hostname
                    or url.username is not None or url.path or url.query or url.fragment or url.port == 0
                    or "*" in origin or url.hostname.startswith(".") or re.search(r"\s", origin)):
                raise ValueError
    except ValueError:
        raise ImproperlyConfigured("SMACKEND_REALTIME_ORIGINS needs exact browser origins, using HTTPS in production.") from None
    backend = environment.get("SMACKEND_REALTIME_VOICE", "mock")
    if backend not in {"mock", "gemini", "disabled"}:
        raise ImproperlyConfigured("SMACKEND_REALTIME_VOICE must be mock, gemini or disabled.")
    settings["INSTALLED_APPS"] += ["channels", "smackend.realtime"]
    settings["CHANNEL_LAYERS"] = {}  # Point-to-point connections do not require a channel layer.
    settings.update(
        SMACKEND_REALTIME_ALLOWED_ORIGINS=origins,
        SMACKEND_REALTIME_VOICE_MODE=backend,
        SMACKEND_REALTIME_CONNECTION_LIMIT=integer(environment, "SMACKEND_REALTIME_CONNECTION_LIMIT", 40, 512),
        SMACKEND_REALTIME_USER_CONNECTIONS=integer(environment, "SMACKEND_REALTIME_USER_CONNECTIONS", 3, 32),
        SMACKEND_REALTIME_SECONDS=integer(environment, "SMACKEND_REALTIME_SECONDS", 180, 600),
    )
    if backend == "gemini":
        model = environment.get("SMACKEND_GEMINI_LIVE_MODEL", "")
        key = environment.get("SMACKEND_GEMINI_LIVE_KEY", "")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,120}", model) or not key:
            raise ImproperlyConfigured("Gemini Live needs SMACKEND_GEMINI_LIVE_MODEL and SMACKEND_GEMINI_LIVE_KEY.")
        quotas = settings.get("SMACKEND_AI_QUOTA_CONFIG", {})
        if not settings.get("SMACKEND_AI_ENABLED") or any("cost_micros" in options for options in quotas.values()):
            raise ImproperlyConfigured("Gemini Live needs the AI module and token quotas. Monetary caps require a reliably priced operation.")
        settings.update(
            SMACKEND_GEMINI_LIVE_MODEL=model,
            SMACKEND_GEMINI_LIVE_KEY=key,
            SMACKEND_REALTIME_TOKEN_RESERVATION=integer(environment, "SMACKEND_REALTIME_TOKEN_RESERVATION", 12000, 60000),
        )
