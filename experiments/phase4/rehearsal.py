"""Exercise three independent consumer projects using an installed wheel or sdist."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg import sql


COMMON = """
from django.db import models, transaction
from django.conf import settings
from django.shortcuts import get_object_or_404
from ninja import Router
from smackend.identity.permissions import owner_scope, session_auth
from smackend.errors import APIError

router = Router(auth=session_auth)

class BusinessRecord(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=120)
    units = models.PositiveIntegerField(default=1)

@router.get("/")
def own_records(request):
    rows = owner_scope(BusinessRecord.objects.all(), request.user)
    return list(rows.order_by("pk").values("id", "title", "units"))
"""

SHOP = """
from uuid import UUID

class Reservation(models.Model):
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    item = models.ForeignKey(BusinessRecord, on_delete=models.PROTECT)
    key = models.UUIDField(unique=True)

@router.post("/stock/{item_id}/reserve/{key}")
def reserve(request, item_id: int, key: UUID):
    with transaction.atomic():
        row = get_object_or_404(BusinessRecord.objects.select_for_update(), pk=item_id)
        existing = Reservation.objects.filter(key=key, buyer=request.user, item=row).first()
        if existing:
            return {"reservation": existing.pk}
        if not row.units:
            raise APIError(409, "out_of_stock", "Stock is unavailable.")
        created = Reservation.objects.create(buyer=request.user, item=row, key=key)
        row.units -= 1
        row.save(update_fields=["units"])
    return {"reservation": created.pk}
"""

DOCUMENT = """
from uuid import uuid4
from asgiref.sync import sync_to_async
from smackend.ai.configuration import configured_service
from smackend.ai.contracts import Message, Part, Prompt
from smackend.media.models import Asset
from smackend.media.service import open_file
from smackend.outbound.django import release_django_connections

class DocumentSource(models.Model):
    document = models.OneToOneField(BusinessRecord, on_delete=models.CASCADE)
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)

def owned_document(request, pk):
    row = get_object_or_404(owner_scope(BusinessRecord.objects.all(), request.user), pk=pk)
    source = DocumentSource.objects.select_related("asset__blob").filter(document=row, asset__owner=request.user).first()
    if source is not None:
        with open_file(source.asset) as uploaded:
            title = uploaded.read(8192).decode("utf-8")
        return row.owner_id, title
    release_django_connections()
    return row.owner_id, row.title

@router.post("/document/{pk}/summary")
async def summary(request, pk: int):
    actor, text = await sync_to_async(owned_document)(request, pk)
    prompt = Prompt((Message("user", (Part("text", text),)),), max_tokens=40)
    result = await configured_service().execute("offline", "text", prompt, subject=actor,
                                               request_key=uuid4(), input_reserve=200)
    return {"summary": result.text}
"""

EXPENSE = """
from django.db.models import Sum

@router.get("/total")
def total(request):
    return owner_scope(BusinessRecord.objects.all(), request.user).aggregate(total=Sum("units"))
"""

EXTENSIONS = {
    "shop": """
class ReturnRequest(models.Model):
    reservation = models.OneToOneField(Reservation, on_delete=models.CASCADE)

@router.post("/return/{pk}")
def return_stock(request, pk: int):
    reservation = get_object_or_404(Reservation, pk=pk, buyer=request.user)
    item, _ = ReturnRequest.objects.get_or_create(reservation=reservation)
    return {"id": item.pk}
""",
    "document": """
class Review(models.Model):
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    record = models.ForeignKey(BusinessRecord, on_delete=models.CASCADE)

@router.get("/review/{pk}")
def review(request, pk: int):
    row = get_object_or_404(Review.objects.select_related("record"), pk=pk, reviewer=request.user)
    return {"title": row.record.title}
""",
    "expense": """
class Budget(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="budgets")
    members = models.ManyToManyField(settings.AUTH_USER_MODEL)
    ceiling = models.PositiveIntegerField()

@router.get("/budget/{pk}")
def budget(request, pk: int):
    row = get_object_or_404(Budget, pk=pk, members=request.user)
    return {"ceiling": row.ceiling}
""",
}

TESTS = """
from uuid import uuid4
from django.contrib.auth import get_user_model
from django.middleware.csrf import _get_new_csrf_string
from django.test import Client, TransactionTestCase
from .models import BusinessRecord

