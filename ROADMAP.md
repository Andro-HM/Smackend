# Smackend roadmap

Phase 0 is provisionally concluded. The [experiment and measured results](experiments/phase0/RESULTS.md), [original architecture](ARCHITECTURE.md), and [framework evaluation](FRAMEWORK_EVALUATION.md) remain the reference material.

The first GitHub publication presents `0.1.0a8` as an alpha. See the [release notes](CHANGELOG.md) and [qualification report](PHASE_4.md) for the changes and supporting evidence.

| Stage | Exit condition |
|---|---|
| **Phase 1.1 — Package and scaffold** | Install a versioned wheel or source distribution, generate a project from the installed CLI, migrate a clean PostgreSQL database, and serve the frontend and Ninja endpoints through ordinary Django entry points. |
| **Phase 1.2–1.8 — Reusable core** | Validated settings, PostgreSQL pooling, project-owned user model and migrations, native router composition, shared API errors, structured diagnostics and application verification. |
| **Phase 2A — Browser identity and authorization** | Verify the optional allauth integration, documented signup and login profiles, PostgreSQL sessions and rate limits, lifecycle changes, and application-owned authorization. Live provider qualification remains separate from simulated integration coverage. |
| **Phase 2B–2E — Optional modules** | Verify managed storage, native administration, outbound HTTP, consistent opt-in configuration, and the combined application upgrade. Qualify live provider services separately. |
| **Phase 3 — AI and real-time** | Verify optional AI adapters, PostgreSQL admission and usage, authenticated Channels, per-connection voice, and combined scaffolds with deterministic providers. Qualify live model, billing, and voice behavior separately. |
| **Later toolkit milestones** | Extend identity where consumer requirements justify it and add the remaining reusable modules with separate scopes and compatibility tests. |
| **Phase 4 — Mandatory before the hackathon-ready release** | Alpha qualification exercises the toolkit on Windows PostgreSQL and on Ubuntu WSL2 with native PostgreSQL. The installed-package regression, module activation, consumer rehearsals, and isolated TLS/recovery checks passed. Reference mixed-load, Docker and live-provider gates remain open. [Measured results](PHASE_4.md) |

## Phase 0 backlog

| Open item | Evidence so far | Evidence required before the hackathon-ready release |
|---|---|---|
| PostgreSQL connection pool contention | The Windows mixed audit measured p95 checkout at 242–270 ms. Releasing the provider connection early shortened its hold time. Widening the pool alone increased DB memory without a repeatable latency gain. | Measure acquisition and hold time per route on Linux with the agreed connection budget. Verify behavior under overload, worker replacement and PostgreSQL restart. |
| Django sync/async handoff overhead | A baseline notes list crossed the boundary a median of 11 times. Reducing handoffs removed measurable work without a stable end-to-end win. | Profile the same HTTP route and middleware under Linux ASGI. Compare one change at a time and rerun session, CSRF and authorization tests. |
| Missed latency targets | The original 100-user local run recorded 590 ms p95 for list and 580 ms for create. The proposed targets were 250 ms and 500 ms respectively. | Repeat staged CRUD and mixed runs on a representative dataset with an independent load host. Report p95, p99, achieved throughput, late starts and failures. Apply or explicitly revise the targets from measured evidence. |
| Sustained concurrency | The Phase 0 runs used short ramps and a separate invalid-login burst. | Run a 30-minute 100-user soak, five admitted successful logins/s, and a bounded overload and recovery trial. Verify row ownership, resource limits and writes interrupted during DB recovery. |
| Linux, Docker and HTTPS qualification | Phase 0 ran against native Windows PostgreSQL. Compose startup and server deployment behind HTTPS remain open. | Verify clean Compose startup, data retention across container recreation, migrations on Linux, trusted proxy handling and browser enforcement of Secure session and CSRF cookies. |

Phase 4 also covers CPU and memory profiles under concurrent model calls, response streams and WebSockets. Qualify live model and storage providers, real token and audio billing, proxy buffering and backpressure, end-to-end latency, worker replacement, sustained stress, revocation under load, database outages and ambiguous provider recovery. Retain the Phase 0 connection, handoff and latency targets above until measured evidence supports a revision.

Keep the Phase 0 Windows numbers as a diagnostic baseline. They describe the workloads that ran on that host.

## Alpha release qualification

- **Passed locally.** The installed a8 wheel and source regressions, the upgrade preserving an existing session and private file, all 26 distinct activation profiles, three consumer extensions, and the isolated Linux recovery and restore probe passed.
- **Security remediation.** The storage extra uses Pillow 13 and restricts decoder selection to configured input formats. Keep scanning the exact deployed dependency set and its native libraries.
- **Open.** Qualify full reference performance with a separate load generator, the PostgreSQL 17 Compose profile and production ingress, sustained real-time and mixed traffic, larger media and stress behavior, live providers, and direct Google registration. Keep the original performance criteria.

| Gate | Status and next evidence |
|---|---|
| 4.1 Baseline | Installed-toolkit Windows and WSL2 baselines and raw timelines retained. Move the load generator off-host for reference qualification. |
| 4.2–4.4 Performance | Local middleware, pool, worker and outbound lifecycle comparisons ran. Worker-owned HTTP reuse met the isolated fan-out target. The 30-minute 100-user run met resource limits and missed latency. Repeat the reference mixed workload and real-time capacity runs under the specified envelope. |
| 4.5 Security and reliability | Storage dependency and decoder remediation passed regression. Cross-process AI spending containment and disposable database failure checks passed. Close the stressed session-revocation, high-load failure and native dependency qualification gaps. |
| 4.6 Deployment | Native WSL2, verified PostgreSQL TLS, HTTP proxy cookies, worker replacement and backup/restore passed locally. Qualify Compose, PostgreSQL 17 on Linux, production ingress and browser HTTPS/WSS. |
| 4.7 Live providers | Simulated only. Real delivery, storage, OAuth, model, voice and billing tests require approved providers and test credentials. Direct Google registration remains outstanding. |
| 4.8 Consumer rehearsal | Three installed-package applications and wildcard extensions passed. Verification detected broken ownership and session handling. A human-timed rehearsal remains open. |
| 4.9 Release | Wheel, source, upgrade and activation checks passed locally. Maintain the alpha designation until the mandatory performance, deployment, provider and recovery gates pass. |
