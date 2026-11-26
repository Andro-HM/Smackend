"""Module dependencies and settings activation for the generated Django project."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    name: str
    help: str
    configurator: str
    extra: str | None = None
    requires: tuple[str, ...] = ()

    @property
    def setting(self):
        return f"SMACKEND_{self.name.upper()}_ENABLED"


MODULES = (
    Module("auth", "enable browser accounts", "smackend.identity.configuration.configure", "auth"),
    Module("storage", "enable storage and accounts", "smackend.media.configuration.configure", "storage", ("auth",)),
    Module("admin", "enable Django administration and accounts", "smackend.administration.configuration.configure",
           requires=("auth",)),
    Module("http", "enable named outbound HTTP providers", "smackend.outbound.django.configure", "http"),
    Module("ai", "enable authenticated AI examples and adapters", "smackend.ai.configuration.configure", "ai",
           ("auth", "http")),
    Module("realtime", "enable authenticated WebSockets and optional voice", "smackend.realtime.configuration.configure",
           "realtime", ("auth",)),
)


def scaffold_modules(selected):
    enabled = set(selected)
    for module in reversed(MODULES):
        if module.name in enabled:
            enabled.update(module.requires)
    return enabled


def configure_settings(settings, environment, *, boolean, production):
    from django.core.exceptions import ImproperlyConfigured
    from django.utils.module_loading import import_string

    for module in MODULES:
        settings[module.setting] = boolean(environment, module.setting)
    enabled = {module.name for module in MODULES if settings[module.setting]}
    for module in MODULES:
        if module.name not in enabled:
            continue
        missing = set(module.requires) - enabled
        if missing:
            required = ", ".join(item.setting for item in MODULES if item.name in missing)
            raise ImproperlyConfigured(f"{module.setting} requires {required}. Enable the dependency and install its extra.")
        import_string(module.configurator)(settings, environment, production=production)
