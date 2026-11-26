import asyncio
from base64 import b64decode, b64encode
import json
import logging
from urllib.parse import urlencode

from websockets.asyncio.client import connect


class VoiceError(Exception):
    """Public details about a provider failure are deliberately generic."""


class MockVoice:
    rate = 16000

    async def relay(self, queue, emit, emit_event):
        await emit_event({"type": "voice_ready", "sample_rate": self.rate})
        while True:
            data = await queue.get()
            if data is None:
                await emit_event({"type": "turn_complete"})
            else:
                await emit(data)


class GeminiLive:
    """Server-to-server Live API transport. No credentials are sent to the browser."""

    rate = 24000
    endpoint = ("wss://generativelanguage.googleapis.com/ws/"
                "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")

    def __init__(self, model, key, *, connector=connect):
        self.model, self.key, self.connector = model, key, connector

    async def relay(self, queue, emit, emit_event):
        # WebSocket DEBUG logs may include the authorization query. Use a private,
        # disabled transport logger independently of application log settings.
        transport_log = logging.getLogger("smackend.realtime.gemini_transport")
        transport_log.disabled = True
        try:
            async with self.connector(
                self.endpoint + "?" + urlencode({"key": self.key}),
                max_size=256 << 10, max_queue=4, open_timeout=6, close_timeout=1, proxy=None,
                logger=transport_log,
            ) as socket:
                await socket.send(json.dumps({"setup": {"model": "models/" + self.model,
                                                        "generationConfig": {"responseModalities": ["AUDIO"]}}}))
                async with asyncio.timeout(6):
                    setup = json.loads(await socket.recv())
                if "setupComplete" not in setup:
                    raise VoiceError("Live voice setup failed.")
                await emit_event({"type": "voice_ready", "sample_rate": self.rate})
                send = asyncio.create_task(self.send_audio(socket, queue))
                receive = asyncio.create_task(self.read_audio(socket, emit, emit_event))
                tasks = (send, receive)
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            raise VoiceError("The live voice provider ended the connection.") from None

    async def send_audio(self, socket, queue):
        while True:
            data = await queue.get()
            payload = {"audioStreamEnd": True} if data is None else {
                "audio": {"mimeType": "audio/pcm;rate=16000", "data": b64encode(data).decode("ascii")},
            }
            await socket.send(json.dumps({"realtimeInput": payload}))

    async def read_audio(self, socket, emit, emit_event):
        async for raw in socket:
            event = json.loads(raw)
            if "goAway" in event or "error" in event:
                raise VoiceError("The live voice provider ended the connection.")
            content = event.get("serverContent", {})
            usage = event.get("usageMetadata", {})
            input_tokens, total_tokens = usage.get("promptTokenCount"), usage.get("totalTokenCount")
            if type(input_tokens) is int and type(total_tokens) is int and 0 <= input_tokens <= total_tokens <= 10**9:
                await emit_event({"type": "usage", "input_tokens": input_tokens,
                                  "output_tokens": total_tokens - input_tokens})
            if content.get("interrupted"):
                await emit_event({"type": "interrupted"})
            for part in content.get("modelTurn", {}).get("parts", ()):
                inline = part.get("inlineData")
                if inline and inline["mimeType"].startswith("audio/pcm"):
                    audio = b64decode(inline["data"], validate=True)
                    for start in range(0, len(audio), 8192):
                        await emit(audio[start:start + 8192])
            if content.get("turnComplete"):
                await emit_event({"type": "turn_complete"})
