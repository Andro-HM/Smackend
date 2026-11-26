"""Exercise the assembled browser identity module in a disposable PostgreSQL database."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Barrier
from time import sleep
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from uuid import uuid4
from unittest.mock import patch

import django
import psycopg
from psycopg import sql

BASE = "/identity/browser/v1/"
PASSWORD = "Session-harbour-471-dog!"
NEXT_PASSWORD = "Meadow-landing-731-elm!"
EMAIL = "person@example.test"
PHONE = "+14155550131"
CODE_PATTERN = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b", re.IGNORECASE)
PROFILES = {
    "username": ("username!,password!", "username", "", "none", False),
    "email-password": ("email!,password!", "email", "", "mandatory", False),
    "phone-password": ("phone!,password!", "phone", "", "none", True),
    "email-otp": ("email!", "", "email", "mandatory", False),
    "phone-otp": ("phone!", "", "phone", "none", True),
    "mixed": ("username!,email!,phone!,password!", "username,email,phone", "email,phone", "mandatory", True),
    "optional": ("username!,email,phone,password!", "username", "email,phone", "optional", False),
    "challenges": ("email!", "", "email", "mandatory", False),
    "authorization": ("username!,password!", "username", "", "none", False),
    "phone-race": ("phone!", "", "phone", "none", True),
    "database-failure": ("username!,password!", "username", "", "none", False),
    "google": ("username!,email!,password!", "username,email", "", "mandatory", False),
    "workers": ("username!,password!", "username", "", "none", False),
}


@contextmanager
def database():
    dsn = os.environ["SMACKEND_SMOKE_DSN"]
    config = psycopg.conninfo.conninfo_to_dict(dsn)
    assert config["host"] in {"127.0.0.1", "localhost", "::1"}
    assert config.get("hostaddr", config["host"]) in {"127.0.0.1", "localhost", "::1"}
    db_name = f"smackend_identity_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
        os.environ.update(
            POSTGRES_DB=db_name, POSTGRES_HOST=admin.info.host, POSTGRES_PORT=str(admin.info.port),
            POSTGRES_USER=admin.info.user, POSTGRES_PASSWORD=config["password"],
        )
        try:
            yield
        finally:
            from django.db import connections

            for connection in connections.all():
                connection.close()
                connection.close_pool()
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db_name)))


def browser():
    from django.test import Client

    client = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1")
    assert client.get(BASE + "auth/session").status_code == 401
    assert "csrftoken" in client.cookies
    return client


def send(client, route, data=None, *, method="POST", expect=None, csrf=True):
    path = route if route.startswith("/") else BASE + route
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value} if csrf else {}
    response = client.generic(method, path, json.dumps(data or {}), content_type="application/json", **headers)
    if expect is not None:
        codes = (expect,) if isinstance(expect, int) else expect
        assert response.status_code in codes, (route, response.status_code, response.content[:650])
    return response


def fresh_code(channel):
    from django.core import mail
    from smackend.identity.delivery import sms_outbox

    body = mail.outbox[-1].body if channel == "email" else sms_outbox[-1][1]
    match = CODE_PATTERN.search(body)
    assert match, f"No test code in the {channel} message"
    return match[0]


def login(client, kind="username", value="sample_user", password=PASSWORD):
    return send(client, "auth/login", {kind: value, "password": password}, expect=200)


def assert_in(client):
    assert client.get("/api/account/profile").status_code == 200


def assert_out(client):
    assert client.get("/api/account/profile").status_code == 401


def enroll(scenario):
    signup, *_ = PROFILES[scenario]
    client = browser()
    credentials = {
        key: value for key, value in {"username": "Sample_User", "email": EMAIL.upper(), "phone": PHONE,
                                      "password": PASSWORD}.items() if key + "!" in signup
    }
    send(client, "auth/signup", credentials, csrf=False, expect=403)
    pending = scenario not in {"username", "workers"}
    send(client, "auth/signup", credentials, expect=401 if pending else 200)
    if "phone!" in signup:
        assert_out(client)
        send(client, "auth/phone/verify", {"code": fresh_code("phone")},
             expect=401 if "email!" in signup else (200, 401))
    if "email!" in signup:
        assert_out(client)
        send(client, "auth/email/verify", {"key": fresh_code("email")}, expect=(200, 401))
    return client


def check_profile(scenario):
    from django.contrib.auth import get_user_model

    signup_client = enroll(scenario)
    user = get_user_model().objects.get()
    assert user.password.startswith("argon2$") if "password!" in PROFILES[scenario][0] else not user.has_usable_password()
    client = browser()
    if scenario in {"username", "email-password", "phone-password"}:
        kind, value = {
            "username": ("username", "SAMPLE_USER"),
            "email-password": ("email", EMAIL.upper()),
            "phone-password": ("phone", PHONE),
        }[scenario]
        send(client, "auth/login", {kind: value, "password": "incorrect"}, expect=400)
        login(client, kind, value)
    else:
        channel, destination = ("email", EMAIL) if scenario == "email-otp" else ("phone", PHONE)
        send(client, "auth/login", {channel: destination, "password": PASSWORD}, expect=400)
        send(client, "auth/code/request", {channel: destination}, expect=401)
        code = fresh_code(channel)
        original_key = client.cookies["sessionid"].value
        send(client, "auth/code/confirm", {"code": code}, expect=200)
        assert client.cookies["sessionid"].value != original_key
        assert not client.session.exists(original_key)
        send(client, "auth/code/confirm", {"code": code}, expect=409)
    assert_in(client)
    send(client, "auth/session", method="DELETE", expect=401)
    assert_out(client)
    if scenario != "username":
        assert signup_client.get("/identity/openapi.json").status_code == 200


def check_mixed():
    from allauth.account.models import EmailAddress
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.models import Session
    from django.core import mail
    from django.db import IntegrityError, transaction
    from django.utils import timezone
    from smackend.identity.models import PhoneAddress, RateBucket

    enrolled = enroll("mixed")
    try:
        with transaction.atomic():
            competing_user = get_user_model().objects.create(username="competing")
            EmailAddress.objects.create(user=competing_user, email=EMAIL.upper(), verified=True)
    except IntegrityError:
        pass
    else:
        raise AssertionError("The database accepted a second case-variant verified email")
    for identifier, value in (("username", "sample_user"), ("email", EMAIL), ("phone", PHONE)):
        client = browser()
        login(client, identifier, value)
        assert_in(client)
    send(browser(), "auth/login", {"username": EMAIL, "password": PASSWORD}, expect=400)
    assert get_user_model().objects.get().username == "sample_user"

    # A fresh login does not substitute for explicit reauthentication.
    send(client, "account/phone", {"phone": "+14155550132"}, expect=401)
    send(client, "auth/reauthenticate", {"password": PASSWORD}, expect=200)
    send(client, "account/phone", {"phone": "+14155550132"}, expect=202)
    send(client, "auth/phone/verify", {"code": fresh_code("phone")}, expect=(200, 401))
    assert PhoneAddress.objects.get(verified=True).number == "+14155550132"
    assert_out(enrolled)
    send(client, "/api/account/profile", {"first_name": "A", "last_name": "B", "is_superuser": True},
         method="PATCH", expect=200)
    assert not get_user_model().objects.get().is_superuser

    lost = browser()
    send(lost, "auth/password/request", {"email": EMAIL}, expect=401)
    code = fresh_code("email")
    send(lost, "auth/password/reset", {"key": code, "password": NEXT_PASSWORD}, expect=401)
    assert_out(client)
    send(lost, "auth/password/reset", {"key": code, "password": PASSWORD}, expect=409)
    login(client, "email", EMAIL, NEXT_PASSWORD)

    # Revocation accepts only the current user's own tracked session IDs.
    other = browser()
    login(other, "email", EMAIL, NEXT_PASSWORD)
    reply = client.get(BASE + "auth/sessions")
    sessions = [item["id"] for item in reply.json()["data"] if not item["is_current"]]
    assert sessions
    send(client, "auth/sessions", {"sessions": sessions}, method="DELETE", expect=200)
    assert_out(other)

    send(client, "account/password/change", {"current_password": NEXT_PASSWORD, "new_password": PASSWORD}, expect=401)
    assert_out(client)
    login(client)
    key = client.cookies["sessionid"].value
    Session.objects.filter(session_key=key).update(expire_date=timezone.now())
    assert_out(client)
    RateBucket.objects.all().delete()
    client = browser()
    login(client)
    send(client, "auth/reauthenticate", {"password": PASSWORD}, expect=200)
    replacement = "replacement@example.test"
    send(client, "account/email", {"email": replacement}, expect=200)
    send(client, "auth/email/verify", {"key": fresh_code("email")}, expect=200)
    send(client, "account/email", {"email": replacement, "primary": True}, method="PATCH", expect=200)
    send(client, "account/email", {"email": EMAIL}, method="DELETE", expect=200)
    send(browser(), "auth/login", {"email": EMAIL, "password": PASSWORD}, expect=400)
    login(browser(), "email", replacement)
    send(client, "/api/account/username", {"username": "changed_user"}, method="PUT", expect=200)
    send(client, "/api/account/deactivate", expect=204)
    assert_out(client)
    assert not get_user_model().objects.get().is_active

    with patch("django.core.mail.backends.locmem.EmailBackend.send_messages", side_effect=OSError("private-provider-error")):
        response = send(browser(), "auth/password/request", {"email": "unknown@example.test"}, expect=401)
        assert b"private-provider-error" not in response.content
    assert mail.outbox


def check_google():
    from allauth.socialaccount.models import SocialAccount
    from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter

    owner = enroll("google")
    if owner.get("/api/account/profile").status_code != 200:
        login(owner, "email", EMAIL)
    send(owner, "auth/reauthenticate", {"password": PASSWORD}, expect=200)
    redirect_path = BASE + "auth/provider/redirect"
    callback = "/identity/google/login/callback/"
    identity = {"id": "synthetic-google-subject", "email": EMAIL, "verified_email": True}

    def begin(client, process):
        response = client.post(redirect_path, {
            "provider": "google", "process": process, "callback_url": "/account/",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        })
        assert response.status_code == 302, response.content[:500]
        query = parse_qs(urlsplit(response["Location"]).query)
        assert query["code_challenge_method"] == ["S256"]
        return query["state"][0]

    def finish(client, state):
        return client.get(callback + "?" + urlencode({"state": state, "code": "synthetic-code"}))

    stranger = browser()
    with patch.object(GoogleOAuth2Adapter, "get_access_token_data", return_value={"access_token": "synthetic"}), \
         patch.object(GoogleOAuth2Adapter, "_fetch_user_info", return_value=identity) as fetch:
        stale = finish(stranger, "wrong-state")
        assert stale.status_code == 302 and "provider=error" in stale["Location"]
        fetch.assert_not_called()
        assert "provider=error" in finish(stranger, begin(stranger, "login"))["Location"]
        assert_out(stranger)
        assert not SocialAccount.objects.exists()
        state = begin(owner, "connect")
        assert "provider=error" in finish(stranger, state)["Location"]
        assert finish(owner, state).status_code == 302
        assert SocialAccount.objects.get().uid == "synthetic-google-subject"
        assert "provider=error" in finish(owner, state)["Location"]
        assert finish(stranger, begin(stranger, "login")).status_code == 302
        assert_in(stranger)


def check_optional():
    from allauth.account.models import EmailAddress
    from django.core import mail
    from smackend.identity.models import PhoneAddress

    client = browser()
    send(client, "auth/signup", {"username": "sample_user", "password": PASSWORD, "email": EMAIL, "phone": PHONE},
         expect=200)
    assert_in(client)
    assert not EmailAddress.objects.get().verified
    assert not PhoneAddress.objects.get().verified
    key_pattern = re.compile(r"#verify-email=([^\s]+)")
    key = key_pattern.search(mail.outbox[-1].body)
    assert key
    send(client, "auth/email/verify", {"key": unquote(key[1])}, expect=200)
    assert EmailAddress.objects.get().verified
    assert client.get(BASE + "account/phone").status_code == 200
    send(client, "auth/reauthenticate", {"password": PASSWORD}, expect=200)
    send(client, "account/phone", {"phone": PHONE}, expect=202)
    send(client, "auth/phone/verify", {"code": fresh_code("phone")}, expect=200)
    assert PhoneAddress.objects.get().verified
    send(client, "auth/login", {"email": EMAIL, "password": PASSWORD}, expect=400)


def check_challenges():
    from django.db import connection
    from django.test.utils import override_settings
    from smackend.identity.models import RateBucket

    enroll("email-otp")
    client = browser()
    send(client, "auth/code/request", {"email": EMAIL}, expect=401)
    code = fresh_code("email")
    for _ in range(3):
        send(client, "auth/code/confirm", {"code": "WRNG-CODE"}, expect=(400, 401))
    send(client, "auth/code/confirm", {"code": code}, expect=409)
    assert_out(client)

    send(client, "auth/code/request", {"email": EMAIL}, expect=401)
    code = fresh_code("email")
    with override_settings(ACCOUNT_LOGIN_BY_CODE_TIMEOUT=-1):
        send(client, "auth/code/confirm", {"code": code}, expect=409)
    assert_out(client)

    send(client, "auth/code/request", {"email": EMAIL}, expect=401)
    original = fresh_code("email")
    send(client, "auth/code/resend", expect=200)
    send(client, "auth/code/resend", expect=200)
    send(client, "auth/code/resend", expect=409)
    send(client, "auth/code/confirm", {"code": original}, expect=(400, 401))
    RateBucket.objects.all().delete()
    known = send(browser(), "auth/code/request", {"email": EMAIL}, expect=401).json()
    unknown = send(browser(), "auth/code/request", {"email": "unknown@example.test"}, expect=401).json()
    assert known == unknown
    client = browser()
    send(client, "auth/code/request", {"email": EMAIL}, expect=401)
    code = fresh_code("email")
    barrier = Barrier(2)

    def submit(_):
        twin = browser()
        twin.cookies = deepcopy(client.cookies)
        try:
            barrier.wait(timeout=10)
            return send(twin, "auth/code/confirm", {"code": code}).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(2) as pool:
        statuses = sorted(pool.map(submit, range(2)))
    assert statuses == [200, 409], statuses


def check_phone_race():
    from django.db import connection
    from smackend.identity.models import PhoneAddress

    contenders = []
    for _ in range(2):
        client = browser()
        send(client, "auth/signup", {"phone": PHONE}, expect=401)
        contenders.append((client, fresh_code("phone")))
    barrier = Barrier(2)

    def verify(contender):
        client, code = contender
        try:
            barrier.wait(timeout=10)
            return send(client, "auth/phone/verify", {"code": code}).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(verify, contenders))
    assert statuses.count(200) == 1 and set(statuses) <= {200, 400, 409}, statuses
    assert PhoneAddress.objects.filter(number=PHONE, verified=True).count() == 1

    with patch("smackend.identity.delivery.memory_sms", side_effect=TimeoutError("synthetic-provider-detail")):
        response = send(browser(), "auth/code/request", {"phone": PHONE}, expect=401)
        assert b"synthetic-provider-detail" not in response.content


def check_workers():
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection
    from django.test.utils import override_settings
    from smackend.identity.models import RateBucket
    from smackend.identity.rate_limit import spend

    def require_time_in_window(seconds):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 60 - mod(floor(extract(epoch FROM statement_timestamp()))::bigint, 60)")
            remaining = cursor.fetchone()[0]
        if remaining < seconds:
            sleep(remaining + 1)

    concurrent = 8
    barrier = Barrier(concurrent)
    names = ("Race_User", "RACE_USER") * (concurrent // 2)

    def submit(name):
        client = browser()
        try:
            barrier.wait(timeout=25)
            return send(client, "auth/signup", {"username": name, "password": PASSWORD}).status_code
        finally:
            close_old_connections()
            connection.close()

    with ThreadPoolExecutor(concurrent) as pool:
        statuses = list(pool.map(submit, names))
    assert statuses.count(200) == 1 and set(statuses) <= {200, 400, 409}, statuses
    assert get_user_model().objects.filter(username="race_user").count() == 1

    subprocess_code = (
        "import django; django.setup(); "
        "from smackend.identity.rate_limit import spend; "
        "print(sum(spend('password','cross-worker-check') == 0 for _ in range(8)))"
    )
    require_time_in_window(10)
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda _: subprocess.check_output([sys.executable, "-c", subprocess_code], text=True), range(3)))
    assert sum(map(int, results)) == 10, results
    RateBucket.objects.create(key="old", bucket=0, attempts=1)
    from django.core.management import call_command

    call_command("clearauthlimits")
    assert not RateBucket.objects.filter(key="old").exists()
    with override_settings(SMACKEND_AUTH_LIMITS={"global": 1}):
        # Seed this assertion's bucket explicitly. The concurrent registrations
        # above may have belonged to the previous minute.
        require_time_in_window(5)
        spend("global", "")
        response = send(browser(), "auth/login", {"username": "race_user", "password": PASSWORD}, expect=429)
        assert int(response["Retry-After"]) > 0


def add_consumer_feature():
    app = Path("resource_app")
    app.mkdir()
    (app / "__init__.py").touch()
    (app / "models.py").write_text('''
from django.conf import settings
from django.db import models

class Record(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    label = models.CharField(max_length=40)
    value = models.PositiveIntegerField()
''', encoding="utf-8")
    (app / "api.py").write_text('''
from django.db.models import Sum
from ninja import Router, Schema
from smackend.identity.permissions import (
    authorized_object, authorized_scope, owner_scope, require_admin, require_permission, require_staff, session_auth,
)
from .models import Record

router = Router(auth=session_auth)

def policy(queryset, actor, action):
    if action in {"update", "delete", "export"}:
        require_permission(actor, "resource_app.change_record")
    return owner_scope(queryset, actor)

def scoped(request, action):
    return authorized_scope(Record.objects.all(), request.auth, action, policy)

class Change(Schema):
    label: str

@router.get("/", response=list[str])
def records(request):
    return list(scoped(request, "list").values_list("label", flat=True))

@router.get("/total", response=int)
def total(request):
    return scoped(request, "aggregate").aggregate(total=Sum("value", default=0))["total"]

@router.get("/export", response=list[str])
def export(request):
    return list(scoped(request, "export").values_list("label", flat=True))

@router.get("/staff", response=bool)
def staff(request):
    require_staff(request.auth)
    return True

@router.get("/admin", response=bool)
def admin(request):
    require_admin(request.auth)
    return True

@router.get("/{key}", response=str)
def get_record(request, key: int):
    return authorized_object(Record.objects.all(), request.auth, "detail", policy, pk=key).label

@router.patch("/{key}", response=str)
def update_record(request, key: int, data: Change):
    record = authorized_object(Record.objects.all(), request.auth, "update", policy, pk=key)
    record.label = data.label
    record.save(update_fields=["label"])
    return record.label

@router.delete("/{key}", response={204: None})
def delete_record(request, key: int):
    authorized_object(Record.objects.all(), request.auth, "delete", policy, pk=key).delete()
    return 204, None
''', encoding="utf-8")
    with Path("config/settings.py").open("a", encoding="utf-8") as settings_file:
        settings_file.write('\nINSTALLED_APPS += ["resource_app"]\n')
    with Path("config/api.py").open("a", encoding="utf-8") as api_file:
        api_file.write('\nfrom resource_app.api import router as resource_router\napi.add_router("/records", resource_router)\n')


def check_authorization():
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group, Permission
    from resource_app.models import Record

    client = browser()
    other = browser()
    for browser_client, username in ((client, "first_member"), (other, "second_member")):
        send(browser_client, "auth/signup", {"username": username, "password": PASSWORD}, expect=200)
    owner = get_user_model().objects.get(username="first_member")
    other_owner = get_user_model().objects.get(username="second_member")
    editors = Group.objects.create(name="review-editors")
    editors.permissions.add(Permission.objects.get(content_type__app_label="resource_app", codename="change_record"))
    owner.groups.add(editors)
    owner.is_staff = True
    owner.save(update_fields=["is_staff"])
    own_row = Record.objects.create(owner=owner, label="Visible", value=7)
    other_row = Record.objects.create(owner=other_owner, label="Hidden", value=19)
    assert client.get("/api/records/").json() == ["Visible"]
    assert client.get("/api/records/total").json() == 7
    assert client.get("/api/records/export").json() == ["Visible"]
    assert client.get("/api/records/staff").status_code == 200
    assert client.get("/api/records/admin").status_code == 403
    assert other.get("/api/records/staff").status_code == 403
    assert other.get("/api/records/export").status_code == 403
    assert client.get(f"/api/records/{other_row.pk}").status_code == 404
    assert browser().get("/api/records/").status_code == 401
    send(client, f"/api/records/{other_row.pk}", {"label": "fail"}, method="PATCH", expect=404)
    send(client, f"/api/records/{other_row.pk}", method="DELETE", expect=404)
    send(other, f"/api/records/{other_row.pk}", {"label": "fail"}, method="PATCH", expect=403)
    send(client, f"/api/records/{own_row.pk}", {"label": "Changed"}, csrf=False, method="PATCH", expect=403)
    send(client, f"/api/records/{own_row.pk}", {"label": "Changed", "owner_id": other_owner.pk},
         method="PATCH", expect=200)
    own_row.refresh_from_db()
    assert own_row.owner_id == owner.pk
    send(client, f"/api/records/{own_row.pk}", method="DELETE", expect=204)
    assert other.get("/api/records/").json() == ["Hidden"]
    send(client, "auth/session", method="DELETE", expect=401)
    login(client, "username", owner.username)
    assert_in(client)


def check_database_failure():
    from django.db import connection

    enroll("username")
    client = browser()
    saved_name = connection.settings_dict["NAME"]
    saved_timeout = connection.settings_dict["OPTIONS"]["pool"]["timeout"]
    connection.close()
    connection.close_pool()
    connection.settings_dict["NAME"] = f"absent_{uuid4().hex}"
    connection.settings_dict["OPTIONS"]["pool"]["timeout"] = 1
    try:
        response = send(client, "auth/login", {"username": "sample_user", "password": PASSWORD}, expect=503)
        assert response.json()["code"] == "identity_unavailable"
        assert b"absent_" not in response.content
    finally:
        connection.close()
        connection.close_pool()
        connection.settings_dict["NAME"] = saved_name
        connection.settings_dict["OPTIONS"]["pool"]["timeout"] = saved_timeout
    login(client)
    assert_in(client)


def main(scenario):
    fields, password, otp, email, phone = PROFILES[scenario]
    sys.path.insert(0, str(Path.cwd()))
    os.environ.update(
        DJANGO_SETTINGS_MODULE="config.settings",
        SMACKEND_AUTH_SIGNUP_FIELDS=fields, SMACKEND_AUTH_PASSWORD_LOGIN=password, SMACKEND_AUTH_OTP=otp,
        SMACKEND_AUTH_EMAIL_VERIFICATION=email, SMACKEND_AUTH_REQUIRE_PHONE_VERIFICATION=str(phone).lower(),
        SMACKEND_AUTH_SMS_BACKEND="smackend.identity.delivery.memory_sms",
        SMACKEND_AUTH_PASSWORD_RESET=str(scenario == "mixed").lower(),
        SMACKEND_AUTH_GOOGLE=str(scenario == "google").lower(),
        SMACKEND_AUTH_GOOGLE_CLIENT_ID="synthetic-id", SMACKEND_AUTH_GOOGLE_CLIENT_SECRET="synthetic-secret",
    )
    if scenario == "authorization":
        add_consumer_feature()
    with database():
        django.setup()
        from django.core.management import call_command

        call_command("check")
        if scenario == "authorization":
            call_command("makemigrations", "resource_app", interactive=False, verbosity=0)
        call_command("migrate", interactive=False, verbosity=0)
        call_command("makemigrations", "--check", "--dry-run", interactive=False)
        if scenario == "mixed":
            check_mixed()
        elif scenario == "google":
            check_google()
        elif scenario == "workers":
            check_workers()
        elif scenario == "optional":
            check_optional()
        elif scenario == "challenges":
            check_challenges()
        elif scenario == "phone-race":
            check_phone_race()
        elif scenario == "authorization":
            check_authorization()
        elif scenario == "database-failure":
            check_database_failure()
        else:
            check_profile(scenario)
        print(f"Verified identity scenario {scenario} against PostgreSQL.")


if __name__ == "__main__":
    main(sys.argv[1])
