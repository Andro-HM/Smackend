import asyncio
from contextlib import aclosing
from dataclasses import dataclass, replace
import json
import logging
from random import uniform
import re
from time import monotonic
from uuid import UUID

from asgiref.sync import sync_to_async

from .contracts import AIError, Prompt, Usage, validate_schema
from .ledger import reserve, settle
from .models import Invocation


logger = logging.getLogger("smackend.ai")
GENERATIVE = {"text", "stream"}
MIMES = {
    "image": {"image/png", "image/jpeg", "image/webp", "image/gif"},
    "document": {"application/pdf"},
    "audio": {"audio/wav", "audio/mpeg", "audio/pcm", "audio/ogg", "audio/flac"},
}


@dataclass(frozen=True)
class Tariff:
    input_micros_per_million: int
    output_micros_per_million: int

    def __post_init__(self):
        if any(type(rate) is not int or not 0 <= rate <= 10**12
               for rate in (self.input_micros_per_million, self.output_micros_per_million)):
            raise ValueError("AI token rates must be bounded nonnegative integers.")

    def cost(self, usage):
        total = usage.input_tokens * self.input_micros_per_million + usage.output_tokens * self.output_micros_per_million
        return (total + 999999) // 1000000


@dataclass(frozen=True)
class Model:
    identifier: str
    adapter: object
    capabilities: frozenset[str]
    tariff: Tariff | None = None

    def __post_init__(self):
        if not self.identifier or len(self.identifier) > 180 or not self.capabilities:
            raise ValueError("Supply a model identifier and at least one declared capability.")
        if not self.capabilities <= self.adapter.supported:
            raise ValueError("The model declares capabilities the adapter does not implement.")


def validate_payload(operation, payload):
    if operation not in GENERATIVE:
        if not isinstance(payload, dict):
            raise AIError("invalid_ai_input", "Provide an object for the selected operation.", status=400)
        if operation == "embeddings":
            values = payload.get("input")
            valid = set(payload) == {"input"} and isinstance(values, list) and 1 <= len(values) <= 32 and all(
                isinstance(text, str) and 0 < len(text) <= 16000 for text in values)
        elif operation in {"image", "speech"}:
            goal = "input" if operation == "speech" else "prompt"
            required = {goal, "voice"} if operation == "speech" else {goal}
            valid = set(payload) == required and isinstance(payload.get(goal), str) and 1 <= len(payload[goal]) <= 16000
            if operation == "speech":
                valid = valid and isinstance(payload.get("voice"), str) and 1 <= len(payload["voice"]) <= 80
        else:
            valid = False
        if not valid:
            raise AIError("invalid_ai_input", "Supply the bounded input fields for the selected operation.", status=400)
        return {operation}
    if (not isinstance(payload, Prompt) or not 1 <= len(payload.messages) <= 32
            or type(payload.max_tokens) is not int or not 1 <= payload.max_tokens <= 16000 or len(payload.tools) > 16):
        raise AIError("invalid_ai_input", "Check the conversation and output token limits.", status=400)
    total = 0
    for message in payload.messages:
        if (message.role not in {"system", "user", "assistant", "tool"} or len(message.parts) > 8
                or not (message.parts or message.calls) or (message.calls and message.role != "assistant")):
            raise AIError("invalid_ai_input", "Each message needs a supported role and one or more parts.", status=400)
        if message.role == "tool" and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", message.tool_name):
            raise AIError("invalid_ai_input", "Tool responses must name their declared tool.", status=400)
        for part in message.parts:
            if part.kind == "text" and isinstance(part.data, str):
                total += len(part.data.encode("utf-8"))
            elif (message.role == "user" and part.kind in MIMES and part.mime in MIMES[part.kind]
                  and isinstance(part.data, bytes)):
                total += len(part.data)
            else:
                raise AIError("invalid_ai_input", "Use text or a supported user attachment.", status=400)
    if total > 6 << 20 or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name) for tool in payload.tools):
        raise AIError("invalid_ai_input", "The conversation exceeds a size or tool limit.", status=400)
    if operation == "stream" and payload.schema is not None:
        raise AIError("unsupported_capability", "Use a non-streaming request for validated structured output.", status=400)
    return payload.requirements(stream=operation == "stream")


