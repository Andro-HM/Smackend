import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
ROOT_URLCONF = "lab.urls"
ASGI_APPLICATION = "lab.asgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "allauth",
    "allauth.account",
    "allauth.headless",
    "accounts",
    "notes",
    "habits",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "lab.views.lab_responses_inline" if os.getenv("PHASE0_INLINE_HEADERS") == "1" else "lab.views.LabResponses",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "pages"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]
test_database = os.environ.get("PHASE0_TEST_DB", "test_phase0_notes")
if not test_database.startswith("test_phase0_") or test_database == os.environ["POSTGRES_DB"]:
    raise ValueError("The test database must be separate and start with test_phase0_.")

profile_enabled = os.getenv("PHASE0_PROFILE") == "1"
pool_size = int(os.getenv("PHASE0_PROFILE_POOL_SIZE", "8")) if profile_enabled else 8
DATABASES = {
    "default": {
        "ENGINE": "lab.profile_db" if profile_enabled else "django.db.backends.postgresql",
        "NAME": os.environ["POSTGRES_DB"],
        "USER": os.environ["POSTGRES_USER"],
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "HOST": "127.0.0.1",
        "PORT": os.environ.get("POSTGRES_PORT", "55432"),
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "connect_timeout": 2,
            "options": "-c statement_timeout=4000 -c lock_timeout=3000",
            "pool": {"min_size": 0, "max_size": pool_size, "timeout": 4, "reconnect_timeout": 15},
        },
        "TEST": {"NAME": test_database},
    }
}

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["allauth.account.auth_backends.AuthenticationBackend"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"django.contrib.auth.password_validation.{name}"}
    for name in (
        "UserAttributeSimilarityValidator",
        "MinimumLengthValidator",
        "CommonPasswordValidator",
        "NumericPasswordValidator",
    )
]
ACCOUNT_ADAPTER = "accounts.adapter.AccountAdapter"
ACCOUNT_LOGIN_METHODS = {"username"}
ACCOUNT_SIGNUP_FIELDS = ["username*", "password1*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_RATE_LIMITS = {"signup": "300/m/ip", "login": "300/m/ip", "login_failed": "300/m/ip,10/m/key"}
PHASE0_STRICT_RATE = os.getenv("PHASE0_STRICT_RATE", "1") == "1"
if PHASE0_STRICT_RATE and os.getenv("PHASE0_RATE_PROBE_ATOMIC_ONLY") == "1":
    # Test-only profile isolates the atomic counter from cache lock contention.
    ACCOUNT_RATE_LIMITS.update(login=None, login_failed=None)
HEADLESS_ONLY = True
HEADLESS_CLIENTS = ("browser",)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "phase0_allauth_cache",
        "OPTIONS": {"MAX_ENTRIES": 10000},
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "phase0_session"
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_SAVE_EVERY_REQUEST = False
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = os.getenv("PHASE0_HTTP_LOOPBACK") != "1"
CSRF_FAILURE_VIEW = "lab.views.csrf_failure"
DATA_UPLOAD_MAX_MEMORY_SIZE = 65536
PHASE0_PROVIDER_URL = os.environ.get("PHASE0_PROVIDER_URL", "http://127.0.0.1:8011")
PHASE0_PROVIDER_RELEASE_DB = os.getenv("PHASE0_PROVIDER_RELEASE_DB", "0")
provider_address = urlsplit(PHASE0_PROVIDER_URL)
if provider_address.scheme != "http" or provider_address.hostname != "127.0.0.1" or provider_address.username:
    raise ValueError("PHASE0_PROVIDER_URL must be a local HTTP simulator on 127.0.0.1")
