import asyncio
from contextlib import suppress
from datetime import timedelta
from importlib import import_module
import json
import logging
from time import monotonic
from types import SimpleNamespace
from uuid import UUID

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.contrib.auth import get_user
from django.db import connection, transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.module_loading import import_string

from .models import SocketLease
from .voice import GeminiLive, MockVoice


logger = logging.getLogger("smackend.realtime")
MAX_FRAME = 4096
QUEUE_DEPTH = 8


@database_sync_to_async
def authorized(session_key, expected, resource):
    if not session_key or expected is None:
        return False
    store = import_module(settings.SESSION_ENGINE).SessionStore(session_key)
    user = get_user(SimpleNamespace(session=store))
    if not user.is_authenticated or not user.is_active or user.pk != expected:
        return False
    policy = getattr(settings, "SMACKEND_REALTIME_AUTHORIZE", None)
    if isinstance(policy, str):
        policy = import_string(policy)
    return policy is None or bool(policy(user, resource))


@database_sync_to_async
def acquire_slot(subject):
    key = salted_hmac("smackend.socket.subject", str(subject), algorithm="sha256").hexdigest()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [6289300457026569])
        now = timezone.now()
        expired = list(SocketLease.objects.filter(expires_at__lte=now).values_list("pk", flat=True)[:128])
        if expired:
            SocketLease.objects.filter(pk__in=expired).delete()
        active = SocketLease.objects.filter(expires_at__gt=now)
        if (active.count() >= settings.SMACKEND_REALTIME_CONNECTION_LIMIT
                or active.filter(subject=key).count() >= settings.SMACKEND_REALTIME_USER_CONNECTIONS):
            return None
        return SocketLease.objects.create(
            subject=key, expires_at=now + timedelta(seconds=settings.SMACKEND_REALTIME_SECONDS + 10),
        ).pk


@database_sync_to_async
def release_slot(slot):
    SocketLease.objects.filter(pk=slot).delete()


