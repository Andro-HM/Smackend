from django.core.exceptions import ImproperlyConfigured


def configure(settings, environment, *, production):
    if not settings.get("SMACKEND_AUTH_ENABLED"):
        raise ImproperlyConfigured("Administration requires SMACKEND_AUTH_ENABLED and the auth extra.")
    settings["INSTALLED_APPS"] += [
        "django.contrib.admin.apps.SimpleAdminConfig",
        "django.contrib.messages", "django.contrib.staticfiles", "smackend.administration",
    ]
    settings["MIDDLEWARE"] += ["django.contrib.messages.middleware.MessageMiddleware"]
    settings["TEMPLATES"][0]["OPTIONS"]["context_processors"] += [
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]
    settings.setdefault("STATIC_URL", "/static/")
    settings.setdefault("STATIC_ROOT", settings["BASE_DIR"] / "staticfiles")
