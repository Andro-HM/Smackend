import os
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from locust import HttpUser, between, events, task

AUTH = "/_allauth/browser/v1/auth/"
NOTES = "/api/notes/"
PASSWORD = "Synthetic!purple-bridge-419"

audit_path = os.getenv("PHASE0_AUDIT_OUTPUT")
if audit_path:
    samples = []
    started = perf_counter()

    @events.request.add_listener
    def sample_request(name, response_time, response, exception, **kwargs):
        headers = response.headers if response is not None else {}
        samples.append([
            round(perf_counter() - started, 4), name, round(response_time, 3),
            not bool(exception), json.loads(headers.get("X-Phase0-Profile", "{}")),
        ])

    @events.quitting.add_listener
    def write_samples(**kwargs):
        Path(audit_path).write_text(json.dumps(samples), encoding="utf-8")


class NotesUser(HttpUser):
    wait_time = between(0.25, 0.75)

    def write(self, method, route, **options):
        return self.client.request(
            method, route, headers={"X-CSRFToken": self.client.cookies.get("csrftoken", "")}, **options
        )

    def on_start(self):
        self.client.get("/", name="bootstrap")
        prefix = os.getenv("PHASE0_LOAD_PREFIX", "load")
        username = prefix + uuid4().hex[:16]
        response = self.write("POST", AUTH + "signup", json={"username": username, "password": PASSWORD}, name="signup")
        if response.status_code != 200:
            from locust.exception import StopUser

            raise StopUser()

    @task(6)
    def list_notes(self):
        self.client.get(NOTES, name="list")

    @task(2)
    def create_delete(self):
        response = self.write("POST", NOTES, json={"title": "Synthetic"}, name="create")
        if response.status_code == 201:
            self.write("DELETE", NOTES + str(response.json()["id"]), name="delete")

    @task(1)
    def provider(self):
        if os.getenv("PHASE0_AUDIT_CRUD_ONLY") == "1":
            self.list_notes()
        else:
            self.client.get("/api/provider", name="provider")

    @task(1)
    def session(self):
        self.client.get(AUTH + "session", name="session")


class DriverOnlyUser(HttpUser):
    abstract = os.getenv("PHASE0_DRIVER_HEADROOM") != "1"

    @task
    def sample(self):
        self.client.get("/sample")
