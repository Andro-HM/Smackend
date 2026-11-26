"""Atomic admission and durable, conservative billing reservations."""

from dataclasses import dataclass
from datetime import timedelta

from django.db import DatabaseError, connection, transaction
from django.db.models import Count, PositiveBigIntegerField, Sum
from django.db.models.functions import Greatest
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .contracts import AIError
from .models import Invocation


@dataclass(frozen=True)
class Quota:
    requests: int = 100
    concurrent: int = 3
    tokens: int = 250_000
    cost_micros: int | None = None

    def __post_init__(self):
        for key in ("requests", "concurrent", "tokens", "cost_micros"):
            value = getattr(self, key)
            if key == "cost_micros" and value is None:
                continue
            if type(value) is not int or not 1 <= value <= 10**15:
                raise ValueError("AI quotas must be positive bounded integers.")


def reserve(subject, key, model, reserved_tokens, cost, lease_seconds, *, user_quota, app_quota):
    subject_hash = salted_hmac("smackend.ai.subject", str(subject), algorithm="sha256").hexdigest()
    try:
        with transaction.atomic(durable=True):
            # One short application-wide lock guards admission across all workers.
            # External calls only start after this transaction and checkout end.
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [6289300457026568])
            now = timezone.now()
            if Invocation.objects.filter(subject=subject_hash, request_key=key).exists():
                raise AIError("duplicate_request", "This AI request key has already been used.", status=409)
            for rows, quota in ((Invocation.objects.all(), app_quota),
                                (Invocation.objects.filter(subject=subject_hash), user_quota)):
                total = rows.filter(day=now.date()).aggregate(tokens=Sum("tokens", default=0),
                                                              cost=Sum("cost_micros", default=0), requests=Count("pk"))
                active = rows.filter(state=Invocation.State.RESERVED, active_until__gt=now).count()
                if (active >= quota.concurrent or total["requests"] >= quota.requests
                        or total["tokens"] + reserved_tokens > quota.tokens
                        or (quota.cost_micros is not None and total["cost"] + cost > quota.cost_micros)):
                    raise AIError("ai_quota", "The AI request exceeds an active usage quota.", status=429)
            return Invocation.objects.create(
                subject=subject_hash, request_key=key, day=now.date(),
                active_until=now + timedelta(seconds=lease_seconds),
                model_alias=model, tokens=reserved_tokens, cost_micros=cost,
            ).pk
    except DatabaseError:
        raise AIError("ai_admission_unavailable", "AI admission is temporarily unavailable.", status=503) from None
    finally:
        if not connection.in_atomic_block:
            connection.close()


def settle(reservation, state, model, usage=None, cost=None):
    fields = {"state": state, "model_alias": model}
    if usage is not None:
        measured = usage.input_tokens + usage.output_tokens
        tokens_charged = (Greatest("tokens", measured, output_field=PositiveBigIntegerField())
                          if state == Invocation.State.UNKNOWN else measured)
        fields.update(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, tokens=tokens_charged)
    if cost is not None:
        fields["cost_micros"] = cost
    if state == Invocation.State.REJECTED:
        fields.update(tokens=0, cost_micros=0)
    try:
        Invocation.objects.filter(pk=reservation, state=Invocation.State.RESERVED).update(**fields)
    finally:
        if not connection.in_atomic_block:
            connection.close()