class Socket(AsyncWebsocketConsumer):
    voice = False

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            if hasattr(self, "slot"):
                await self.disconnect(1006)

    async def connect(self):
        self.closed = False
        self.slot = self.voice_task = self.monitor = self.queue = None
        self.send_lock = asyncio.Lock()
        self.subject = self.scope["user"].pk
        self.session_key = self.scope["session"].session_key
        self.resource = "voice" if self.voice else "echo"
        self.started = self.updated = monotonic()
        try:
            if not await self.check_access():
                return await self.close(code=4403)
            if self.voice and settings.SMACKEND_REALTIME_VOICE_MODE == "disabled":
                return await self.close(code=4404)
            self.slot = await acquire_slot(self.subject)
        except Exception as exc:
            logger.warning("Socket admission unavailable", extra={"event": "realtime.failure",
                                                                  "error_type": type(exc).__name__})
            return await self.close(code=1013)
        if self.slot is None:
            return await self.close(code=1013)
        await self.accept()
        self.monitor = asyncio.create_task(self.revalidate())

    async def check_access(self):
        return await authorized(self.session_key, self.subject, self.resource)

    async def revalidate(self):
        try:
            while True:
                await asyncio.sleep(getattr(settings, "SMACKEND_REALTIME_RECHECK_SECONDS", 2))
                now = monotonic()
                if now - self.updated > 45 or now - self.started > settings.SMACKEND_REALTIME_SECONDS:
                    return await self.finish(4000)
                if not await self.check_access():
                    return await self.finish(4403)
        except Exception as exc:
            logger.warning("Socket revalidation failed", extra={"event": "realtime.failure",
                                                                "error_type": type(exc).__name__})
            await self.finish(1013)

    async def emit(self, *, text=None, data=None):
        async with asyncio.timeout(2), self.send_lock:
            if not self.closed:
                await self.send(text_data=text, bytes_data=data)

    async def emit_json(self, value):
        await self.emit(text=json.dumps(value, separators=(",", ":")))

    async def enqueue(self, frame):
        if self.queue is None or not self.voice_task or self.voice_task.done():
            return await self.emit_json({"type": "error", "code": "voice_not_started"})
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            await self.finish(1013)

    async def receive(self, text_data=None, bytes_data=None):
        if self.closed:
            return
        self.updated = monotonic()
        if bytes_data is not None:
            if not self.voice or not 0 < len(bytes_data) <= MAX_FRAME or len(bytes_data) % 2:
                return await self.finish(1009)
            return await self.enqueue(bytes_data)
        if not text_data or len(text_data.encode("utf-8")) > MAX_FRAME:
            return await self.finish(1009)
        try:
            payload = json.loads(text_data)
            command = payload["type"]
        except (ValueError, KeyError, TypeError, RecursionError):
            return await self.finish(1007)
        if command == "ping":
            await self.emit_json({"type": "pong"})
        elif command == "echo" and not self.voice and isinstance(payload.get("text"), str):
            await self.emit_json({"type": "echo", "text": payload["text"]})
        elif self.voice and command == "start" and (self.voice_task is None or self.voice_task.done()):
            try:
                call_key = UUID(payload.get("key", ""))
            except (ValueError, AttributeError, TypeError):
                return await self.emit_json({"type": "error", "code": "voice_key_required"})
            self.queue = asyncio.Queue(maxsize=QUEUE_DEPTH)
            self.voice_task = asyncio.create_task(self.run_voice(call_key))
        elif self.voice and command == "end":
            await self.enqueue(None)
        elif self.voice and command == "stop":
            await self.stop_voice()
            await self.emit_json({"type": "voice_stopped"})
        else:
            await self.emit_json({"type": "error", "code": "unsupported_message"})

    async def run_voice(self, call_key):
        reservation = None
        observed = {"input_tokens": 0, "output_tokens": 0}

        async def event(message):
            if message["type"] == "usage":
                for key in observed:
                    observed[key] = max(observed[key], message[key])
            else:
                await self.emit_json(message)

        try:
            if not await self.check_access():
                await self.finish(4403)
                return
            if settings.SMACKEND_REALTIME_VOICE_MODE == "gemini":
                from smackend.ai.configuration import configured_service
                from smackend.ai.ledger import reserve

                service = configured_service()
                reservation = await database_sync_to_async(reserve)(
                    self.subject, call_key, "gemini-live", settings.SMACKEND_REALTIME_TOKEN_RESERVATION, 0,
                    settings.SMACKEND_REALTIME_SECONDS + 30, user_quota=service.user_quota, app_quota=service.app_quota,
                )
                voice = GeminiLive(settings.SMACKEND_GEMINI_LIVE_MODEL, settings.SMACKEND_GEMINI_LIVE_KEY)
            else:
                voice = MockVoice()
            await voice.relay(self.queue, self.emit_audio, event)
        except Exception as exc:
            logger.warning("Realtime voice ended", extra={"event": "realtime.failure", "error_type": type(exc).__name__})
            with suppress(TimeoutError, OSError):
                await self.emit_json({"type": "error", "code": getattr(exc, "code", "voice_unavailable")})
        finally:
            if reservation is not None:
                from smackend.ai.contracts import Usage
                from smackend.ai.ledger import settle
                from smackend.ai.models import Invocation

                # Keep the reservation when a Live session cannot supply authoritative
                # final billing totals. Never replay the stream after a disconnect.
                try:
                    await database_sync_to_async(settle)(
                        reservation, Invocation.State.UNKNOWN, "gemini-live", Usage(**observed) if any(observed.values()) else None,
                    )
                except Exception as exc:
                    logger.warning("Voice settlement failed", extra={"event": "realtime.failure",
                                                                    "error_type": type(exc).__name__})

    async def emit_audio(self, audio):
        await self.emit(data=audio)

    async def stop_voice(self):
        if self.voice_task and self.voice_task is not asyncio.current_task():
            self.voice_task.cancel()
            await asyncio.gather(self.voice_task, return_exceptions=True)
        self.voice_task = self.queue = None

    async def finish(self, code):
        if self.closed:
            return
        self.closed = True
        await self.stop_voice()
        await self.close(code=code)

    async def disconnect(self, code):
        self.closed = True
        if self.monitor:
            monitor, self.monitor = self.monitor, None
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        await self.stop_voice()
        if self.slot is not None:
            slot, self.slot = self.slot, None
            try:
                await release_slot(slot)
            except Exception:
                logger.warning("Socket lease will expire without explicit cleanup", extra={"event": "realtime.failure"})


class VoiceSocket(Socket):
    voice = True
