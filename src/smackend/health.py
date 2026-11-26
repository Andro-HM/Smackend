"""Health routes shared by projects. Each call creates a native Ninja router."""

import logging

from django.db import connection
from django.http import HttpResponse
from ninja import Router, Schema

from .database import DATABASE_ERRORS
from .errors import ErrorBody, error_response

logger = logging.getLogger("smackend.health")


class Health(Schema):
    status: str


class ReadinessError(ErrorBody):
    status: str


def health_router():
    router = Router(tags=["Health"])

    @router.get("/health", response=Health)
    def health(request, response: HttpResponse):
        response["Cache-Control"] = "no-store"
        return {"status": "ok"}

    @router.get("/ready", response={200: Health, 503: ReadinessError})
    def readiness(request, response: HttpResponse):
        response["Cache-Control"] = "no-store"
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except DATABASE_ERRORS as exc:
            logger.warning("Readiness check failed", extra={
                "event": "readiness.failure", "error_type": type(exc).__name__,
                "request": request, "status_code": 503,
            })
            return error_response(request, 503, code="database_unavailable", readiness=True)
        return {"status": "ready"}

    return router