class Service:
    def __init__(self, models, *, user_quota, app_quota, deadline=25, attempts=1, clients=None):
        if not 0 < deadline <= 300 or type(attempts) is not int or not 1 <= attempts <= 3:
            raise ValueError("AI deadline or retry attempt limit is invalid.")
        self.models = models
        self.user_quota, self.app_quota = user_quota, app_quota
        self.deadline, self.attempts = deadline, attempts
        self.clients = clients or {}

    def available(self, model, capability):
        return model in self.models and capability in self.models[model].capabilities

    async def _reserve(self, alias, operation, payload, subject, request_key, input_reserve, fallback):
        requirements = validate_payload(operation, payload)
        if not isinstance(request_key, UUID) or not str(subject).strip():
            raise AIError("invalid_request_key", "Supply a stable actor and an idempotency UUID.", status=400)
        if type(input_reserve) is not int or not 1 <= input_reserve <= 10**8 or len(fallback) > 3:
            raise AIError("invalid_ai_input", "The input reservation or fallback count is out of range.", status=400)
        aliases = (alias, *fallback)
        if len(aliases) != len(set(aliases)):
            raise AIError("invalid_model", "Fallback aliases must be distinct.", status=400)
        output = payload.max_tokens if operation in GENERATIVE else 0
        quote = Usage(input_reserve, output)
        costs = []
        for name in aliases:
            candidate = self.models.get(name)
            if candidate is None:
                raise AIError("invalid_model", "Select a configured model alias.", status=400)
            if requirements - candidate.capabilities:
                missing = ", ".join(sorted(requirements - candidate.capabilities))
                raise AIError("unsupported_capability", f"The selected model lacks: {missing}.", status=400)
            # Flat image/audio billing and some embedding responses cannot be priced reliably from tokens.
            billable_tokens = operation in GENERATIVE and not any(item.endswith("_input") for item in requirements)
            if (self.user_quota.cost_micros is not None or self.app_quota.cost_micros is not None) and (
                candidate.tariff is None or not billable_tokens
            ):
                raise AIError("missing_price", "Spending controls require a priced text operation.", status=400)
            costs.append(candidate.tariff.cost(quote) if candidate.tariff and billable_tokens else 0)
        reservation = await sync_to_async(reserve)(
            subject, request_key, alias, quote.input_tokens + quote.output_tokens, max(costs),
            self.deadline + 30, user_quota=self.user_quota, app_quota=self.app_quota,
        )
        return aliases, reservation

    async def _settle(self, reservation, state, alias, usage, operation):
        model = self.models[alias]
        billed = model.tariff.cost(usage) if usage and model.tariff and operation in GENERATIVE else None
        try:
            await sync_to_async(settle)(reservation, state, alias, usage, billed)
        except Exception as exc:
            logger.error("AI ledger settlement failed", extra={"event": "ai.settlement_failed",
                                                               "error_type": type(exc).__name__})

    def open_client(self, alias):
        from contextlib import nullcontext

        return (nullcontext(self.clients[alias]) if alias in self.clients else self.models[alias].adapter.client())

    async def _next(self, failure, attempt, deadline):
        if not failure.safe_to_retry:
            raise failure
        if attempt + 1 == self.attempts:
            return False
        delay = failure.retry_after if failure.retry_after is not None else uniform(0, 0.15 * 2**attempt)
        if delay > 2 or monotonic() + delay >= deadline:
            raise failure
        await asyncio.sleep(delay)
        return True

    async def execute(self, alias, operation, payload, *, subject, request_key, input_reserve, fallback=()):
        if operation == "stream":
            raise AIError("invalid_ai_input", "Use the streaming interface for this operation.", status=400)
        aliases, booking = await self._reserve(alias, operation, payload, subject, request_key, input_reserve, fallback)
        state, usage = Invocation.State.UNKNOWN, None
        deadline = monotonic() + self.deadline
        try:
            async with asyncio.timeout(self.deadline):
                for alias in aliases:
                    model = self.models[alias]
                    for attempt in range(self.attempts):
                        state = Invocation.State.UNKNOWN
                        try:
                            async with self.open_client(alias) as client:
                                result = await model.adapter.call(operation, model.identifier, payload, client=client)
                        except AIError as failure:
                            if failure.safe_to_retry:
                                state = Invocation.State.REJECTED
                            if not await self._next(failure, attempt, deadline):
                                break
                        else:
                            usage, state = result.usage, Invocation.State.COMPLETE
                            if operation == "text" and payload.schema is not None:
                                try:
                                    value = json.loads(result.text)
                                except (ValueError, TypeError, RecursionError):
                                    raise AIError("invalid_structured_output", "The AI response was not valid JSON.") from None
                                validate_schema(payload.schema, value)
                                result = replace(result, structured=value)
                            return result
                raise AIError("providers_busy", "The configured AI providers rejected the request.", status=503)
        except TimeoutError:
            raise AIError("provider_timeout", "The AI request expired.", status=504) from None
        finally:
            await self._settle(booking, state, alias, usage, operation)

    async def stream(self, alias, prompt, *, subject, request_key, input_reserve, fallback=()):
        aliases, booking = await self._reserve(alias, "stream", prompt, subject, request_key, input_reserve, fallback)
        state, emitted, usage = Invocation.State.UNKNOWN, False, None
        deadline = monotonic() + self.deadline
        try:
            for alias in aliases:
                model = self.models[alias]
                for attempt in range(self.attempts):
                    state = Invocation.State.UNKNOWN
                    try:
                        async with self.open_client(alias) as client:
                            async with aclosing(model.adapter.stream(model.identifier, prompt, client=client)) as events:
                                while True:
                                    try:
                                        async with asyncio.timeout(max(0, deadline - monotonic())):
                                            event = await anext(events)
                                    except StopAsyncIteration:
                                        break
                                    emitted = True
                                    if event.kind == "usage":
                                        report = event.data
                                        usage = Usage(max(report.input_tokens, usage.input_tokens if usage else 0),
                                                      max(report.output_tokens, usage.output_tokens if usage else 0))
                                    yield event
                    except AIError as failure:
                        if emitted:
                            raise
                        if failure.safe_to_retry:
                            state = Invocation.State.REJECTED
                        if not await self._next(failure, attempt, deadline):
                            break
                    else:
                        state = Invocation.State.COMPLETE
                        return
            raise AIError("providers_busy", "The configured AI providers rejected the request.", status=503)
        except TimeoutError:
            raise AIError("provider_timeout", "The AI stream expired.", status=504) from None
        finally:
            # A mid-stream usage snapshot may omit tokens billed after disconnect.
            await self._settle(booking, state, alias, usage if state == Invocation.State.COMPLETE else None, "stream")
