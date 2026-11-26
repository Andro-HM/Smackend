"""Run assembled module contracts with real PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from unittest.mock import patch

import django

from identity_probe import PASSWORD, browser, database, send


BASE = "/api/files"


def register(name):
    client = browser()
    send(client, "auth/signup", {"username": name, "password": PASSWORD}, expect=200)
    return client


def upload(client, files, **data):
    return client.post(BASE + "/", {"files": files, **data}, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)


def run_storage():
    from django.conf import settings
    from django.core.files.storage import storages
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.db import connection
    from django.test import Client
    from django.test.utils import override_settings
    from PIL import Image
    from dataclasses import replace

    from smackend.media.models import Asset, Blob
    from smackend.media.service import cleanup

    owner = register("file_owner")
    outsider = register("file_outsider")
    anonymous = Client(HTTP_HOST="127.0.0.1")
    image = BytesIO()
    Image.new("RGB", (5, 5), "blue").save(image, "PNG")
    contents = image.getvalue()

    def payload(name="../../evil.exe", data=b"hello"):
        return SimpleUploadedFile(name, data)

    result = upload(owner, [payload(), payload(data=contents)])
    assert result.status_code == 201, result.content
    private, picture = result.json()
    assert private["name"] == "evil.txt" and picture["content_type"] == "image/png"
    asset_id = private["id"]
    path = f"{BASE}/{asset_id}"
    assert outsider.get(path).status_code == 404
    assert anonymous.get(path + "/download").status_code == 404
    assert send(outsider, path, method="DELETE", expect=404)
    assert owner.post(BASE + "/", {"files": [payload()]}).status_code == 403
    answer = owner.get(path + "/download")
    assert answer.status_code == 200 and answer["X-Content-Type-Options"] == "nosniff"
    assert answer["Content-Disposition"].startswith("attachment")
    assert b"".join(answer.streaming_content) == b"hello"
    answer.close()

    result = owner.post(path + "/replace", {"file": payload("new.txt", b"replaced"), "public": "true"},
                        HTTP_X_CSRFTOKEN=owner.cookies["csrftoken"].value)
    assert result.status_code == 200 and result.json()["id"] == asset_id, result.content
    public = anonymous.get(path + "/download")
    assert b"".join(public.streaming_content) == b"replaced"
    public.close()
    backend = storages["smackend_media"]
    old = Blob.objects.get(state=Blob.State.GARBAGE)
    assert backend.exists(old.storage_name)
    with patch.object(backend, "delete", side_effect=OSError("backend failed")):
        assert cleanup(grace=0) == (0, 1)
    assert cleanup(grace=0) == (1, 0)
    assert not backend.exists(old.storage_name)

    assert upload(owner, [payload(data=b"\x00unsafe")]).status_code == 415
    unsupported = BytesIO()
    Image.new("RGB", (5, 5), "red").save(unsupported, "TIFF")
    with patch.object(Image, "open", wraps=Image.open) as opened:
        assert upload(owner, [payload(name="fixture.png", data=unsupported.getvalue())]).status_code == 415
        assert opened.call_count == 1
        assert set(opened.call_args.kwargs["formats"]) == {"PNG", "JPEG", "WEBP"}
    with override_settings(SMACKEND_STORAGE_POLICY=replace(settings.SMACKEND_STORAGE_POLICY, kinds=frozenset({"text"}))):
        assert upload(owner, [payload(name="fixture.png", data=contents)]).status_code == 415
    owner_count = Asset.objects.count()
    with override_settings(SMACKEND_STORAGE_POLICY=replace(settings.SMACKEND_STORAGE_POLICY, file_bytes=32)):
        too_big = upload(owner, [payload(), payload(data=b"x" * 40)])
        assert too_big.status_code == 413, too_big.content
    assert Asset.objects.count() == owner_count

    save = backend.save
    calls = []

    def fail_second(name, file):
        calls.append(name)
        if len(calls) == 2:
            raise OSError("backend failed")
        return save(name, file)

    with patch.object(backend, "save", side_effect=fail_second):
        assert upload(owner, [payload(), payload()]).status_code == 503
    assert backend.exists(calls[0])
    assert Asset.objects.count() == owner_count
    assert Blob.objects.filter(state=Blob.State.GARBAGE).count() == 2
    assert cleanup(grace=0) == (2, 0)
    assert not backend.exists(calls[0])
    send(owner, path, method="DELETE", expect=204)
    assert anonymous.get(path).status_code == 404
    assert cleanup(grace=0) == (1, 0)

    if settings.STORAGES["smackend_media"]["BACKEND"].endswith("FileSystemStorage"):
        # A fresh storage instance reads from the same persistent root.
        from django.core.files.storage import FileSystemStorage

        from_disk = FileSystemStorage(location=backend.location)
        blob = Asset.objects.get(pk=picture["id"]).blob
        with from_disk.open(blob.storage_name, "rb") as stream:
            assert stream.read() == contents

        rivals = [Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1") for _ in range(2)]
        for rival in rivals:
            rival.cookies = owner.cookies.copy()
        barrier = Barrier(2)

        def competing(client):
            try:
                barrier.wait(timeout=10)
                return upload(client, [payload("same.txt")]).status_code
            finally:
                connection.close()

        with override_settings(SMACKEND_STORAGE_POLICY=replace(settings.SMACKEND_STORAGE_POLICY, quota_files=2)):
            with ThreadPoolExecutor(2) as pool:
                statuses = sorted(pool.map(competing, rivals))
        assert statuses == [201, 409], statuses


def run_admin():
    from django.apps import apps
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group, Permission
    from django.core.management import call_command

    from smackend.administration.models import AuditEvent

    accounts = get_user_model().objects
    normal = register("admin_normal")
    staff = register("admin_staff")
    operator = register("admin_operator")
    operator_row = accounts.get(username="admin_operator")
    staff_row = accounts.get(username="admin_staff")
    target = accounts.get(username="admin_normal")
    assert operator.get("/admin/").status_code == 302
    assert normal.post("/admin/login/", {"username": "admin_operator", "password": PASSWORD},
                       HTTP_X_CSRFTOKEN=normal.cookies["csrftoken"].value).status_code == 405
    call_command("grantadministrator", str(operator_row.pk))
    assert operator.get("/api/account/profile").status_code == 401
    send(operator, "auth/login", {"username": "admin_operator", "password": PASSWORD}, expect=200)
    assert operator.get("/admin/").status_code == 302
    send(operator, "auth/reauthenticate", {"password": PASSWORD}, expect=200)
    assert operator.get("/admin/").status_code == 200
    assert normal.get("/admin/").status_code == 302

    staff_row.is_staff = True
    staff_row.save(update_fields=["is_staff"])
    staff_row.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="view_user"))
    send(staff, "auth/reauthenticate", {"password": PASSWORD}, expect=200)
    users_url = "/admin/accounts/user/"
    csrf = lambda client: {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    assert staff.get(users_url + "?q=admin_normal").status_code == 200
    assert staff.get("/admin/auth/group/").status_code == 403
    assert staff.get("/admin/administration/auditevent/").status_code == 403
    assert staff.post(users_url + f"{staff_row.pk}/change/", {"is_superuser": "true"}, **csrf(staff)).status_code == 403
    assert staff.post(users_url, {"action": "deactivate", "_selected_action": str(target.pk)}, **csrf(staff)).status_code == 200
    target.refresh_from_db()
    assert target.is_active
    assert not accounts.get(pk=staff_row.pk).is_superuser

    role = operator.post("/admin/auth/group/add/", {"name": "publishers"}, **csrf(operator))
    assert role.status_code == 302, role.status_code
    assert Group.objects.filter(name="publishers").exists()
    selected = {"_selected_action": str(target.pk)}
    assert operator.post(users_url, selected | {"action": "deactivate"}).status_code == 403
    for action in ("revoke", "deactivate", "activate"):
        reply = operator.post(users_url, selected | {"action": action}, **csrf(operator))
        assert reply.status_code == 302, (action, reply.status_code)
    assert normal.get("/api/account/profile").status_code == 401
    assert accounts.get(pk=target.pk).is_active
    assert set(AuditEvent.objects.values_list("action", flat=True)) == {
        "administrator_granted", "role_saved", "sessions_revoked", "deactivated", "activated",
    }
    assert operator.get("/admin/administration/auditevent/").status_code == 200
    assert operator.get("/admin/media/asset/").status_code == (200 if apps.is_installed("smackend.media") else 404)
    if apps.is_installed("smackend.media"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        file = upload(operator, [SimpleUploadedFile("sample.txt", b"sample")])
        assert file.status_code == 201
        key = file.json()[0]["id"]
        retired = operator.post("/admin/media/asset/", {"action": "retire", "_selected_action": key},
                                **csrf(operator))
        assert retired.status_code == 302
        assert operator.get(BASE + "/" + key).status_code == 404
        assert AuditEvent.objects.filter(action="file_retired", object_id=key).exists()


def add_consumer_app(*, with_routes=True):
    app = Path("consumer")
    app.mkdir()
    (app / "__init__.py").touch()
    (app / "models.py").write_text('''
from django.db import models

class Entry(models.Model):
    title = models.CharField(max_length=50)
''', encoding="utf-8")
    (app / "api.py").write_text('''
from asgiref.sync import sync_to_async
from django.db import connection
import httpx
from ninja import Router
from smackend.outbound.client import AsyncClient, Client
from smackend.outbound.django import provider, release_django_connections
from .models import Entry

router = Router()

def read_count():
    result = Entry.objects.count()
    release_django_connections()
    assert connection.connection is None
    return result

def simulated(request):
    assert request.headers["X-Api-Key"] == "synthetic-key"
    if request.url.path == "/failure":
        raise httpx.ConnectError("private simulated provider failure")
    return httpx.Response(200, json={"upstream_count": 7})

@router.get("/count", response=int)
def count(request):
    current = read_count()
    with Client(provider("catalog"), transport=httpx.MockTransport(simulated)) as client:
        reply = client.request("GET", "/count").json()
    return current + reply["upstream_count"]

@router.get("/async-count", response=int)
async def async_count(request):
    current = await sync_to_async(read_count)()
    async with AsyncClient(provider("catalog"), transport=httpx.MockTransport(simulated)) as client:
        reply = (await client.request("GET", "/count")).json()
    return current + reply["upstream_count"]

@router.get("/failure")
def failure(request):
    read_count()
    with Client(provider("catalog"), transport=httpx.MockTransport(simulated)) as client:
        client.request("GET", "/failure")
''', encoding="utf-8")
    with Path("config/settings.py").open("a", encoding="utf-8") as settings_file:
        settings_file.write('\nINSTALLED_APPS += ["consumer"]\n')
    if with_routes:
        attach_consumer_router()


def attach_consumer_router():
    with Path("config/api.py").open("a", encoding="utf-8") as api:
        api.write('\nfrom consumer.api import router as consumer_router\napi.add_router("/consumer", consumer_router)\n')


def configure_simulated_provider():
    os.environ.update(
        SMACKEND_HTTP_PROVIDERS='{"catalog":{"base_url":"https://catalog.example","attempts":1,'
                                       '"credentials":{"X-Api-Key":{"env":"CATALOG_TOKEN"}}}}',
        CATALOG_TOKEN="synthetic-key",
    )


def run_http(*, existing_entry=False):
    from django.db import transaction
    from django.test import Client

    from consumer.models import Entry
    from smackend.outbound.django import release_django_connections

    web = Client(HTTP_HOST="127.0.0.1")
    if not existing_entry:
        Entry.objects.create(title="consumer row")
    for path in ("/api/consumer/count", "/api/consumer/async-count"):
        result = web.get(path)
        assert result.status_code == 200 and result.json() == 8, (path, result.status_code, result.content)
    failed = web.get("/api/consumer/failure")
    assert failed.status_code == 502 and b"private simulated" not in failed.content
    assert failed.json()["code"] == "provider_unavailable"
    assert web.get("/api/ready").status_code == 200 and Entry.objects.count() == 1
    with transaction.atomic():
        try:
            release_django_connections()
        except RuntimeError:
            pass
        else:
            raise AssertionError("The helper allowed an external call before committing a transaction")


def run_plain():
    from django.apps import apps
    from django.conf import settings
    from django.test import Client

    web = Client(HTTP_HOST="127.0.0.1")
    assert web.get("/api/ready").status_code == 200
    for route in ("/api/account/capabilities", "/api/files/", "/admin/"):
        assert web.get(route).status_code == 404
    assert not apps.is_installed("allauth") and not apps.is_installed("smackend.media")
    assert not hasattr(settings, "SMACKEND_OUTBOUND_PROVIDERS")


def upgrade_project():
    from consumer.models import Entry

    Entry.objects.create(title="existing consumer data")
    original = register("existing_owner")
    assert original.get("/api/account/profile").status_code == 200
    attach_consumer_router()
    with Path("config/admin.py").open("a", encoding="utf-8") as admin:
        admin.write("\nfrom consumer.models import Entry\nadmin_site.register(Entry)\n")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "upgraded"],
        env={**os.environ, "SMACKEND_STORAGE_ENABLED": "true", "SMACKEND_ADMIN_ENABLED": "true",
             "SMACKEND_HTTP_ENABLED": "true", "SMACKEND_PROBE_SESSION": original.cookies["sessionid"].value},
        capture_output=True, text=True, timeout=85,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def run_upgraded():
    from django.core.management import call_command
    from django.test import Client
    from django.urls import reverse
    from consumer.models import Entry

    call_command("check")
    call_command("migrate", interactive=False, verbosity=0)
    call_command("makemigrations", "--check", "--dry-run", interactive=False)
    web = Client(HTTP_HOST="127.0.0.1")
    web.cookies["sessionid"] = os.environ["SMACKEND_PROBE_SESSION"]
    profile = web.get("/api/account/profile")
    assert profile.status_code == 200 and profile.json()["username"] == "existing_owner"
    assert Entry.objects.get().title == "existing consumer data"
    assert reverse("smackend_admin:consumer_entry_changelist") == "/admin/consumer/entry/"
    run_storage()
    run_admin()
    run_http(existing_entry=True)
    verification = subprocess.run(
        [sys.executable, "-c", "from smackend.cli import main; raise SystemExit(main(['verify']))"],
        capture_output=True, text=True, timeout=50,
    )
    assert verification.returncode == 0, verification.stdout + verification.stderr


def main(scenario):
    sys.path.insert(0, str(Path.cwd()))
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    simulator = nullcontext()
    if scenario == "http":
        add_consumer_app()
        configure_simulated_provider()
    if scenario == "upgrade":
        add_consumer_app(with_routes=False)
    if scenario == "plain":
        os.environ.update(SMACKEND_HTTP_PROVIDERS="invalid", SMACKEND_STORAGE_BACKEND="s3")
    if scenario == "upgraded":
        configure_simulated_provider()
        django.setup()
        run_upgraded()
        return
    if scenario == "s3":
        import boto3
        from moto import mock_aws

        os.environ.update(SMACKEND_STORAGE_BACKEND="s3", SMACKEND_STORAGE_BUCKET="smackend-test-media",
                          SMACKEND_STORAGE_S3_REGION="us-east-1")
        simulator = mock_aws()
    with simulator, database():
        if scenario == "s3":
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="smackend-test-media")
        django.setup()
        from django.core.management import call_command

        call_command("check")
        if scenario in {"http", "upgrade"}:
            call_command("makemigrations", "consumer", interactive=False, verbosity=0)
        call_command("migrate", interactive=False, verbosity=0)
        call_command("makemigrations", "--check", "--dry-run", interactive=False)
        if scenario in {"storage", "s3"}:
            run_storage()
        elif scenario == "http":
            run_http()
        elif scenario == "plain":
            run_plain()
        elif scenario == "upgrade":
            upgrade_project()
        else:
            run_admin()


if __name__ == "__main__":
    main(sys.argv[1])
