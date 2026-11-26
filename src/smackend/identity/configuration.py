"""Translate Smackend's opt-in profile into ordinary allauth and Django settings."""

from dataclasses import dataclass
from importlib.util import find_spec
import re

from django.core.exceptions import ImproperlyConfigured


IDENTIFIERS = frozenset({"username", "email", "phone"})
CHANNELS = frozenset({"email", "phone"})
LOC_MEM = "django.core.mail.backends.locmem.EmailBackend"
SMTP = "django.core.mail.backends.smtp.EmailBackend"
SIGNUP = frozenset({"username", "email", "phone", "password"})


def boolean(environment, suffix, default=False):
    key = f"SMACKEND_AUTH_{suffix}"
    value = environment.get(key, "true" if default else "false").strip().lower()
    if value in {"true", "1", "false", "0"}:
        return value in {"true", "1"}
    raise ImproperlyConfigured(f"{key} must be true or false.")


def tokens(environment, suffix, allowed, default="", *, required_marker=False):
    key = f"SMACKEND_AUTH_{suffix}"
    value = environment.get(key, default).strip()
    result = tuple(item.strip() for item in value.split(",")) if value else ()
    canonical = [item.removesuffix("!") if required_marker else item for item in result]
    if len(canonical) != len(set(canonical)) or any(item not in allowed for item in canonical):
        raise ImproperlyConfigured(f"{key} contains an unknown or repeated field.")
    return result


@dataclass(frozen=True)
class IdentityProfile:
    signup: tuple[str, ...]
    password_login: tuple[str, ...]
    otp: tuple[str, ...]
    email_verification: str
    require_phone: bool
    reset: bool
    allow_signup: bool
    google: bool

    @property
    def fields(self):
        return frozenset(item.removesuffix("!") for item in self.signup)


def read_profile(environment):
    profile = IdentityProfile(
        signup=tokens(environment, "SIGNUP_FIELDS", SIGNUP, "username!,password!", required_marker=True),
        password_login=tokens(environment, "PASSWORD_LOGIN", IDENTIFIERS, "username"),
        otp=tokens(environment, "OTP", CHANNELS),
        email_verification=environment.get("SMACKEND_AUTH_EMAIL_VERIFICATION", "none").strip(),
        require_phone=boolean(environment, "REQUIRE_PHONE_VERIFICATION"),
        reset=boolean(environment, "PASSWORD_RESET"),
        allow_signup=boolean(environment, "ALLOW_SIGNUP", True),
        google=boolean(environment, "GOOGLE"),
    )
    fields = profile.fields
    required = {item[:-1] for item in profile.signup if item.endswith("!")}
    methods = set(profile.password_login) | set(profile.otp)
    if not methods or not methods <= fields:
        raise ImproperlyConfigured("Configure at least one login identifier and include every enabled identifier in SMACKEND_AUTH_SIGNUP_FIELDS.")
    if ("password" in fields) != bool(profile.password_login):
        raise ImproperlyConfigured("Enable password signup and at least one password login identifier together.")
    if profile.email_verification not in {"none", "optional", "mandatory"}:
        raise ImproperlyConfigured("SMACKEND_AUTH_EMAIL_VERIFICATION must be none, optional or mandatory.")
    if (profile.email_verification != "none") != ("email" in fields):
        raise ImproperlyConfigured("Collecting email requires optional or mandatory verification. Otherwise use none.")
    if profile.email_verification == "mandatory" and "email" not in required:
        raise ImproperlyConfigured("Mandatory email verification requires email! at signup.")
    if profile.require_phone and "phone" not in required:
        raise ImproperlyConfigured("Mandatory phone verification requires phone! at signup.")
    if profile.reset and ("email" not in fields or not profile.password_login):
        raise ImproperlyConfigured("Password reset requires email collection and password login.")
    usable_at_signup = {"username"}
    if profile.email_verification == "mandatory":
        usable_at_signup.add("email")
    if profile.require_phone:
        usable_at_signup.add("phone")
    usable = required & usable_at_signup
    if profile.allow_signup and not (
        ("password" in required and usable.intersection(profile.password_login))
        or usable.intersection(profile.otp)
    ):
        raise ImproperlyConfigured("Require a usable login identifier at signup. Email or phone used as the only route into an account must be required and verified.")
    return profile


