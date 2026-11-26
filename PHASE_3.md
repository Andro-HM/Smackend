# AI and real-time integration

`smackend init my-app --ai --realtime` generates a project with optional AI and Django Channels. Use either flag independently. Both need browser accounts. AI also needs the existing HTTP module. The generated project retains its own URL, ASGI and ordinary Django settings and models. Install from the wheel or source distribution, apply reviewed migrations and sign in at `/account/` before opening `/assist/`. The examples default to an offline text provider and a PCM echo relay.

## Models, providers and capabilities

Use `SMACKEND_HTTP_PROVIDERS` from Phase 2 to configure trusted origins, TLS, request timeouts, pool limits and server-side credential references. Then set `SMACKEND_AI_MODELS` to an object keyed by application aliases. Each entry needs `adapter`, `provider`, a provider-supplied `model` identifier, and explicit `capabilities`. The default offline alias never reaches the network.

```json
{
  "general": {
    "adapter": "openai",
    "provider": "openai-http",
    "model": "YOUR_MODEL_ID",
    "capabilities": ["text", "stream", "json", "tools", "image_input"]
  }
}
```

Configure the outbound provider with the vendor's credential header and an environment reference, such as a Bearer authorization header for OpenAI, `x-goog-api-key` for Gemini or `x-api-key` for Anthropic. Leave the examples offline with `SMACKEND_AI_DEMO_MODEL=offline`, set an empty value to remove demo endpoints, or explicitly choose a text model alias after reviewing its admission settings.

| Adapter | Implemented wire contracts |
|---|---|
| `openai` | Chat Completions text, SSE, JSON Schema and tools. Image, PDF and WAV/MP3 input parts when the selected model supports them. Embeddings, base64-returning image generation, and speech via dedicated endpoints. |
| `compatible`, `local` | OpenAI-compatible wire format with `max_tokens` and no forced stream usage option. Configure origin and optional prefix. Only advertise capabilities the server implements. |
| `gemini` | GenerateContent text, SSE, inline images/PDF/audio, JSON Schema and tools. Batched embeddings. Image and speech output for models with the supported response modalities. |
| `anthropic` | Messages text, SSE, image/PDF, JSON format and tools. Embedding and media-generation operations are outside this adapter. |
| Custom adapter | A dotted factory receiving an outbound `Provider`, optional prefix and explicit 429 replay policy. Implement `supported`, `client()`, `call()` and `stream()` for the desired operations. |

Capabilities describe both the adapter and the operator-selected model. Requests are checked before admission. Vendor-specific combinations and schema restrictions still apply. Text and tool deltas stream with separate usage events. Validated structured output is available on the complete-response path. Image, embedding and speech results use the complete-response interface. For other vendor operations, use the adapter's native `client()` or register an application adapter. The built-in tool interface returns proposed calls and accepts explicit tool-result turns. Applications validate arguments, authorize actions, execute tools, and store conversation state. [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) · [Gemini generation](https://ai.google.dev/api/generate-content) · [Anthropic Messages](https://platform.claude.com/docs/en/api/messages)

## Admission, billing and cancellation

Use `configured_service()` or build a `Service` with application models and quotas. Pass the authenticated actor, a durable application-request UUID, and a conservative input token reservation to `execute()` or `stream()`. Never derive the actor from an untrusted request field. Pass immutable message history and tool schemas from your application. Application-owned clients may be supplied per alias to reuse connection pools within a worker event loop. In async endpoints, finish authorized ORM work in one synchronous helper and call `release_django_connections()` there before awaiting external I/O.

PostgreSQL serializes admission for application and actor budgets. `SMACKEND_AI_QUOTAS` accepts `user` and `app` objects with `requests`, `tokens`, `concurrent`, and optional `cost_micros`. Request and token budgets use the UTC day. The default user budget is 100 requests, 250,000 tokens and three concurrent requests. The application budget is 1,000 requests, 2,500,000 tokens and 20 concurrent requests. AI requests may time out or be cancelled independently of authentication, storage, ordinary HTTP or database operations.

