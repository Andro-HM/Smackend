from django.conf import settings
from ninja import NinjaAPI

from smackend.errors import install_error_handlers
from smackend.health import health_router

api = NinjaAPI(title="__PROJECT_NAME__")
install_error_handlers(api)
api.add_router("", health_router())

if settings.SMACKEND_HTTP_ENABLED:
    from smackend.outbound.django import install_http_errors

    install_http_errors(api)

if settings.SMACKEND_AUTH_ENABLED:
    from smackend.identity.api import identity_router

    api.add_router("/account", identity_router())

if settings.SMACKEND_STORAGE_ENABLED:
    from smackend.media.api import media_router

    api.add_router("/files", media_router())

# Register your own native Ninja routers here using api.add_router().

if settings.SMACKEND_AI_ENABLED and settings.SMACKEND_AI_DEMO_MODEL:
    from smackend.ai.api import ai_router

    api.add_router("/ai", ai_router())
