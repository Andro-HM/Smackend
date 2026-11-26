from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.db import connection, connections
from django.test import Client
from django.utils import timezone

from notes.models import Note

pytestmark = pytest.mark.django_db
AUTH = "/_allauth/browser/v1/auth/"
PASSWORD = "Synthetic!purple-bridge-419"
NOTES = "/api/notes/"


def browser():
    client = Client(enforce_csrf_checks=True)
    assert client.get("/").status_code == 200
    return client


def send(client, method, path, body=None, **headers):
    if body is not None:
        headers["content_type"] = "application/json"
        headers["data"] = body
    return getattr(client, method)(path, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value, **headers)


def signup(client, username):
    return send(client, "post", AUTH + "signup", {"username": username, "password": PASSWORD})


@pytest.fixture
def signed_in():
    client = browser()
    response = signup(client, "note_owner")
    assert response.status_code == 200, response.json()
    return client


def test_auth_lifecycle(settings):
    assert connection.vendor == "postgresql"
    client = browser()
    assert signup(client, "Alice_One").status_code == 200
    user = get_user_model().objects.get(username="alice_one")
    assert user.password.startswith("argon2$") and user.check_password(PASSWORD)
    cookie = settings.SESSION_COOKIE_NAME
    first_key = client.cookies[cookie].value
    assert client.cookies[cookie]["httponly"]
    assert client.cookies[cookie]["samesite"] == "Lax"
    assert Session.objects.filter(session_key=first_key).exists()
    assert client.get("/notes/").status_code == 200
    assert client.get(AUTH + "session").json()["meta"]["is_authenticated"]
    assert send(client, "delete", AUTH + "session").status_code == 401
    assert not Session.objects.filter(session_key=first_key).exists()
    assert client.get(NOTES).status_code == 401
    wrong = send(client, "post", AUTH + "login", {"username": "alice_one", "password": "wrong"})
    assert wrong.status_code in {400, 401}, wrong.content
    assert client.get(NOTES).status_code == 401
    assert send(client, "post", AUTH + "login", {"username": "alice_one", "password": PASSWORD}).status_code == 200
    assert client.cookies[cookie].value != first_key
    replay = Client()
    replay.cookies[cookie] = first_key
    assert replay.get(NOTES).status_code == 401


def test_frontend_assets_use_browser_mime_types():
    client = browser()
    assert client.get("/assets/login.js")["Content-Type"] == "text/javascript"
    assert client.get("/assets/lab.css")["Content-Type"] == "text/css"


@pytest.mark.parametrize("change", ["expiration", "disable", "password"])
def test_session_invalidation(signed_in, settings, change):
    if change == "expiration":
        Session.objects.filter(session_key=signed_in.cookies[settings.SESSION_COOKIE_NAME].value).update(
            expire_date=timezone.now()
        )
    else:
        user = get_user_model().objects.get(username="note_owner")
        if change == "disable":
            user.is_active = False
        else:
            user.set_password("Changed!purple-bridge-933")
        user.save()
    assert signed_in.get(NOTES).status_code == 401


def test_duplicate_and_invalid_registration(signed_in):
    client = browser()
    assert signup(client, "NOTE_OWNER").status_code == 400
    assert signup(client, "bad.user").status_code == 400
    assert get_user_model().objects.count() == 1


def test_csrf_and_anonymous_writes(signed_in):
    anonymous = browser()
    for action in ("signup", "login"):
        denied = anonymous.post(AUTH + action, {"username": "new_user", "password": PASSWORD}, content_type="application/json")
        assert denied.status_code == 403
    assert anonymous.get(NOTES).status_code == 401
    assert send(anonymous, "post", NOTES, {"title": "Anonymous"}).status_code == 401
    assert signed_in.post(NOTES, {"title": "No token"}, content_type="application/json").status_code == 403
    assert signed_in.delete(AUTH + "session").status_code == 403
    assert send(signed_in, "post", NOTES, {"title": "Foreign origin"}, HTTP_ORIGIN="https://example.invalid").status_code == 403
    assert signed_in.get(NOTES).status_code == 200
    assert Note.objects.count() == 0


def test_note_crud_and_ownership(signed_in):
    owner = signed_in
    outsider = browser()
    assert signup(outsider, "other_owner").status_code == 200
    created = send(owner, "post", NOTES, {"title": "First", "body": "Private"})
    assert created.status_code == 201
    record = created.json()
    detail = NOTES + str(record["id"])
    assert owner.get(NOTES).json() == [record]
    assert owner.get(detail).json() == record
    assert outsider.get(NOTES).json() == []
    assert outsider.get(detail).status_code == 404
    assert send(outsider, "put", detail, {"title": "Changed"}).status_code == 404
    assert send(outsider, "delete", detail).status_code == 404
    assert send(outsider, "post", NOTES, {"title": "Forged", "owner": 1}).status_code == 422
    assert send(owner, "put", detail, {"title": "Updated"}).json()["title"] == "Updated"
    assert send(owner, "delete", detail).status_code == 204
    assert owner.get(detail).status_code == 404


def test_habits_coexist_with_notes(signed_in):
    owner = signed_in
    other = browser()
    assert signup(other, "habit_other").status_code == 200
    assert send(owner, "post", NOTES, {"title": "Retained"}).status_code == 201
    result = send(owner, "post", "/api/habits/", {"label": "Walk"})
    assert result.status_code == 201
    detail = f"/api/habits/{result.json()['id']}/repeat"
    assert send(other, "post", detail, {}).status_code == 404
    assert send(owner, "post", detail, {}).status_code == 204
    assert owner.get("/api/habits/").json()[0]["repetitions"] == 1
    assert other.get("/api/habits/").json() == []
    assert owner.get(AUTH + "session").status_code == 200
    assert owner.get(NOTES).json()[0]["title"] == "Retained"


@pytest.mark.django_db(transaction=True)
def test_concurrent_registration_and_note_creates():
    gate = Barrier(10, timeout=15)

    def register(index):
        try:
            client = browser()
            gate.wait()
            return signup(client, "race_user" if index % 2 else "RACE_USER").status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(register, range(10)))
    assert results.count(200) == 1, results
    assert set(results) <= {200, 400, 409}, results
    assert get_user_model().objects.filter(username="race_user").count() == 1

    gate = Barrier(10, timeout=15)
    clients = [browser() for _ in range(10)]
    for client in clients:
        assert send(client, "post", AUTH + "login", {"username": "race_user", "password": PASSWORD}).status_code == 200
    connections.close_all()

    def create(index):
        try:
            gate.wait()
            return send(clients[index], "post", NOTES, {"title": f"Concurrent {index}"}).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert list(pool.map(create, range(10))) == [201] * 10
    assert Note.objects.count() == 10