class ConsumerTests(TransactionTestCase):
    def setUp(self):
        model = get_user_model()
        self.owner = model.objects.create_user(username="owneralpha")
        self.other = model.objects.create_user(username="ownerbeta")
        self.record = BusinessRecord.objects.create(owner=self.owner, title="Private synthetic fixture", units=1)
        BusinessRecord.objects.create(owner=self.other, title="Other private fixture", units=8)
        self.browser = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1")
        self.browser.force_login(self.owner)
        self.token = _get_new_csrf_string()
        self.browser.cookies["csrftoken"] = self.token

    def test_private_listing_and_session(self):
        result = self.browser.get("/api/business/")
        self.assertEqual(result.status_code, 200)
        self.assertEqual([row["title"] for row in result.json()], ["Private synthetic fixture"])
        self.assertEqual(self.browser.get("/api/account/profile").json()["username"], "owneralpha")
        self.browser.logout()
        self.assertEqual(self.browser.get("/api/business/").status_code, 401)

    def post(self, suffix):
        return self.browser.post("/api/business/" + suffix, HTTP_X_CSRFTOKEN=self.token)
"""

FEATURE_TESTS = {
    "shop": """
    def test_idempotent_reservation(self):
        path = f"stock/{self.record.pk}/reserve/{uuid4()}"
        self.assertEqual(self.browser.post("/api/business/" + path).status_code, 403)
        first = self.post(path)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.post(path).json(), first.json())
        self.assertEqual(self.post(f"stock/{self.record.pk}/reserve/{uuid4()}").status_code, 409)

    def test_two_buyers_cannot_reserve_last_unit(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connections
        barrier = Barrier(2)
        def checkout(buyer):
            client = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1")
            client.force_login(buyer)
            token = _get_new_csrf_string()
            client.cookies["csrftoken"] = token
            try:
                barrier.wait(timeout=10)
                return client.post(f"/api/business/stock/{self.record.pk}/reserve/{uuid4()}",
                                   HTTP_X_CSRFTOKEN=token).status_code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(checkout, [self.owner, self.other])), [200, 409])
        self.record.refresh_from_db()
        self.assertEqual(self.record.units, 0)
""",
    "document": """
    def test_owned_summary(self):
        from .models import DocumentSource
        from django.core.files.uploadedfile import SimpleUploadedFile
        stored = self.browser.post("/api/files/", {"files": SimpleUploadedFile("page.txt", b"Synthetic page")},
                                   HTTP_X_CSRFTOKEN=self.token)
        self.assertEqual(stored.status_code, 201)
        DocumentSource.objects.create(document=self.record, asset_id=stored.json()[0]["id"])
        self.assertEqual(self.post(f"document/{self.record.pk}/summary").json(),
                         {"summary": "Offline response: Synthetic page"})
        self.browser.force_login(self.other)
        self.assertEqual(self.post(f"document/{self.record.pk}/summary").status_code, 404)
""",
    "expense": """
    def test_owner_total(self):
        self.assertEqual(self.browser.get("/api/business/total").json(), {"total": 1})
""",
}

EXTENSION_TESTS = {
    "shop": """
    def test_return_ownership(self):
        key = self.post(f"stock/{self.record.pk}/reserve/{uuid4()}").json()["reservation"]
        self.assertEqual(self.post(f"return/{key}").status_code, 200)
        self.browser.force_login(self.other)
        self.assertEqual(self.post(f"return/{key}").status_code, 404)
""",
    "document": """
    def test_review_assignment(self):
        from .models import Review
        row = Review.objects.create(record=self.record, reviewer=self.other)
        self.assertEqual(self.browser.get(f"/api/business/review/{row.pk}").status_code, 404)
        self.browser.force_login(self.other)
        self.assertEqual(self.browser.get(f"/api/business/review/{row.pk}").json(), {"title": self.record.title})
""",
    "expense": """
    def test_budget_membership(self):
        from .models import Budget
        row = Budget.objects.create(owner=self.other, ceiling=250)
        self.assertEqual(self.browser.get(f"/api/business/budget/{row.pk}").status_code, 404)
        row.members.add(self.owner)
        self.assertEqual(self.browser.get(f"/api/business/budget/{row.pk}").json(), {"ceiling": 250})
