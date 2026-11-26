from django.conf import settings
from django.http import HttpResponse
from django.urls import include, path
from django.views.decorators.http import require_safe
from django.views.generic import TemplateView

from .api import api


@require_safe
def frontend_asset(request, filename, content_type):
    # Only the two fixed files named in urlpatterns can be served here.
    data = (settings.FRONTEND_DIR / filename).read_bytes()
    return HttpResponse(data, content_type=content_type)


urlpatterns = [
    path("", TemplateView.as_view(template_name="index.html", extra_context={
        "accounts_enabled": settings.SMACKEND_AUTH_ENABLED,
        "assist_enabled": (settings.SMACKEND_REALTIME_ENABLED
                           or (settings.SMACKEND_AI_ENABLED and settings.SMACKEND_AI_DEMO_MODEL)),
    })),
    path("assets/styles.css", frontend_asset, {"filename": "styles.css", "content_type": "text/css"}),
    path("assets/app.js", frontend_asset, {"filename": "app.js", "content_type": "text/javascript"}),
    path("api/", api.urls),
]

if settings.SMACKEND_AUTH_ENABLED:
    urlpatterns += [
        path("identity/", include("smackend.identity.urls")),
        path("account/", TemplateView.as_view(template_name="account.html"), name="account_frontend"),
        path("assets/account.js", frontend_asset, {"filename": "account.js", "content_type": "text/javascript"}),
    ]

if settings.SMACKEND_ADMIN_ENABLED:
    from .admin import admin_site

    urlpatterns += [path("admin/", admin_site.urls)]

if settings.SMACKEND_AI_ENABLED or settings.SMACKEND_REALTIME_ENABLED:
    urlpatterns += [
        path("assist/", TemplateView.as_view(template_name="assist.html", extra_context={
            "chat_enabled": settings.SMACKEND_AI_ENABLED and settings.SMACKEND_AI_DEMO_MODEL,
            "realtime_enabled": settings.SMACKEND_REALTIME_ENABLED,
            "voice_enabled": settings.SMACKEND_REALTIME_ENABLED and settings.SMACKEND_REALTIME_VOICE_MODE != "disabled",
        })),
        path("assets/assist.js", frontend_asset, {"filename": "assist.js", "content_type": "text/javascript"}),
    ]
