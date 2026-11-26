from contextlib import aclosing
import asyncio
import os
from pathlib import Path
import json
import subprocess
import sys
from uuid import uuid4

import django

from identity_probe import database
from modules_probe import register


async def exercise_ai():
    from asgiref.sync import sync_to_async
    from django.test import AsyncClient
    from django.test.utils import override_settings

    from smackend.ai.contracts import AIError, Message, Part, Prompt, Tool, Usage
    from smackend.ai.ledger import Quota, reserve, settle
    from smackend.ai.mock import MockAdapter
    from smackend.ai.models import Invocation
    from smackend.ai.service import Model, Service, Tariff

    class Failing(MockAdapter):
        def __init__(self, safe):
            self.safe = safe
            self.calls = 0

        async def call(self, *args, **kwargs):
            self.calls += 1
            raise AIError("offline_failure", "Simulated provider failure.", safe_to_retry=self.safe)

    class Waiting(MockAdapter):
        def __init__(self):
            self.started = asyncio.Event()
            self.closed = asyncio.Event()

        async def call(self, *args, **kwargs):
            self.started.set()
            try:
                await asyncio.Future()
            finally:
                self.closed.set()

    async def expect(code, call):
        try:
            await call
        except AIError as exc:
            assert exc.code == code, (code, exc.code)
        else:
            raise AssertionError(f"Expected {code}")

    offline, rejected, ambiguous, waiting = MockAdapter(), Failing(True), Failing(False), Waiting()
    models = dict(mock=Model("offline", offline, offline.supported, Tariff(1_000_000, 1_000_000)),
                  reject=Model("reject", rejected, rejected.supported),
                  lost=Model("ambiguous", ambiguous, ambiguous.supported),
                  wait=Model("waiting", waiting, waiting.supported))
    quota = Quota(requests=50, concurrent=1, tokens=10000)
    service = Service(models, user_quota=quota, app_quota=Quota(concurrent=20), deadline=5)
    prompt = Prompt((Message("user", (Part("text", "first"),)),
                     Message("assistant", (Part("text", "reply"),)),
                     Message("user", (Part("text", "last"),))), max_tokens=40)
    options = {"subject": "alice", "request_key": uuid4(), "input_reserve": 60}
    answer = await service.execute("mock", "text", prompt, **options)
    assert answer.text == "Offline response: last" and answer.usage.output_tokens == 4
    await expect("duplicate_request", service.execute("mock", "text", prompt, **options))
    assert (await service.execute("mock", "text", prompt, **(options | {"subject": "bob"}))).text == answer.text

    def charged_alias(key):
        return Invocation.objects.get(request_key=key).model_alias

    new_options = options | {"request_key": uuid4()}
    assert (await service.execute("reject", "text", prompt, fallback=("mock",), **new_options)).text == answer.text
    assert rejected.calls == 1 and await sync_to_async(charged_alias)(new_options["request_key"]) == "mock"
    await expect("offline_failure", service.execute("lost", "text", prompt, fallback=("mock",),
                                                    **(options | {"request_key": uuid4()})))
    assert ambiguous.calls == 1
    await expect("unsupported_capability", service.execute("mock", "image", {"prompt": "hello"},
                                                            **(options | {"request_key": uuid4()})))
    model = await service.execute("mock", "embeddings", {"input": ["abc"]}, **(options | {"request_key": uuid4()}))
    assert model.vectors == ((3.0, 1.0),)
    model = await service.execute("mock", "speech", {"input": "hello", "voice": "sample"},
                                  **(options | {"request_key": uuid4()}))
    assert model.media[0].mime == "audio/pcm"
    tool = Tool("look_up", "Returns an item", {"type": "object"})
    model = await service.execute("mock", "text", Prompt(prompt.messages, max_tokens=40, tools=(tool,)),
                                  **(options | {"request_key": uuid4()}))
    assert model.calls[0].name == "look_up"
    multimodal = Prompt((Message("user", (Part("text", "review"), Part("document", b"demo", "application/pdf"),
                                          Part("image", b"demo", "image/png"))),), max_tokens=40,
                        schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]})
    model = await service.execute("mock", "text", multimodal, **(options | {"request_key": uuid4()}))
    assert model.structured == {"answer": "Offline response: review [2 attachments]"}

    budget = Service(models, user_quota=Quota(cost_micros=99), app_quota=Quota(), deadline=1)
    await expect("ai_quota", budget.execute("mock", "text", prompt, **(options | {"subject": "budget", "request_key": uuid4()})))
    await expect("missing_price", budget.execute("lost", "text", prompt, **(options | {"subject": "budget", "request_key": uuid4()})))
    task = asyncio.create_task(service.execute("wait", "text", prompt, **(options | {"request_key": uuid4()})))
    await waiting.started.wait()
    await expect("ai_quota", service.execute("mock", "text", prompt, **(options | {"request_key": uuid4()})))
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert waiting.closed.is_set()
    timed = Service(models, user_quota=quota, app_quota=Quota(concurrent=20), deadline=0.03)
    await expect("provider_timeout", timed.execute("wait", "text", prompt, **(options | {"request_key": uuid4()})))
    async with aclosing(service.stream("mock", prompt, **(options | {"request_key": uuid4()}))) as stream:
        assert (await anext(stream)).kind == "text"

    async with aclosing(service.stream("mock", prompt, **(options | {"request_key": uuid4()}))) as stream:
        assert "".join([event.data async for event in stream if event.kind == "text"]) == answer.text

    client = await sync_to_async(register)("ai_browser")
    browser = AsyncClient(enforce_csrf_checks=True)
    browser.cookies = client.cookies.copy()
    csrf = {"X-CSRFToken": browser.cookies["csrftoken"].value}
    body = {"idempotency_key": str(uuid4()), "messages": [{"role": "user", "text": "browser"}]}
    with override_settings(ALLOWED_HOSTS=["testserver"]):
        result = await browser.post("/api/ai/chat", body, content_type="application/json", headers=csrf)
        assert result.status_code == 200 and result.json()["text"] == "Offline response: browser", result.content
        body["idempotency_key"] = str(uuid4())
        stream = await browser.post("/api/ai/stream", body, content_type="application/json", headers=csrf)
        assert stream.status_code == 200
        chunks = [chunk async for chunk in stream.streaming_content]
        assert b'"done": true' in b"".join(chunks)
        assert (await browser.get("/api/ready")).status_code == 200
        assert (await browser.get("/api/files/")).status_code == 200
        state = await browser.get("/api/account/profile")
        assert state.status_code == 200
        assert (await browser.get("/admin/")).status_code == 302

    def voice_snapshot():
        booked = reserve("snapshot", uuid4(), "gemini-live", 30, 0, 30, user_quota=quota, app_quota=Quota())
        settle(booked, Invocation.State.UNKNOWN, "gemini-live", Usage(2, 3))
        row = Invocation.objects.get(pk=booked)
        assert (row.tokens, row.input_tokens, row.output_tokens) == (30, 2, 3)

    await sync_to_async(voice_snapshot)()


