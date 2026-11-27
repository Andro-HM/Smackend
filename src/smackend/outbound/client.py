import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
import json
import logging
from random import uniform
import re
from threading import BoundedSemaphore
from time import monotonic, sleep
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

import httpx


SAFE_REPLAY = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
RETRY_STATUSES = {408, 429, 502, 503, 504}
PROTECTED_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "upgrade", "accept-encoding",
                     "proxy-authorization", "proxy-connection"}
logger = logging.getLogger("smackend.outbound")


@dataclass(frozen=True)
class Provider:
    base_url: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    connect_timeout: float = 3
    response_timeout: float = 5
    total_timeout: float = 15
    admission_timeout: float = 1
    concurrency: int = 8
    attempts: int = 3
    max_response_bytes: int = 2 << 20
    max_backoff: float = 3
    allow_http: bool = False

    def __post_init__(self):
        try:
            origin = urlsplit(self.base_url)
            if (origin.scheme not in ({"http", "https"} if self.allow_http else {"https"})
                    or not origin.hostname or origin.username is not None or origin.path not in {"", "/"}
                    or origin.query or origin.fragment or origin.port == 0
                    or not self.base_url.isascii() or re.search(r"[\s\\%]", self.base_url)):
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Provider base_url must be a trusted HTTP origin without credentials or a path.") from None
        for key, bound in (("connect_timeout", 60), ("response_timeout", 120), ("total_timeout", 300),
                           ("admission_timeout", 30), ("max_backoff", 60)):
            value = getattr(self, key)
            if type(value) not in (float, int) or not 0 < value <= bound:
                raise ValueError(f"Provider {key} must be a positive number at most {bound}.")
        for key, bound in (("concurrency", 64), ("attempts", 5), ("max_response_bytes", 32 << 20)):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= bound:
                raise ValueError(f"Provider {key} must be an integer between 1 and {bound}.")
        if not isinstance(self.headers, dict):
            raise ValueError("Provider headers must be an object.")
        validate_headers(self.headers)
        object.__setattr__(self, "headers", MappingProxyType(self.headers.copy()))


class ProviderFailure(Exception):
    def __init__(self, code, *, status=None, retry_after=None, transient=False):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.transient = transient


@dataclass(frozen=True)
class ProviderResponse:
    status: int
    headers: httpx.Headers = field(repr=False)
    body: bytes = field(repr=False)

    def json(self, validate=None):
        try:
            value = json.loads(self.body)
            return value if validate is None else validate(value)
        except (TypeError, ValueError, KeyError):
            raise ProviderFailure("invalid_response", status=self.status) from None


def validate_headers(headers):
    names = set()
    for key, value in headers.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                or key.lower() in PROTECTED_HEADERS or key.lower() in names
                or not isinstance(value, str) or not re.fullmatch(r"[\x20-\x7e]*", value)):
            raise ValueError("Provider headers contain a forbidden header or invalid value.")
        names.add(key.lower())
    return names


def request_arguments(provider, method, path, *, headers=None, params=None, json=None, content=None,
                      retry_non_idempotent=False, idempotency_key=None):
    method = method.upper()
    decoded = unquote(path)
    if (not path.startswith("/") or decoded.startswith("//") or any(part in {".", ".."} for part in decoded.split("/"))
            or re.search(r"[?#\\%\x00-\x20\x7f]", decoded) or not path.isascii()):
        raise ValueError("Use a same-origin absolute path without credentials, query or traversal segments.")
    if content is not None and (not isinstance(content, bytes) or json is not None):
        raise ValueError("Supply JSON or a replayable bytes body.")
    headers = dict(headers or {})
    overrides = validate_headers(headers)
    if overrides & ({key.lower() for key in provider.headers} | {"authorization", "cookie", "idempotency-key"}):
        raise ValueError("Request headers cannot override provider authentication or idempotency headers.")
    if retry_non_idempotent and not idempotency_key:
        raise ValueError("Opting into write retries requires a provider-supported idempotency key.")
    if idempotency_key:
        validate_headers({"Idempotency-Key": idempotency_key})
        headers["Idempotency-Key"] = idempotency_key
    replay = provider.attempts if method in SAFE_REPLAY or retry_non_idempotent else 1
    return method, provider.base_url.rstrip("/") + path, replay, {
        "params": params, "json": json, "content": content, "headers": headers,
    }


