# Changelog

## 0.1.0a10 — Existing-project module activation alpha

- Added `smackend add` and `smackend status` for auth, storage, admin, HTTP, AI and real-time modules.
- Added dry-run planning, explicit a9 upgrade, local-wheel discovery, managed migrations and configuration rollback before migration execution. [Activation guide](PHASE_A10.md)

## 0.1.0a9 — Performance remediation alpha

- Added worker-owned async HTTP pools and a shared per-provider admission budget for outbound integrations and built-in AI adapters.
- Corrected benchmark accounting and added protected route comparisons, correlated timing and integrity checks.
- Passed 140 PostgreSQL regression tests, wheel and source installation smoke tests, and all six installed-project verification checks on WSL2 with PostgreSQL 17.11.
- Demonstrated lower local fan-out, AI and first-event latency in the a8 comparison. The 100-user soak completed 74,960 valid operations and met sampled resource limits. Sustained CRUD, SSE first-event and 500-socket latency missed their targets.
- Passed local PostgreSQL interruption, worker replacement, restore, HTTPS security and direct WSS revocation checks. The reference mixed-load, deployment and live-provider gates remain open.
- Existing projects can enable managed outbound clients by wrapping their ASGI application and enabling lifespan. No toolkit migration is required. [Remediation report](PHASE_4_OPTIMIZATION.md) · [a9 qualification](PHASE_4.md#a9-final-qualification-follow-up)

## 0.1.0a8 — Qualification alpha

- Updated ASGI identity middleware to keep ordinary requests on the async path while preserving account and session checks.
- Raised the storage extra to Pillow 13 and restricted decoder selection to configured formats.
- Added installed-package benchmarks, a repeatable consumer rehearsal, dependency checks, and isolated Linux deployment and recovery evidence.
- Prepared publication documentation and repository hygiene. Original production qualification targets remain open. [Phase 4 report](PHASE_4.md)

## 0.1.0a7 — AI and real-time

- Added optional provider adapters, streaming and multimodal interfaces, usage controls, and offline examples.
- Added authenticated Django Channels sockets and a Gemini Live transport with a simulated test path. [Phase 3](PHASE_3.md)

## 0.1.0a6 — Optional infrastructure

- Added managed storage, Django administration, outbound HTTP, and module activation. [Phase 2](PHASE_2.md)

## 0.1.0a5 — Browser identity

- Added configurable browser accounts and session-based authorization, verified identifiers, OTP flows, and linked Google sign-in. [Phase 2A](PHASE_2A.md)

## 0.1.0a1–0.1.0a4 — Toolkit foundation

- Introduced the CLI and scaffold, validated settings, PostgreSQL connection management, and reusable API and verification infrastructure. [Phase 1](PHASE_1.md)