async def exercise_sockets():
    from asgiref.sync import sync_to_async
    from asgiref.testing import ApplicationCommunicator
    from django.test.utils import override_settings

    from config.asgi import application
    from smackend.realtime.models import SocketLease

    first = await sync_to_async(register)("socket_first")
    second = await sync_to_async(register)("socket_second")

    async def attach(path, client=None, origin=b"http://127.0.0.1:8000"):
        headers = [(b"host", b"127.0.0.1:8000")]
        if origin:
            headers.append((b"origin", origin))
        if client:
            headers.append((b"cookie", f'sessionid={client.cookies["sessionid"].value}'.encode()))
        scope = {"type": "websocket", "path": path, "headers": headers, "scheme": "ws", "query_string": b""}
        socket = ApplicationCommunicator(application, scope)
        await socket.send_input({"type": "websocket.connect"})
        return socket, await socket.receive_output(timeout=5)

    async def disconnect(socket):
        await socket.send_input({"type": "websocket.disconnect", "code": 1000})
        await socket.wait(timeout=5)

    async def message(socket, **content):
        await socket.send_input({"type": "websocket.receive", "text": json.dumps(content)})

    for who, origin in ((None, b"http://127.0.0.1:8000"), (first, b"https://attacker.invalid"), (first, None)):
        rejected, response = await attach("/ws/realtime/", who, origin=origin)
        assert response["type"] == "websocket.close"
        await disconnect(rejected)

    with override_settings(SMACKEND_REALTIME_RECHECK_SECONDS=0.05, SMACKEND_REALTIME_USER_CONNECTIONS=1):
        echo, opened = await attach("/ws/realtime/", first)
        other, opened_other = await attach("/ws/realtime/", second)
        assert opened["type"] == opened_other["type"] == "websocket.accept"
        denied, rejection = await attach("/ws/voice/", first)
        assert rejection == {"type": "websocket.close", "code": 1013}
        await disconnect(denied)
        await message(echo, type="echo", text="first only")
        event = await echo.receive_output()
        assert json.loads(event["text"])["text"] == "first only"
        assert await other.receive_nothing()
        await message(echo, type="ping")
        assert json.loads((await echo.receive_output())["text"]) == {"type": "pong"}

        # Another process commits revocation to PostgreSQL. The original ASGI
        # connection must stop even though its Channels scope still holds a user.
        code = ("import sys,django; django.setup(); from django.contrib.sessions.models import Session; "
                "Session.objects.filter(session_key=sys.argv[1]).delete()")
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code,
                                                        first.cookies["sessionid"].value],
                                         capture_output=True, timeout=12)
        assert result.returncode == 0
        assert (await echo.receive_output(timeout=5))["code"] == 4403
        await disconnect(echo)
        await disconnect(other)

        voice, opened = await attach("/ws/voice/", second)
        assert opened["type"] == "websocket.accept"
        await message(voice, type="start", key=str(uuid4()))
        ready = json.loads((await voice.receive_output())["text"])
        assert ready == {"type": "voice_ready", "sample_rate": 16000}, ready
        audio = b"\x03\0" * 320
        await voice.send_input({"type": "websocket.receive", "bytes": audio})
        assert (await voice.receive_output())["bytes"] == audio
        await message(voice, type="end")
        assert json.loads((await voice.receive_output())["text"])["type"] == "turn_complete"
        await message(voice, type="stop")
        assert json.loads((await voice.receive_output())["text"])["type"] == "voice_stopped"
        await voice.send_input({"type": "websocket.receive", "bytes": b"x" * 5000})
        assert (await voice.receive_output())["code"] == 1009
        await disconnect(voice)

    with override_settings(SMACKEND_REALTIME_AUTHORIZE=lambda user, resource: resource != "voice"):
        denied, rejection = await attach("/ws/voice/", second)
        assert rejection["code"] == 4403
        await disconnect(denied)
    assert await sync_to_async(SocketLease.objects.count)() == 0