""",
}


@contextmanager
def database():
    dsn = os.environ["SMACKEND_SMOKE_DSN"]
    connection_params = psycopg.conninfo.conninfo_to_dict(dsn)
    loopback = {"127.0.0.1", "::1", "localhost"}
    if (connection_params["host"] not in loopback
            or connection_params.get("hostaddr", connection_params["host"]) not in loopback):
        raise ValueError("Use a disposable local PostgreSQL cluster.")
    name = "smackend_rehearsal_" + uuid4().hex
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("SMACKEND_", "POSTGRES_", "PG", "DJANGO_")) and key != "PYTHONPATH"}
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        environment.update(SMACKEND_SMOKE_DSN=dsn, POSTGRES_HOST=db.info.host, POSTGRES_PORT=str(db.info.port),
                           POSTGRES_USER=db.info.user, POSTGRES_PASSWORD=connection_params["password"],
                           POSTGRES_DB=name)
    try:
        yield environment
    finally:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as cleanup:
            cleanup.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def run(folder, env, *args, expected=True):
    result = subprocess.run([sys.executable, *args], cwd=folder, env=env, text=True,
                            check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=140)
    if (result.returncode == 0) is not expected:
        raise RuntimeError(result.stdout)
    return result.stdout.strip()


def rehearse(parent, scenario, options, feature):
    tick = monotonic()
    cli = ("-c", "from smackend.cli import main; raise SystemExit(main())")
    with database() as env:
        run(parent, env, *cli, "init", scenario, *options)
        project = parent / scenario
        app = project / "business"
        app.mkdir()
        (app / "__init__.py").touch()
        settings = project / "config/settings.py"
        with settings.open("a", encoding="utf-8") as target:
            target.write('\nINSTALLED_APPS += ["business"]\n')
        with (project / "config/api.py").open("a", encoding="utf-8") as target:
            target.write('\nfrom business.models import router as business_router\napi.add_router("/business", business_router)\n')
        models = app / "models.py"
        tests = app / "tests.py"
        models.write_text(COMMON + feature, encoding="utf-8")
        tests.write_text(TESTS + FEATURE_TESTS[scenario], encoding="utf-8")
        for command in (("makemigrations", "business"), ("migrate", "--noinput")):
            run(project, env, "manage.py", *command)
        original = run(project, env, *cli, "verify")
        first = monotonic() - tick
        models.write_text(COMMON + feature + EXTENSIONS[scenario], encoding="utf-8")
        tests.write_text(TESTS + FEATURE_TESTS[scenario] + EXTENSION_TESTS[scenario], encoding="utf-8")
        drift = run(project, env, *cli, "verify", expected=False)
        assert "FAIL Model migration drift" in drift
        for command in (("makemigrations", "business"), ("migrate", "--noinput")):
            run(project, env, "manage.py", *command)
        extended = run(project, env, *cli, "verify")
        second = monotonic() - tick - first
        if scenario == "expense":
            contents = models.read_text(encoding="utf-8")
            models.write_text(contents.replace("rows = owner_scope(BusinessRecord.objects.all(), request.user)",
                                               "rows = BusinessRecord.objects.all()"), encoding="utf-8")
            try:
                owner_failure = run(project, env, *cli, "verify", expected=False)
                assert "FAIL Application and core tests" in owner_failure
            finally:
                models.write_text(contents, encoding="utf-8")
            original_settings = settings.read_text(encoding="utf-8")
            try:
                settings.write_text(original_settings + '\nMIDDLEWARE.remove("django.contrib.sessions.middleware.SessionMiddleware")\n',
                                    encoding="utf-8")
                session_failure = run(project, env, *cli, "verify", expected=False)
                assert "FAIL " in session_failure
            finally:
                settings.write_text(original_settings, encoding="utf-8")
            run(project, env, *cli, "verify")
            failure = {"owner_scope": owner_failure, "session_middleware": session_failure}
        else:
            failure = "Negative ownership control ran in the expense project."
        return {"app": scenario, "initial_seconds": round(first, 2), "extension_seconds": round(second, 2),
                "initial_verify": original, "migration_drift": drift, "extension_verify": extended,
                "negative_control": failure}


if __name__ == "__main__":
    scratch = Path(sys.argv[1]).resolve()
    scratch.mkdir(parents=True, exist_ok=False)
    data = [
        rehearse(scratch, "shop", ("--storage", "--admin"), SHOP),
        rehearse(scratch, "document", ("--ai", "--realtime", "--storage"), DOCUMENT),
        rehearse(scratch, "expense", ("--auth",), EXPENSE),
    ]
    print(json.dumps(data, indent=2))
