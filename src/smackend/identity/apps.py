from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, register
from django.utils.module_loading import import_string

from .rate_limit import DEFAULT_LIMITS


@register()
def identity_settings(app_configs, **kwargs):
    problems = []
    overrides = getattr(settings, "SMACKEND_AUTH_LIMITS", {})
    if not isinstance(overrides, dict) or any(
        key not in DEFAULT_LIMITS or type(value) is not int or value <= 0 for key, value in overrides.items()
    ):
        problems.append(Error("SMACKEND_AUTH_LIMITS requires known names and positive integer quotas.",
                              id="smackend_identity.E001"))
    if "phone" in settings.SMACKEND_IDENTITY_PROFILE.fields:
        try:
            sms = import_string(settings.SMACKEND_SMS_BACKEND)
            if not callable(sms):
                raise TypeError
        except (ImportError, TypeError, ValueError):
            problems.append(Error("SMACKEND_AUTH_SMS_BACKEND must import a callable.", id="smackend_identity.E002"))
    if settings.SESSION_ENGINE != "django.contrib.sessions.backends.db":
        problems.append(Error("Smackend identity requires Django's database session engine.", id="smackend_identity.E003"))
    return problems


class IdentityConfig(AppConfig):
    name = "smackend.identity"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals  # noqa: F401
