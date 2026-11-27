"""App-owned synthetic workload mounted on a freshly generated Smackend project."""

import asyncio
import os
from contextlib import nullcontext

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import models, transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Router, Schema

from smackend.identity.permissions import owner_scope, session_auth
from smackend.outbound.client import AsyncClient
from smackend.outbound.django import provider, release_django_connections


TEST_PASSWORD = "Fixture-only!Smackend-phase-four-275"


class Record(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    sequence = models.IntegerField()
    amount = models.PositiveIntegerField(default=1)
    text = models.CharField(max_length=220)

    class Meta:
        app_label = "qualification"
        constraints = [models.UniqueConstraint(fields=("owner", "sequence"), name="qualification_owner_sequence")]


class Entry(Schema):
    id: int
    sequence: int
    amount: int
    text: str


class Change(Schema):
    amount: int


def records(request):
    return owner_scope(Record.objects.all(), request.user)


router = Router(auth=session_auth)


@router.get("/records", response=list[Entry])
def listing(request):
    return list(records(request).order_by("sequence")[:25])


comparison_api = NinjaAPI(urls_namespace="qualification_native_ninja")


@comparison_api.get("/records", response=list[Entry], auth=session_auth)
def ninja_read(request):
    return listing(request)


def django_read(request):
    if session_auth(request) is None:
        return JsonResponse({"detail": "Unauthorized"}, status=401)
    values = [{"id": row.id, "sequence": row.sequence, "amount": row.amount, "text": row.text}
              for row in listing(request)]
    return JsonResponse(values, safe=False, json_dumps_params={"separators": (",", ":")})


@router.get("/total")
def total(request):
    return records(request).aggregate(total=Sum("amount"))


@router.put("/records/{sequence}", response=Entry)
def change(request, sequence: int, body: Change):
    with transaction.atomic():
        row = records(request).select_for_update().get(sequence=sequence)
        row.amount = body.amount
        row.save(update_fields=["amount"])
    return row


@router.post("/records/{sequence}", response={201: Entry})
def create(request, sequence: int, body: Change):
    row = Record.objects.create(owner=request.auth, sequence=sequence, amount=body.amount, text="Temporary fixture")
    return 201, row


@router.delete("/records/{sequence}", response={204: None})
def delete(request, sequence: int):
    row = get_object_or_404(records(request), sequence=sequence)
    row.delete()
    return 204, None


def release_identity(request):
    # Ninja has already resolved the authenticated principal.
    subject = request.auth.pk
    if settings.QUALIFICATION_EARLY_RELEASE:
        release_django_connections()
    return subject


@router.get("/external")
async def external(request):
    await sync_to_async(release_identity)(request)
    pooled = getattr(settings, "QUALIFICATION_OUTBOUND_CLIENT", None)
    if settings.QUALIFICATION_MANAGED:
        from smackend.outbound.asgi import outbound_client

        owned = outbound_client("synthetic")
    else:
        owned = nullcontext(pooled) if pooled else AsyncClient(provider("synthetic"))
    async with owned as client:
        result = await asyncio.gather(*(client.request("GET", f"/wait/{name}") for name in ("a", "b", "c")))
    return {"result": len(result), "connections": [value.json()["loopback_connection"] for value in result]}


def seed(users, rows):
    from django.contrib.auth import get_user_model
    from django.contrib.auth.hashers import make_password
    from django.contrib.sessions.backends.db import SessionStore
    from django.middleware.csrf import _get_new_csrf_string
    from django.test import Client
    from django.core.files.uploadedfile import SimpleUploadedFile

    file_bytes = int(os.getenv("QUALIFICATION_FILE_BYTES", "4096"))
    model = get_user_model()
    encoded = make_password(TEST_PASSWORD)
    model.objects.bulk_create([model(username=f"fixture{index:04d}", password=encoded) for index in range(users)])
    identities = []
    for index, user in enumerate(model.objects.filter(username__startswith="fixture").order_by("username")):
        records = [Record(owner=user, sequence=sequence, text=f"Owned synthetic entry {index} " + "x" * 130)
                   for sequence in range(rows)]
        Record.objects.bulk_create(records)
        session = SessionStore()
        session["_auth_user_id"] = str(user.pk)
        session["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        session["_auth_user_hash"] = user.get_session_auth_hash()
        session.save()
        csrf = _get_new_csrf_string()
        client = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1")
        client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
        client.cookies[settings.CSRF_COOKIE_NAME] = csrf
        prefix = f"Owned text fixture {index:04d}\n".encode()
        uploaded = client.post("/api/files/", {"files": SimpleUploadedFile(
            "fixture.txt", (prefix + b"x" * (file_bytes - len(prefix))), content_type="text/plain",
        )}, HTTP_X_CSRFTOKEN=csrf)
        if uploaded.status_code != 201:
            raise RuntimeError(f"Storage seed failed with HTTP {uploaded.status_code}")
        identities.append({"index": index, "session": session.session_key, "csrf": csrf,
                           "file": uploaded.json()[0]["id"], "file_bytes": file_bytes})
    if ledger_rows := int(os.getenv("QUALIFICATION_LEDGER_ROWS", "0")):
        from django.utils import timezone
        from django.utils.crypto import salted_hmac
        from smackend.ai.models import Invocation
        from uuid import uuid4

        subjects = [salted_hmac("smackend.ai.subject", str(pk), algorithm="sha256").hexdigest()
                    for pk in model.objects.order_by("pk").values_list("pk", flat=True)]
        now = timezone.now()
        for start in range(0, ledger_rows, 1000):
            Invocation.objects.bulk_create([
                Invocation(subject=subjects[index % len(subjects)], request_key=uuid4(), day=now.date(),
                           active_until=now, model_alias="seed", state=Invocation.State.COMPLETE,
                           tokens=10, cost_micros=0)
                for index in range(start, min(start + 1000, ledger_rows))
            ])
    return identities
