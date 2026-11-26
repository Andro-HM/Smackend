import asyncio
from base64 import b64encode
from contextlib import asynccontextmanager
import json

import pytest

pytest.importorskip("websockets")

from smackend.realtime.voice import GeminiLive


def test_gemini_live_protocol_and_cancellation():
    async def run():
        sent, heard, events = [], [], []
        transmitted = asyncio.Event()
        played = asyncio.Event()
        left = asyncio.Event()
        pcm = b"\x01\x00" * 320

        class Connection:
            async def send(self, message):
                data = json.loads(message)
                sent.append(data)
                if data.get("realtimeInput", {}).get("audioStreamEnd"):
                    transmitted.set()

            async def recv(self):
                return '{"setupComplete":{}}'

            async def __aiter__(self):
                await transmitted.wait()
                yield json.dumps({"serverContent": {
                    "modelTurn": {"parts": [{"inlineData": {
                        "mimeType": "audio/pcm;rate=24000", "data": b64encode(pcm).decode(),
                    }}]}, "turnComplete": True,
                }})
                await asyncio.Future()

        @asynccontextmanager
        async def connect(url, **options):
            assert url.startswith("wss://generativelanguage.googleapis.com/") and "key=synthetic" in url
            assert options["max_queue"] == 4 and options["logger"].disabled
            try:
                yield Connection()
            finally:
                left.set()

        async def emit(data):
            heard.append(data)

        async def event(payload):
            events.append(payload)
            if payload["type"] == "turn_complete":
                played.set()

        queue = asyncio.Queue(maxsize=4)
        queue.put_nowait(pcm)
        queue.put_nowait(None)
        relay = GeminiLive("selected-model", "synthetic", connector=connect)
        task = asyncio.create_task(relay.relay(queue, emit, event))
        await asyncio.wait_for(played.wait(), timeout=3)
        assert sent[0]["setup"]["model"] == "models/selected-model"
        assert sent[1]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
        assert heard == [pcm] and events[0]["sample_rate"] == 24000
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert left.is_set()

    asyncio.run(run())