The ledger records stable idempotency keys, model aliases, usage and conservative reservations. Keep idempotency history and the Django secret stable. Ambiguous dispatch, disconnect or crash keeps the reservation until reconciled. Leases expire for concurrency accounting. A caller may explicitly request a bounded fallback chain or bounded same-model retries. Only a known admission rejection may be replayed. A provider's HTTP 429 qualifies only when its documented admission semantics are trusted and `replay_429` is explicitly enabled. Timeouts, transport ambiguity and partial streams do not trigger replay or fallback. Consume streams using an async context that closes them when the client disconnects.

For spending caps, configure `price: [input_micros_per_million, output_micros_per_million]` for every candidate in one currency and supply an upper bound for input tokens. Hard budget behavior depends on that bound, provider output enforcement and reliable token prices and usage. Validated usage replaces the reservation when available. If actual usage exceeds the reservation, the ledger records the larger charge and subsequent admission accounts for it. Flat-rate media and multi-modal requests are rejected when monetary caps apply. The ledger is an admission and reconciliation aid. Compare it with actual provider invoices before relying on it for billing.

## Channels and Gemini Live

`/ws/realtime/` echoes only to its own socket. `/ws/voice/` accepts PCM frames after a JSON `start` message with a UUID, plus `end`, `stop` and `ping` controls. The real Gemini relay uses mono PCM16 at 16 kHz upstream and 24 kHz downstream. It handles the setup handshake, stream-end and interruption controls, snapshots reported usage, and closes both sides on cancellation. Use `SMACKEND_REALTIME_VOICE=gemini`, `SMACKEND_GEMINI_LIVE_MODEL` and `SMACKEND_GEMINI_LIVE_KEY` to activate it. Gemini Live needs AI token accounting. It keeps a conservative usage reservation because final billable usage is not guaranteed, and it is unavailable with monetary cap settings. The default mock echoes PCM at 16 kHz and incurs no upstream cost. [Gemini Live WebSockets reference](https://ai.google.dev/api/live)

Set exact browser origins with `SMACKEND_REALTIME_ORIGINS`, including scheme and port when applicable. HTTPS origins are required in production. Channels authenticates with Django's shared PostgreSQL session. On the default two-second recheck interval, the socket reloads that session and evaluates the app policy when configured through `SMACKEND_REALTIME_AUTHORIZE`. The policy receives the authenticated user and `echo` or `voice`. A failed check, invalidated session, or database error closes the connection. PostgreSQL leases bound concurrent sockets per application and actor. Default limits are 40 and 3 respectively. Set `SMACKEND_REALTIME_SECONDS` to bound session duration. The example sends heartbeats every 15 seconds and requires a fresh connection after disconnect. Voice reconnection does not resume or replay a call.

There is no Channels layer configured. Use the built-in sockets for per-connection work, not cross-worker groups, shared rooms, offline delivery or transparent resume. Put message limits at the proxy and ASGI server as well. For local ASGI testing use `uvicorn config.asgi:application --lifespan off --no-access-log --ws-max-size 4096 --ws-max-queue 8` from the generated project. The relay bounds its provider queue, frames and send wait. A slow consumer is disconnected when the queue fills. Applications can pass their own routes to `with_realtime()` and must enforce their own resource policies on custom consumers. [Channels security](https://channels.readthedocs.io/en/stable/topics/security.html)

## Upgrading and verification

Preserve the existing project, secrets, accounts and database. Update the Smackend pin and select the extras you intend to enable. Merge Phase 3 configuration, ASGI and URL/API registration from a fresh scaffold. Copy the optional frontend examples if desired. Apply the AI and WebSocket lease migrations only for enabled modules, then run `smackend verify`. Disabling a module leaves its stored tables intact.

Run `uv sync --all-extras --group integration`, supply the suite's private disposable PostgreSQL DSN, and set `SMACKEND_PHASE3_SMOKE_PYTHON` to the installed app interpreter. The test matrix covers provider wire adapters, offline outputs and controls, authenticated and rejected WebSockets, cross-process session revocation, cancellation, isolated scopes and assembled existing modules. The Gemini Live transport is tested against a simulated bidirectional provider. No live API credentials or billable requests are needed for verification. Live provider/model qualification, pricing reconciliation, microphone and audio behavior under real production conditions, stress and deployment qualification remain Phase 4 work.

## Current qualification

See the [a8 capacity measurements](PHASE_4.md#capacity-characterization) and [live-provider status](PHASE_4.md#live-integrations) for the results after this milestone.