def retry_after(response):
    value = response.headers.get("Retry-After", "")
    if len(value) > 100:
        return None
    if value.isascii() and value.isdecimal():
        return min(int(value), 86_400)
    try:
        return min(86_400, max(0, (parsedate_to_datetime(value).astimezone(UTC) - datetime.now(UTC)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


def check_status(response):
    if not response.is_success:
        raise ProviderFailure("rate_limited" if response.status_code == 429 else "provider_status",
                              status=response.status_code, transient=response.status_code in RETRY_STATUSES,
                              retry_after=retry_after(response))
    if response.headers.get("content-encoding", "identity").lower() not in {"identity", ""}:
        raise ProviderFailure("unsupported_encoding", status=response.status_code)


def account_bytes(target, chunk, limit):
    if len(target) + len(chunk) > limit:
        raise ProviderFailure("response_too_large")
    target.extend(chunk)


def seconds_left(deadline):
    left = deadline - monotonic()
    if left <= 0:
        raise ProviderFailure("provider_timeout")
    return left


def attempt_timeout(provider, deadline):
    left = seconds_left(deadline)
    return httpx.Timeout(min(provider.response_timeout, left),
                         connect=min(provider.connect_timeout, left), pool=min(provider.admission_timeout, left))


def transport_failure(exc):
    return ProviderFailure("provider_timeout" if isinstance(exc, httpx.TimeoutException) else "provider_unavailable",
                           transient=True)


def next_delay(provider, failure, attempt, attempts, deadline):
    logger.warning("Outbound attempt failed", extra={"event": "outbound.failure", "status_code": failure.status})
    if not failure.transient or attempt + 1 >= attempts:
        raise failure from None
    delay = (failure.retry_after if failure.retry_after is not None
             else uniform(0, min(provider.max_backoff, 0.2 * 2 ** attempt)))
    if delay > provider.max_backoff or delay >= seconds_left(deadline):
        raise failure from None
    return delay


def client_options(provider, transport):
    return dict(
        headers={"Accept-Encoding": "identity", **provider.headers}, follow_redirects=False, trust_env=False,
        limits=httpx.Limits(max_connections=provider.concurrency, max_keepalive_connections=provider.concurrency),
        transport=transport,
    )


class Client:
    def __init__(self, provider, *, transport=None):
        self.provider = provider
        self._client = httpx.Client(**client_options(provider, transport))
        self._slots = BoundedSemaphore(provider.concurrency)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._client.close()

    def request(self, method, path, **kwargs):
        provider = self.provider
        method, url, attempts, options = request_arguments(provider, method, path, **kwargs)
        deadline = monotonic() + provider.total_timeout
        if not self._slots.acquire(timeout=min(provider.admission_timeout, provider.total_timeout)):
            raise ProviderFailure("provider_busy")
        try:
            for attempt in range(attempts):
                try:
                    with self._client.stream(method, url, **options, timeout=attempt_timeout(provider, deadline)) as response:
                        check_status(response)
                        body = bytearray()
                        chunks = [response.content] if response.is_stream_consumed else response.iter_raw(chunk_size=32768)
                        for chunk in chunks:
                            seconds_left(deadline)
                            account_bytes(body, chunk, provider.max_response_bytes)
                        seconds_left(deadline)
                        return ProviderResponse(response.status_code, response.headers, bytes(body))
                except (httpx.RequestError, ProviderFailure) as exc:
                    failure = exc if isinstance(exc, ProviderFailure) else transport_failure(exc)
                sleep(next_delay(provider, failure, attempt, attempts, deadline))
        finally:
            self._slots.release()


class _RejectCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


class AsyncClient:
    def __init__(self, provider, *, transport=None, _shared_slots=None):
        self.provider = provider
        self._client = httpx.AsyncClient(**client_options(provider, transport))
        # Upstream cookies must never carry state from one application request to another.
        self._client.cookies.jar = CookieJar(policy=_RejectCookies())
        self._slots = _shared_slots if _shared_slots is not None else asyncio.Semaphore(provider.concurrency)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        await self._client.aclose()

    @asynccontextmanager
    async def stream(self, method, path, **kwargs):
        """Expose a bounded raw stream. The caller owns any decision to retry."""
        provider = self.provider
        method, url, _, options = request_arguments(provider, method, path, **kwargs)
        deadline = monotonic() + provider.total_timeout
        try:
            async with asyncio.timeout(min(provider.admission_timeout, provider.total_timeout)):
                await self._slots.acquire()
        except TimeoutError:
            raise ProviderFailure("provider_busy") from None
        try:
            async with asyncio.timeout(seconds_left(deadline)):
                async with self._client.stream(method, url, **options,
                                               timeout=attempt_timeout(provider, deadline)) as response:
                    check_status(response)

                    async def chunks():
                        size = 0
                        if response.is_stream_consumed:
                            if len(response.content) > provider.max_response_bytes:
                                raise ProviderFailure("response_too_large")
                            yield response.content
                            return
                        async for chunk in response.aiter_raw():
                            size += len(chunk)
                            if size > provider.max_response_bytes:
                                raise ProviderFailure("response_too_large")
                            yield chunk

                    yield chunks()
        except TimeoutError:
            raise ProviderFailure("provider_timeout") from None
        except httpx.RequestError as exc:
            raise transport_failure(exc) from None
        finally:
            self._slots.release()

    async def request(self, method, path, **kwargs):
        provider = self.provider
        method, url, attempts, options = request_arguments(provider, method, path, **kwargs)
        deadline = monotonic() + provider.total_timeout
        try:
            async with asyncio.timeout(min(provider.admission_timeout, provider.total_timeout)):
                await self._slots.acquire()
        except TimeoutError:
            raise ProviderFailure("provider_busy") from None
        try:
            async with asyncio.timeout(seconds_left(deadline)):
                for attempt in range(attempts):
                    try:
                        async with self._client.stream(method, url, **options,
                                                       timeout=attempt_timeout(provider, deadline)) as response:
                            check_status(response)
                            body = bytearray()
                            if response.is_stream_consumed:
                                account_bytes(body, response.content, provider.max_response_bytes)
                            else:
                                async for chunk in response.aiter_raw(chunk_size=32768):
                                    account_bytes(body, chunk, provider.max_response_bytes)
                            return ProviderResponse(response.status_code, response.headers, bytes(body))
                    except (httpx.RequestError, ProviderFailure) as exc:
                        failure = exc if isinstance(exc, ProviderFailure) else transport_failure(exc)
                    await asyncio.sleep(next_delay(provider, failure, attempt, attempts, deadline))
        except TimeoutError:
            raise ProviderFailure("provider_timeout") from None
        finally:
            self._slots.release()