def disabled_smoke():
    from django.apps import apps
    from django.test import Client

    client = Client(HTTP_HOST="127.0.0.1")
    assert client.get("/api/ready").status_code == 200
    assert client.get("/assist/").status_code == 404
    assert not apps.is_installed("channels") and not apps.is_installed("smackend.ai")


def cross_process_ai_budget():
    """Separate application processes contend for the last paid reservation."""
    from django.conf import settings
    from django.db.models import Sum
    from django.utils import timezone
    from smackend.ai.models import Invocation

    current_charge = Invocation.objects.filter(day=timezone.now().date()).aggregate(total=Sum("cost_micros", default=0))["total"]

    candidate = """
import os, sys, time, django
from pathlib import Path
from uuid import uuid4
django.setup()
from smackend.ai.contracts import AIError
from smackend.ai.ledger import Quota, reserve
root, index = Path(sys.argv[1]), sys.argv[2]
(root / index).touch()
deadline = time.monotonic() + 10
while not (root / 'go').exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('Missing start signal')
    time.sleep(0.02)
try:
    reserve(index, uuid4(), "paid", 2, 10, 30,
            user_quota=Quota(), app_quota=Quota(cost_micros=int(sys.argv[3])))
except AIError as error:
    print(error.code)
else:
    print("admitted")
"""
    from tempfile import TemporaryDirectory
    from time import monotonic, sleep

    environment = {**os.environ, "PYTHONPATH": str(settings.BASE_DIR)}
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        children = [
            subprocess.Popen([sys.executable, "-c", candidate, directory, f"paid-{index}", str(current_charge + 10)],
                             env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for index in range(2)
        ]
        try:
            deadline = monotonic() + 10
            while not all((Path(directory) / f"paid-{index}").exists() for index in range(2)):
                assert all(child.poll() is None for child in children)
                assert monotonic() < deadline, "Timed out waiting for independent AI workers"
                sleep(0.02)
            (Path(directory) / "go").touch()
            outcomes = []
            for child in children:
                stdout, stderr = child.communicate(timeout=10)
                assert not child.returncode, stderr
                outcomes.append(stdout.strip())
            assert sorted(outcomes) == ["admitted", "ai_quota"], outcomes
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()


def main(mode):
    sys.path.insert(0, str(Path.cwd()))
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    if mode == "disabled":
        os.environ.update(SMACKEND_AI_MODELS="invalid", SMACKEND_REALTIME_ORIGINS="invalid")
    with database():
        django.setup()
        from django.core.management import call_command

        call_command("check")
        call_command("migrate", interactive=False, verbosity=0)
        call_command("makemigrations", "--check", "--dry-run", interactive=False)
        if mode in {"ai", "combined"}:
            asyncio.run(exercise_ai())
        if mode in {"realtime", "combined"}:
            asyncio.run(exercise_sockets())
        if mode == "combined":
            cross_process_ai_budget()
            checks = subprocess.run(
                [sys.executable, "-c", "from smackend.cli import main; raise SystemExit(main(['verify']))"],
                text=True, capture_output=True, timeout=70,
            )
            assert checks.returncode == 0, checks.stdout + checks.stderr
        if mode == "disabled":
            disabled_smoke()


if __name__ == "__main__":
    main(sys.argv[1])
