import os

from asgiref.sync import iscoroutinefunction
from django.conf import settings
from django.db import IntegrityError, OperationalError, connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.decorators import sync_and_async_middleware
from django.utils.deprecation import MiddlewareMixin
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_safe
from psycopg_pool import PoolTimeout

DATABASE_ERRORS = (OperationalError, PoolTimeout)


def unavailable():
    return JsonResponse({"code": "database_unavailable"}, status=503)


class LabResponses(MiddlewareMixin):
    """Sanitize expected DB failures and prevent caching phase-0 responses."""

    def process_exception(self, request, exception):
        if isinstance(exception, DATABASE_ERRORS):
            return unavailable()
        if isinstance(exception, IntegrityError) and request.path == "/_allauth/browser/v1/auth/signup":
            constraint = getattr(getattr(exception.__cause__, "diag", None), "constraint_name", None)
            if constraint == "accounts_user_username_key":
                return JsonResponse({"code": "username_taken"}, status=409)

    def process_response(self, request, response):
        return mark_lab_response(response)


def mark_lab_response(response):
    response["Cache-Control"] = "no-store"
    # Makes multi-process observations attributable in this loopback experiment.
    response["X-Lab-Worker"] = str(os.getpid())
    return response


@sync_and_async_middleware
def lab_responses_inline(get_response):
    """Keep the existing exception handler and set response headers on the current thread."""
    if iscoroutinefunction(get_response):
        async def middleware(request):
            return mark_lab_response(await get_response(request))
    else:
        def middleware(request):
            return mark_lab_response(get_response(request))

    middleware.process_exception = LabResponses(get_response).process_exception
    return middleware


def csrf_failure(request, reason=""):
    return JsonResponse({"code": "csrf_rejected"}, status=403)


@require_safe
@ensure_csrf_cookie
def page(request, template):
    return render(request, template)


@require_safe
def asset(request, filename):
    # These are tiny, fixed files named by the URLconf.
    content_type = "text/css" if filename.endswith(".css") else "text/javascript"
    return HttpResponse((settings.BASE_DIR / "pages" / filename).read_bytes(), content_type=content_type)


@require_safe
def live(request):
    return JsonResponse({"status": "alive"})


@require_safe
def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DATABASE_ERRORS:
        return unavailable()
    return JsonResponse({"status": "ready"})
