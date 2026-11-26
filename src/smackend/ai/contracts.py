from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


class AIError(Exception):
    def __init__(self, code, message, *, status=502, safe_to_retry=False, retry_after=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.safe_to_retry = safe_to_retry
        self.retry_after = retry_after


@dataclass(frozen=True)
class Part:
    kind: Literal["text", "image", "document", "audio"]
    data: str | bytes = field(repr=False)
    mime: str = ""


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant", "system", "tool"]
    parts: tuple[Part, ...]
    calls: tuple[ToolCall, ...] = ()
    tool_id: str = ""
    tool_name: str = ""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict


@dataclass(frozen=True)
class Prompt:
    messages: tuple[Message, ...]
    max_tokens: int = 256
    schema: dict | None = field(default=None, repr=False)
    tools: tuple[Tool, ...] = ()

    def requirements(self, *, stream=False):
        required = {"text"}
        if stream:
            required.add("stream")
        if self.schema is not None:
            required.add("json")
        if self.tools or any(message.calls or message.role == "tool" for message in self.messages):
            required.add("tools")
        required.update(part.kind + "_input" for message in self.messages for part in message.parts
                        if part.kind != "text")
        return required


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self):
        if any(type(value) is not int or not 0 <= value <= 10**9 for value in (self.input_tokens, self.output_tokens)):
            raise ValueError("Invalid reported token usage")


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict = field(repr=False)


@dataclass(frozen=True)
class Result:
    text: str = field(default="", repr=False)
    structured: object = field(default=None, repr=False)
    calls: tuple[ToolCall, ...] = ()
    media: tuple[Part, ...] = ()
    vectors: tuple[tuple[float, ...], ...] = field(default=(), repr=False)
    usage: Usage | None = None
    native: object = field(default=None, repr=False)


@dataclass(frozen=True)
class Event:
    kind: Literal["text", "tool_delta", "usage"]
    data: str | dict | Usage = field(repr=False)


def validate_schema(schema, value):
    try:
        # Block remote schema retrieval and keep validation entirely offline.
        from referencing import Registry

        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, registry=Registry()).validate(value)
    except (SchemaError, ValidationError, ValueError, TypeError, RecursionError):
        raise AIError("invalid_structured_output", "The AI response did not match the declared JSON schema.") from None
    except Exception as exc:
        # The registry raises an unresolved-reference error for missing resources.
        if exc.__class__.__module__.startswith(("referencing", "jsonschema")):
            raise AIError("invalid_structured_output", "The AI response schema could not be resolved locally.") from None
        raise
