from base64 import b64decode
from contextlib import aclosing
import json
from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import StreamingHttpResponse
from ninja import Field, Router, Schema

from smackend.errors import APIError
from smackend.identity.permissions import session_auth
from smackend.outbound.django import release_django_connections

from .configuration import configured_service
from .contracts import AIError, Message, Part, Prompt


class Attachment(Schema):
    mime: str = Field(max_length=48)
    data: str = Field(max_length=2_000_000)


class Turn(Schema):
    role: str
    text: str = Field(max_length=16_000)


class Chat(Schema):
    idempotency_key: UUID
    messages: list[Turn] = Field(min_length=1, max_length=20)
    attachment: Attachment | None = None


class Answer(Schema):
    text: str


def prompt_for(body):
    if any(item.role not in {"user", "assistant"} for item in body.messages) or body.messages[-1].role != "user":
        raise APIError(400, "invalid_messages", "Use user and assistant turns, ending with a user message.")
    messages = [Message(item.role, (Part("text", item.text),)) for item in body.messages]
    if body.attachment:
        mime = body.attachment.mime
        kind = {"application/pdf": "document"}.get(mime, "image" if mime.startswith("image/") else "")
        try:
            data = b64decode(body.attachment.data, validate=True)
        except ValueError:
            raise APIError(400, "invalid_attachment", "Supply a Base64 image or PDF.") from None
        if not kind or not data:
            raise APIError(400, "invalid_attachment", "Supply a Base64 image or PDF.")
        messages[-1] = Message("user", (*messages[-1].parts, Part(kind, data, mime)))
    return Prompt(tuple(messages))


def input_bound(prompt):
    # Conservative example allowance. Applications can supply a model-specific upper bound.
    return max(4096, sum((len(part.data.encode("utf-8")) if isinstance(part.data, str) else len(part.data) // 8) + 32
                         for message in prompt.messages for part in message.parts))


def ai_router():
    router = Router(auth=session_auth, tags=["AI examples"])

    @router.post("/chat", response=Answer)
    async def chat(request, body: Chat):
        subject = request.auth.pk
        await sync_to_async(release_django_connections)()
        prompt = prompt_for(body)
        try:
            result = await configured_service().execute(
                settings.SMACKEND_AI_DEMO_MODEL, "text", prompt, subject=subject,
                request_key=body.idempotency_key, input_reserve=input_bound(prompt),
            )
        except AIError as exc:
            raise APIError(exc.status, exc.code, str(exc)) from None
        return {"text": result.text}

    @router.post("/stream")
    async def stream(request, body: Chat):
        subject = request.auth.pk
        await sync_to_async(release_django_connections)()
        prompt = prompt_for(body)
        service = configured_service()

        async def generate():
            try:
                async with aclosing(service.stream(
                    settings.SMACKEND_AI_DEMO_MODEL, prompt, subject=subject, request_key=body.idempotency_key,
                    input_reserve=input_bound(prompt),
                )) as events:
                    async for event in events:
                        if event.kind == "text":
                            yield "data: " + json.dumps({"text": event.data}) + "\n\n"
                yield "data: " + json.dumps({"done": True}) + "\n\n"
            except AIError as exc:
                yield "event: error\ndata: " + json.dumps({"code": exc.code, "message": str(exc)}) + "\n\n"

        return StreamingHttpResponse(generate(), content_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        })

    return router
