from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from smackend.diagnostics import logging_config
from smackend.modules import configure_settings

from . import env

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
_environment = env.read_environment(BASE_DIR / ".env")
_profile = env.choice(_environment, "DJANGO_ENV", "development", ("development", "production"))
_production = _profile == "production"

SECRET_KEY = env.required(_environment, "DJANGO_SECRET_KEY")
DEBUG = env.boolean(_environment, "DJANGO_DEBUG")
if _production:
    if DEBUG:
        raise ImproperlyConfigured("DJANGO_DEBUG must be false in production.")
    if len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith("django-insecure-"):
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be a strong random value of at least 50 characters in production.")

_hosts = (
    env.required(_environment, "DJANGO_ALLOWED_HOSTS")
    if _production else _environment.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]")
)
ALLOWED_HOSTS = env.allowed_hosts(_hosts, production=_production)
CSRF_TRUSTED_ORIGINS = env.trusted_origins(
    _environment.get("DJANGO_CSRF_TRUSTED_ORIGINS", ""), production=_production,
)
SECURE_SSL_REDIRECT = _production
SESSION_COOKIE_SECURE = _production
CSRF_COOKIE_SECURE = _production
SECURE_HSTS_SECONDS = 300 if _production else 0
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https")
    if env.boolean(_environment, "DJANGO_TRUST_PROXY_SSL_HEADER") else None
)
ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
AUTH_USER_MODEL = "accounts.User"
NINJA_PAGINATION_PER_PAGE = 25
NINJA_PAGINATION_MAX_LIMIT = 100
LOGGING = logging_config(env.choice(
    _environment, "SMACKEND_LOG_LEVEL", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
))

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "accounts",
]
MIDDLEWARE = [
    "smackend.diagnostics.request_diagnostics",
    "django.middleware.security.SecurityMiddleware",
    "smackend.errors.api_error_responses",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [FRONTEND_DIR],
    }
]
_db_password = env.required(_environment, "POSTGRES_PASSWORD")
if _production and (len(_db_password) < 16 or len(set(_db_password)) < 5):
    raise ImproperlyConfigured("POSTGRES_PASSWORD must be a strong random value of at least 16 characters in production.")
_db_host = (
    env.required(_environment, "POSTGRES_HOST")
    if _production else _environment.get("POSTGRES_HOST", "127.0.0.1")
)
_sslmode = env.choice(
    _environment, "POSTGRES_SSLMODE", "verify-full" if _production else "prefer",
    ("disable", "allow", "prefer", "require", "verify-ca", "verify-full"),
)
if _production and _sslmode != "verify-full":
    raise ImproperlyConfigured("POSTGRES_SSLMODE must be verify-full in production.")
_pool_min = env.integer(_environment, "POSTGRES_POOL_MIN_SIZE", 0, 0, 128)
_pool_max = env.integer(_environment, "POSTGRES_POOL_MAX_SIZE", 4, 1, 128)
if _pool_min > _pool_max:
    raise ImproperlyConfigured("POSTGRES_POOL_MIN_SIZE must be at most POSTGRES_POOL_MAX_SIZE.")

_pg_timeouts = {
    "statement_timeout": env.integer(_environment, "POSTGRES_STATEMENT_TIMEOUT_MS", 3000, 1, 2_147_483_647),
    "lock_timeout": env.integer(_environment, "POSTGRES_LOCK_TIMEOUT_MS", 1000, 1, 2_147_483_647),
    "idle_in_transaction_session_timeout": env.integer(
        _environment, "POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_MS", 10000, 1, 2_147_483_647,
    ),
}
_db_options = {
    "connect_timeout": env.integer(_environment, "POSTGRES_CONNECT_TIMEOUT_SECONDS", 3, 2, 300),
    "options": " ".join(f"-c {name}={milliseconds}" for name, milliseconds in _pg_timeouts.items()),
    "sslmode": _sslmode,
    "pool": {
        "min_size": _pool_min,
        "max_size": _pool_max,
        "timeout": env.integer(_environment, "POSTGRES_POOL_TIMEOUT_SECONDS", 4, 1, 300),
    },
}
if _production:
    # libpq can prefer GSS encryption even when sslmode is verify-full.
    _db_options["gssencmode"] = "disable"
if _environment.get("POSTGRES_SSLROOTCERT"):
    _db_options["sslrootcert"] = env.required(_environment, "POSTGRES_SSLROOTCERT")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.required(_environment, "POSTGRES_DB"),
        "USER": env.required(_environment, "POSTGRES_USER"),
        "PASSWORD": _db_password,
        "HOST": env.database_host(_db_host),
        "PORT": env.port(_environment, "POSTGRES_PORT", "5432" if _production else "55432"),
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": _db_options,
    }
}

configure_settings(globals(), _environment, boolean=env.boolean, production=_production)

# Keep adding ordinary Django settings, apps and middleware here as needed.
