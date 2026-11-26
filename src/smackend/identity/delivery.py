"""Delivery is an external side effect. It runs after any enclosing DB transaction."""

import logging

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.utils.module_loading import import_string

from .rate_limit import spend

logger = logging.getLogger("smackend.identity")
sms_outbox = []


class DeliveryError(Exception):
    """Provider integrations can raise this for unavailable or rejected delivery."""


def send_after_commit(channel, destination, deliver):
    def send():
        try:
            if spend("send_global", "") or spend("send_recipient", f"{channel}:{destination}"):
                logger.warning("Identity delivery budget exhausted", extra={"event": "identity.delivery_limited"})
                return
        except DatabaseError:
            logger.error("Identity delivery budget unavailable", extra={"event": "identity.delivery_failed"})
            return
        # No database mutation follows this callback. Free the checkout before external I/O.
        connection.close()
        try:
            deliver()
        except Exception as exc:
            logger.error("Identity provider delivery failed", extra={
                "event": "identity.delivery_failed", "error_type": type(exc).__name__,
            })

    transaction.on_commit(send)


def send_sms(destination, text):
    backend = import_string(settings.SMACKEND_SMS_BACKEND)
    send_after_commit("sms", destination, lambda: backend(destination, text))


def memory_sms(destination, text):
    """Development and tests can inspect this in the sending Python process."""
    sms_outbox.append((destination, text))