def configure(settings, environment, *, production):
    if find_spec("allauth") is None or find_spec("argon2") is None:
        raise ImproperlyConfigured("Install the Smackend auth extra before enabling browser accounts.")
    profile = read_profile(environment)
    seconds = environment.get("SMACKEND_AUTH_SESSION_SECONDS", "28800")
    if not re.fullmatch(r"[0-9]{1,5}", seconds) or not 300 <= int(seconds) <= 86400:
        raise ImproperlyConfigured("SMACKEND_AUTH_SESSION_SECONDS must be between 300 and 86400.")
    email_backend = environment.get("SMACKEND_AUTH_EMAIL_BACKEND", LOC_MEM).strip()
    sms_backend = environment.get("SMACKEND_AUTH_SMS_BACKEND", "").strip()
    if production and "email" in profile.fields and email_backend in {
        "", LOC_MEM, "django.core.mail.backends.console.EmailBackend", "django.core.mail.backends.dummy.EmailBackend",
        "django.core.mail.backends.filebased.EmailBackend",
    }:
        raise ImproperlyConfigured("Configure a real email backend for production email flows via SMACKEND_AUTH_EMAIL_BACKEND.")
    if "phone" in profile.fields and not sms_backend:
        raise ImproperlyConfigured("Configure SMACKEND_AUTH_SMS_BACKEND when collecting phone numbers.")
    if production and sms_backend.startswith("smackend.identity.delivery."):
        raise ImproperlyConfigured("Configure a real SMS backend in production.")

    settings["INSTALLED_APPS"] += [
        "allauth", "allauth.account", "allauth.headless", "allauth.usersessions", "smackend.identity",
    ]
    middleware = settings["MIDDLEWARE"]
    middleware.insert(middleware.index("django.contrib.sessions.middleware.SessionMiddleware"),
                      "smackend.identity.middleware.auth_guard")
    middleware += [
        "allauth.account.middleware.AccountMiddleware",
        "smackend.identity.middleware.track_sessions",
    ]
    settings["TEMPLATES"][0].update(APP_DIRS=True)
    settings["TEMPLATES"][0].setdefault("OPTIONS", {}).setdefault("context_processors", []).append(
        "django.template.context_processors.request",
    )
    settings.update(
        SMACKEND_IDENTITY_PROFILE=profile,
        SMACKEND_SMS_BACKEND=sms_backend,
        AUTHENTICATION_BACKENDS=["smackend.identity.adapters.VerifiedIdentifierBackend"],
        SESSION_ENGINE="django.contrib.sessions.backends.db",
        SESSION_COOKIE_AGE=int(seconds),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_SAVE_EVERY_REQUEST=False,
        PASSWORD_HASHERS=[
            "django.contrib.auth.hashers.Argon2PasswordHasher",
            "django.contrib.auth.hashers.PBKDF2PasswordHasher",
            "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
            "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
            "django.contrib.auth.hashers.ScryptPasswordHasher",
        ],
        AUTH_PASSWORD_VALIDATORS=[
            {"NAME": f"django.contrib.auth.password_validation.{name}"}
            for name in ("UserAttributeSimilarityValidator", "MinimumLengthValidator",
                         "CommonPasswordValidator", "NumericPasswordValidator")
        ],
        EMAIL_BACKEND=email_backend,
        EMAIL_TIMEOUT=5,
        ACCOUNT_ADAPTER="smackend.identity.adapters.AccountAdapter",
        HEADLESS_ADAPTER="smackend.identity.adapters.HeadlessAdapter",
        HEADLESS_ONLY=True,
        HEADLESS_CLIENTS=("browser",),
        HEADLESS_SERVE_SPECIFICATION=True,
        HEADLESS_FRONTEND_URLS={
            "account_signup": "/account/",
            "account_confirm_email": "/account/#verify-email={key}",
            "socialaccount_login_error": "/account/?provider=error",
            "socialaccount_connections": "/account/",
            "account_login": "/account/",
        },
        ACCOUNT_LOGIN_METHODS=set(profile.password_login) | set(profile.otp),
        ACCOUNT_SIGNUP_FIELDS=[entry.replace("password", "password1").replace("!", "*") for entry in profile.signup],
        ACCOUNT_EMAIL_VERIFICATION=profile.email_verification,
        ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED=profile.email_verification == "mandatory",
        ACCOUNT_PHONE_VERIFICATION_ENABLED=profile.require_phone,
        ACCOUNT_PREVENT_ENUMERATION="strict",
        ACCOUNT_UNIQUE_EMAIL=True,
        ACCOUNT_LOGIN_BY_CODE_ENABLED=bool(profile.otp),
        ACCOUNT_LOGIN_BY_CODE_SUPPORTS_RESEND=2,
        ACCOUNT_EMAIL_VERIFICATION_SUPPORTS_RESEND=2,
        ACCOUNT_PHONE_VERIFICATION_SUPPORTS_RESEND=2,
        ACCOUNT_PASSWORD_RESET_BY_CODE_ENABLED=True,
        ACCOUNT_LOGIN_ON_PASSWORD_RESET=False,
        ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE=True,
        ACCOUNT_REAUTHENTICATION_REQUIRED=True,
        ACCOUNT_REAUTHENTICATION_METHODS={"password", "code"},
        ACCOUNT_REAUTHENTICATION_TIMEOUT=300,
        ACCOUNT_RATE_LIMITS=False,
        USERSESSIONS_TRACK_ACTIVITY=False,
        ALLAUTH_USER_CODE_FORMAT={"numeric": False, "dashed": True, "length": 8},
        LOGIN_REDIRECT_URL="/account/",
    )
    if email_backend == SMTP:
        port = environment.get("SMACKEND_AUTH_EMAIL_PORT", "587")
        host = environment.get("SMACKEND_AUTH_EMAIL_HOST", "").strip()
        if not host or not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
            raise ImproperlyConfigured("Configure a valid SMTP host and port for Smackend email.")
        settings.update(
            EMAIL_HOST=host,
            EMAIL_PORT=int(port),
            EMAIL_HOST_USER=environment.get("SMACKEND_AUTH_EMAIL_USER", ""),
            EMAIL_HOST_PASSWORD=environment.get("SMACKEND_AUTH_EMAIL_PASSWORD", ""),
            EMAIL_USE_TLS=True,
            DEFAULT_FROM_EMAIL=environment.get("SMACKEND_AUTH_EMAIL_FROM", "webmaster@localhost"),
        )
    if profile.google:
        client_id = environment.get("SMACKEND_AUTH_GOOGLE_CLIENT_ID", "").strip()
        secret = environment.get("SMACKEND_AUTH_GOOGLE_CLIENT_SECRET", "").strip()
        if not client_id or not secret:
            raise ImproperlyConfigured("Google OAuth requires a client ID and secret.")
        settings["INSTALLED_APPS"] += ["allauth.socialaccount", "allauth.socialaccount.providers.google"]
        settings.update(
            SOCIALACCOUNT_ADAPTER="smackend.identity.social.SocialAdapter",
            SOCIALACCOUNT_AUTO_SIGNUP=False,
            SOCIALACCOUNT_EMAIL_AUTHENTICATION=False,
            SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT=False,
            SOCIALACCOUNT_LOGIN_ON_GET=False,
            SOCIALACCOUNT_STORE_TOKENS=False,
            SOCIALACCOUNT_REAUTHENTICATION_REQUIRED=True,
            SOCIALACCOUNT_PROVIDERS={"google": {
                "APPS": [{"client_id": client_id, "secret": secret}],
                "OAUTH_PKCE_ENABLED": True,
            }},
        )
