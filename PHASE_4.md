# Smackend Phase 4 qualification

**Release candidate** `0.1.0a8` is an alpha. The release gates below require measured passes on the intended deployment. Phase 0's evidence remains the historical baseline.

## Evidence and scope

The [qualification harness](experiments/phase4/) generates an ordinary Django project with the installed wheel and adds an application-owned model, migrations and Ninja router. It seeds 1,000 synthetic identities with 100 owned records each and one private local file each. It uses the real password hasher for signup and login. Steady-state workloads start with pre-created database sessions so that signup cost is measured separately. Provider HTTP, AI responses and PCM are synthetic. No live credentials or private application data entered the harness.

[Raw evidence](experiments/phase4/evidence/) includes gzip JSON Lines with outcomes and scheduled-arrival latency, trial summaries with route percentiles and profiling fields, and process and PostgreSQL samples. It excludes cookies, credentials, SQL text, file contents and account identifiers. The Windows and Ubuntu trials use shared app, database, simulator and driver hosts. The Ubuntu runs are WSL2 characterization with native PostgreSQL 16.15. The reference architecture still calls for Linux, PostgreSQL 17, four allocated application and database vCPUs, TLS ingress and a separate load host.

| Threshold carried forward | Required evidence |
|---|---|
| Read p95 ≤250 ms, write ≤500 ms, aggregate ≤750 ms | Owner-scoped workloads at the 100-user target |
| Unexpected failures below 0.1%, zero unauthorized observations or corruption | Report per route, count missed arrivals separately and include the security and recovery results |
| Five admitted successful logins/s, login p95 ≤1 s | Distinct valid accounts with real Argon2 and independent signup traffic |
| External fan-out p95 ≤250 ms at 25 offered requests/s | Three independent 100 ms simulated calls and at least 99% completed |
| Standard SSE and socket targets | Proxy first-event and frame-gap tests, 100 and 500 persistent sockets, scoped revocation under load, and the 50-call duplex relay control |
| 30 minutes at 100 users | App RSS below 1.5 GiB, fewer than 40 DB connections, warmed-to-final median RSS growth below 10%, and recovery within the documented window |

## Windows baseline

The v0.1.0a7 wheel ran with two ASGI workers and eight pooled connections per worker on native Windows PostgreSQL 17.9. These short trials included cold processes and shared-host activity. Use them to identify bottlenecks and generator limitations. They cannot certify sustainable capacity.

| Scenario | Completed/s | Overall p50 / p95 / p99 (ms) | Read / write / aggregate p95 (ms) | Invalid / completed |
|---|---:|---|---|---:|
| CRUD, 50 users | 47.8 | 47 / 328 / 438 | 344 / 375 / 281 | 0 / 573 |
| CRUD, 100 users | 46.8 | 750 / 4,531 / 6,250 | 4,687 / 4,187 / 4,234 | 0 / 842 |
| CRUD, 200 users | 28.9 | 5,547 / 13,266 / 16,453 | 13,594 / 12,062 / 13,968 | 0 / 520 |
| CRUD, 500 users | 33.8 | 12,437 / 14,750 / 15,843 | 14,359 / 15,250 / 14,813 | 0 / 608 |
| CRUD, offered 100/s | 39.1 | 386 / 485 / 527 | 483 / 513 / 496 | 0 / 704 |
| Mixed, two trials at 100/s | 13.6–15.1 | p95 12,160–12,403 | read 12,151–12,325 | 204 / 515 combined |

The Windows driver missed 937 of 2,400 offered arrivals against the lightweight simulator alone. At 200 and 500 CRUD users the driver event-loop lag exceeded two seconds at p95. The server pool also saturated at 16 sessions. The data establish both local driver and server pressure. They leave the relative attribution at high concurrency unresolved. The two mixed runs held as many as 132 application threads, sampled 236–243 MiB peak application RSS, and consumed 38–50 application CPU seconds per trial. The Linux driver reuses a transport for separate login cookie exchanges and avoids unnecessary scheduler yields while catching up on fixed-rate arrivals.

At 100 CRUD users on Windows, the read p95 inside ASGI was 190 ms and checkout p95 was 31 ms, versus 4,687 ms on the client. During the mixed Windows runs, read checkout p95 reached about 4,014–4,016 ms, consistent with the four-second acquisition timeout. Read SQL execution p95 remained 44–69 ms. Saturated checkout and client scheduling both contributed. Under the deliberately held connection control, external-route connection hold p95 rose to 7.29–7.39 seconds from 77–261 ms with early release. This confirms the lease-duration mechanism. Variable arrivals and failed requests prevent attribution of a CRUD p95 improvement to that control.

## Ubuntu WSL2 baseline

| Workload, installed a7 | Completed/s | Invalid / completed | Missed / offered | Key p95 (ms) |
|---|---:|---:|---:|---:|
| No-DB simulator, offered 200/s | 37.3 | 0 / 448 | 1,952 / 2,400 | 6,763 total |
| 100-user CRUD, offered 50/s | 50.0 | 0 / 900 | 0 / 900 | read 81, write 84, aggregate 80 |
| 100-user mixed, three trials at 50/s | 34.9–42.9 | 0 / 2,399 | 601 / 3,000 | read 3,334–5,899 |
| Three-way external fan-out, offered 25/s | 20.6 | 1 / 411 | 89 / 500 | 4,951 total |

The CRUD reference slice used a median of three queries for reads, four for writes and two for session/profile. Read checkout p95 was 0.66 ms, SQL p95 was 2.57 ms, and read execution through ASGI headers was 13.69 ms. In mixed trials, read checkout p95 was 2.1–4.5 ms and the ASGI slice was 62–130 ms. Driver-loop lag reached 151 ms at p95 in one mixed trial. Client-side connection queuing, scheduling and co-host interference dominated the measured tail in these WSL2 runs. Smaller SQL timings alone cannot explain the user-visible miss. The installed a7 process reached first readiness in 1.94 s on WSL2 and 2.81 s on Windows. Five-second idle peak RSS was 191 MiB and 183 MiB respectively, with one app database connection after the readiness request.

On installed a8, the isolated two-worker core-only app reached readiness in 1.13 s and peaked at 169 MiB during ten seconds idle. The all-module app took 1.36 s and peaked at 187 MiB. Each showed 10 peak threads and one client database connection after readiness. Loading the core-only Django URL and ASGI stack imported none of the optional AI, HTTP, storage or account libraries. These are single cold-start observations. [Idle evidence](experiments/phase4/evidence/idle_probe.json)

## Sustained 100-user workload

After a 60-second warmup, the patched a8 wheel completed a 1,800-second WSL2 closed-loop run with 100 clients. The mix included authenticated CRUD and session checks, upload and download, external HTTP fan-out, model responses and short SSE streams. It produced 60,527 completed requests at 33.6/s, with one aggregate transport error and no observed wrong-owner responses in the checked payloads. The error rate was 0.0017% overall and 0.0166% for aggregates. The app, PostgreSQL, driver and simulator shared the host. Regression, TLS and browser checks also ran on that machine during parts of the soak.

| Operation | p50 (ms) | p95 (ms) | p99 (ms) | Gate |
|---|---:|---:|---:|---|
| Overall | 1,363 | 5,286 | 8,198 | 33.6 completed/s at the observed load |
| Read | 1,321 | 5,182 | 8,197 | Missed 250 ms |
| Write | 1,358 | 5,298 | 8,455 | Missed 500 ms |
| Aggregate | 1,329 | 5,351 | 8,211 | Missed 750 ms |
| Short SSE first event | 1,506 | 5,592 | 8,318 | Missed 250 ms |

Peak sampled app RSS was 243 MiB with 77 threads. Median app RSS grew 1.25%, from 239.68 MiB in minutes 5–10 to 242.67 MiB in the final five minutes. PostgreSQL reached 16 app connections. These measurements met the reference numerical RSS, growth and connection limits on this host. Sampled CPU time was 1,462 app seconds, 103 PostgreSQL seconds and 517 driver seconds. Driver-loop lag reached 133 ms at p95. The soak established completion and exposed a large latency miss. It did not exercise the reference combination of 50 duplex calls, 25 CRUD/s and five admitted logins/s simultaneously. [Soak evidence](experiments/phase4/evidence/linux-soak/)

An independent native curl control completed all 1,500 local no-DB requests at 4,820/s with p95 8.2 ms. The Python driver delivered only 37.3/s against the same type of no-DB simulator at 200 offered/s on WSL2. These runs establish substantial headroom in the simulator and local network path. The burst and fixed-rate protocols differ, and neither replaces an independent reference load host against the authenticated application. [Curl control](experiments/phase4/evidence/curl-control.json)

## Performance methodology

The profiling variants reuse the unchanged Phase 0 probes. SQL counts time driver execution, and checkout includes the pool's liveness check. The header snapshot includes measured holds and a current checkout up to first response headers. Long response bodies, WebSocket frames and final cleanup are outside that snapshot. Thread-adapter spans can nest. The calculated resume residual is a diagnostic proxy and is unsuitable as an exclusive CPU or scheduler measurement. Resource samples sum RSS over the process tree and can double-count shared pages. The driver reports its own event-loop lag and missed starts. Raw samples retain failed requests and timeouts. Treat lower tail latency with more missed arrivals as a failed comparison.

Source review found two synchronous middleware layers on the identity-enabled ASGI path. The candidate lets unrelated HTTP routes pass through the guard without a synchronous dispatch. Native allauth activity tracking remains available when configured, and allauth's login signals continue to register sessions for revocation. Credential admission, protected account writes, session locking, CSRF, and authentication keep their existing execution path and database semantics. Historical Phase 0 results reported about 11 handoffs and a 195 ms scheduling residual at p95. The new route has a different middleware stack and aggregate instrumentation. Compare those figures within their own harness.

The dedicated outbound route releases the authenticated request's database lease before starting three simulated calls. The Windows hold control deliberately keeps that lease until the response. AI ledger admission, session authorization, upload reservation and download lookup already release their own checkout before applicable outbound work. Provider delays, outbound-client setup and database waits are separate potential contributors.

## Middleware comparison

The a7 and middleware-only a8 artifacts used the same PostgreSQL, Python, Django, Pillow 12.3 and simulator versions, two workers and an eight-connection pool per worker. Pillow 12.3 in the comparison was confined to synthetic test files. The released candidate uses Pillow 13. Each mixed trial offered 50 requests/s for 20 seconds with 100 available actors.

| Measurement | a7 baseline | Middleware-only a8 |
|---|---:|---:|
| CRUD read p95 at 50/s (ms) | 81.3 | 72.8 |
| CRUD write p95 at 50/s (ms) | 84.3 | 74.7 |
| CRUD aggregate p95 at 50/s (ms) | 80.3 | 70.9 |
| Mixed total p95, three trials (ms) | 3,665–5,855 | 256–272 |
| Mixed read p95, three trials (ms) | 3,334–5,899 | 113–124 |
| Mixed achieved rate | 34.9–42.9/s | 50.0/s in each trial |
| Mixed missed arrivals | 601 of 3,000 | 0 of 3,000 |
| Mixed unexpected completed outcomes | 0 of 2,399 | 1 of 3,000 |
| External fan-out at 25/s, p95 (ms) | 4,951 | 508 |

The candidate removed long synchronous middleware spans around ordinary ASGI requests. The instrumented mixed reads still made three queries. Their median counted short executor dispatches rose from 2 to 15 because Django adapted more individual stages once the outer synchronous wrapper was gone. This result favors measuring how long a request occupies an executor or database checkout. The short quiet read plan executed in 0.116 ms on the seeded database. The post-trial check found 100,000 correctly scoped records and 1,000 consistent private file references. [Middleware trials](experiments/phase4/evidence/linux-middleware-a8/)

The same-host crossover narrowed the result. At 50 offered/s, a7 completed 1,000 and 955 requests, with mixed p95 of 278 and 2,196 ms. The patched a8 completed 1,000 requests in each repeat, with p95 of 298 and 255 ms. Both patched repeats used Pillow 13. Across the five local a8 mixed trials, all offered arrivals entered and completed. The healthy a7 run was faster than one of the a8 runs and used less app CPU. The shared-host results support retaining the narrow middleware change for steadier short-run admission. They do not establish the large latency reduction seen in the original comparison as a causal estimate or show a general p95 gain. [a7 crossover](experiments/phase4/evidence/linux-a7-confirm/) · [Patched a8 crossover](experiments/phase4/evidence/linux-secure-confirm/)

Two workers with four connections each respected an eight-connection budget. At 50 offered/s, the two mixed trials completed 47.5/s and 50.0/s, with p95 2,081 ms and 254 ms. One worker with a total budget of 16 cut peak app RSS to about 114 MiB. Its two mixed runs achieved 49.4/s and 40.0/s, with p95 1,524 ms and 4,066 ms. The two-worker configuration gave more consistent results in the short comparison. The pool-size difference remains inconclusive, and the package retains its existing configurable defaults. [Four-connection pools](experiments/phase4/evidence/linux-pool4/) · [Single-worker trials](experiments/phase4/evidence/linux-workers1/)

## Outbound connection reuse

The qualification application ran two trials of three simultaneous 100 ms calls per authenticated request. Both variants completed all 500 offered requests per trial at 25/s. Only the client lifecycle changed. The reused client retains its configured provider admission limit per worker.

| Measurement per trial | Client opened for each HTTP request | One client per ASGI worker |
|---|---:|---:|
| Client p95 (ms) | 920–1,044 | 233–235 |
| Distinct loopback connection identifiers across 1,500 provider calls | 1,430–1,470 | 18–19 |
| Sampled app CPU (seconds) | 27.91–28.47 | 10.88–11.40 |
| Peak sampled app RSS (MiB) | 229.0–231.6 | 208.5–210.1 |
| Peak app DB sessions | 9–10 | 3 |

This isolated improvement met the 250 ms fan-out target on both repeats. The example uses the installed `AsyncClient` and creates and closes it through the consumer's ASGI lifespan. Keep the client in its owning event loop and finish database work before the provider await. The package still lets each consumer choose its lifecycle and provider configuration. The measurements use loopback HTTP and a fixed synthetic delay. Real TLS and upstream outage qualification remain open. [Fresh-client runs](experiments/phase4/evidence/linux-external-fresh/) · [Reused-client runs](experiments/phase4/evidence/linux-external-reuse/) · [Lifespan example](experiments/phase4/lifecycle.py)

## Capacity characterization

These runs used the patched a8 artifact, a 100,000-row dataset, two workers and a budget of eight pooled connections per worker. Closed-loop clients wait for each response before their next operation. To measure fixed-rate admission separately, the harness schedules arrivals independently and records busy actors and late starts as missed offers.

| CRUD users | Completed/s | Failed / completed | Overall p50 / p95 / p99 (ms) | Read / write / aggregate p95 (ms) |
|---|---:|---:|---|---|
| 10 | 10.1 | 0 / 152 | 31 / 113 / 200 | 138 / 73 / 73 |
| 20 | 19.5 | 0 / 293 | 35 / 205 / 294 | 214 / 75 / 76 |
| 50 | 46.1 | 0 / 692 | 66 / 490 / 602 | 506 / 465 / 484 |
| 100 | 45.2 | 0 / 904 | 940 / 3,310 / 5,030 | 3,289 / 2,992 / 3,550 |
| 200 | 40.8 | 0 / 815 | 2,773 / 11,998 / 15,349 | 11,383 / 11,441 / 12,645 |
| 500 | 53.5 | 1 / 1,070 | 7,359 / 14,238 / 16,389 | 14,238 / 13,871 / 14,923 |

The local system stopped scaling steadily beyond the 50-user run. The 16-connection app budget filled at 50 users and above. A single short run per user count and the driver control limit capacity attribution. At 25 compound operations/s, all 450 create-then-delete operations succeeded with 104 ms p95 across both requests. [Capacity evidence](experiments/phase4/evidence/linux-capacity/)

| Isolated workload | Offered/s | Successful / offered | p95 (ms) |
|---|---:|---:|---:|
| Valid logins for 60 seconds | 5 | 300 / 300 | 253 |
| Distinct signups for 30 seconds | 3 | 90 / 90 | 216 |
| Private 4 KiB file replacement | 20 | 240 / 240 | 87 |
| Private 4 KiB download | 50 | 600 / 600 | 75 |
| Synthetic AI completion | 25 | 375 / 375 | 321 |
| Synthetic three-event SSE | 25 | 375 / 375 | 355 to first event |

The authentication load reused the real Argon2 policy and explicitly raised only the test per-IP allowance to 400/minute because all clients shared loopback. Application-wide and identifier budgets remained active. All 300 logins were admitted at the target rate and met the one-second p95 target on this isolated host. The short SSE first-event target still missed. The file tests characterize small payloads. [Per-route evidence](experiments/phase4/evidence/linux-capacity/)

In 40-second socket holds, the 100-client trial returned all 2,000 echoes with 19.8 ms p95. The 500-client trial returned all 9,256 echoes checked, with 409 ms p95 against the 100 ms target. Each echo checked its own unique payload. Short PCM echo trials using repeated calls measured per-frame p95 of 7.2, 15.7, 55.6 and 111.9 ms at 10, 20, 50 and 100 users. These are basic echo observations from the point-to-point mock. They do not certify long-lived rooms, stressed revocation or the live voice provider. [Socket and basic PCM evidence](experiments/phase4/evidence/linux-capacity/)

The 100-client SSE trial returned all 100 numbered events in order in all 271 completed streams. First-event p95 was 5,429 ms and inter-event p99 was 145 ms. The first-event gate missed and the local gap target passed. The dedicated duplex test sent and received 640-byte sequence-marked frames independently at a nominal 20 ms cadence. At 50 calls, all 21,100 expected relay frames matched. Relay RTT p95 was 21.0 ms versus 1.2 ms on the direct 50-client simulator, a 19.8 ms difference between the two distributions. At 100 calls all 35,100 frames matched, with p95 RTT rising to 1,005 ms. The final integrity snapshot found zero socket leases. The loopback trials used the mock relay without production ingress. Long-lived, interruption, failure, slow-reader and mixed voice gates remain open. [Streaming and duplex runs](experiments/phase4/evidence/linux-realtime/)

A 35-second combined burst offered 100 operations/s across 150 actors with CRUD, login, AI, SSE and duplex voice. It completed 1,151 valid operations and missed 2,349 starts. Read p95 was 8.62 s, login p95 was 11.83 s at only 1.9 successful logins/s, and voice-frame RTT p95 was 220 ms. The resulting database and file integrity checks passed with zero remaining socket leases. This is overload characterization. The full 30-minute reference mix remains an open gate. [Combined burst](experiments/phase4/evidence/linux-secure-confirm/)

## Implementation audit

| Area | Reviewed on the assembled application |
|---|---|
| Identity and authorization | Account routes use browser sessions and guard the native allauth flows. Rotated and expired sessions are covered by regression probes. Default roles use Django permissions. Consumer applications scope lists, counts and details separately. Login protection depends on routing administrative login through the managed account flow. |
| CSRF and XSS | Ninja's session authenticator checks CSRF on writes. The account API uses Django CSRF. Frontend text rendering uses text nodes. Managed files download as attachments with nosniff and sanitized display names. Deploy ingress body limits. File inspection validates accepted formats and dimensions. Malware analysis and streaming of large files through a production proxy require separate qualification. |
| SQL and admission | The custom PostgreSQL rate statement binds its inputs. Business queries use the ORM. Connection caps apply per worker. The AI ledger serializes admission and uses durable request keys. Hard spending controls depend on price, trustworthy token bounds and provider usage. Usage arriving above its reservation cannot protect the earlier estimate. Keep a stable secret for durable admission keys. |
| OTP and OAuth | The test matrix simulates delivery, bounded attempts, expiry, concurrent proofs and provider OAuth state. Google account linking requires reauthentication and a local account with explicit linkage. Direct Google registration remains outstanding. |
| Outbound HTTP | Configured origins are operator-owned. Paths reject absolute URLs and encoded traversal. HTTPX disables redirect following and proxy environment discovery. Configure network egress for deployments that permit operator-supplied or local origins. A trusted hostname still depends on trusted DNS and network routing. |
| Realtime | The handshake checks exact origins and a valid host. Per-socket authorization rechecks PostgreSQL session state. Queues and frames are bounded. A failed recheck shuts down delivery. Recheck completion can wait on the executor or pool, so the two-second polling interval alone cannot establish the five-second revocation gate under saturated load. |
| Secrets and logging | Structured logging picks fields by name, and the Uvicorn qualification runs suppress raw access URLs. Do the same at the reverse proxy and external telemetry layer. Keep API keys out of provider URLs where the provider supports a header. |

The first PyPI advisory scan flagged 42 advisories for Pillow 12.3.0. The new storage extra requires Pillow 13. The upload handler passes the configured allowlist to Pillow before decoding and tests rejection of a benign unsupported TIFF mislabeled as PNG. The release includes those controls for Windows and Linux without adding a required dependency to core-only applications. [Pillow 13 release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html) · [Pillow input security guidance](https://pillow.readthedocs.io/en/stable/handbook/security.html)

The follow-up scan of 61 exact dependency pins from the installed Windows candidate reported zero known advisories. The first scan could not audit the unpublished Smackend distribution from package metadata. The repository review and contract checks cover that code. Package advisory coverage does not include OS packages or native libraries embedded in wheels. [Before scan](experiments/phase4/evidence/python-audit.json) · [Patched pins scan](experiments/phase4/evidence/python-audit-a8-pins.json)

## Regression verification

- The installed a8 wheel passed **138 tests** against native Windows PostgreSQL in 260.76 seconds. Scope included the original auth, storage, provider and failure contracts plus a two-process competing spending reservation and the restricted decoder check. [Wheel run](experiments/phase4/evidence/wheel-regression.txt)
- The source-installed a8 application passed **21 integration tests** in 100.13 seconds with real PostgreSQL and simulated providers. [Source run](experiments/phase4/evidence/source-regression.txt)
- An assembled a7 project kept its existing Django session, account profile and private stored file after the a8 upgrade. All six `smackend verify` checks passed. [Upgrade run](experiments/phase4/evidence/upgrade.json)
- Generated all 64 flag subsets and executed application startup, Django checks and OpenAPI generation for all 26 distinct enabled profiles. Detailed PostgreSQL and provider contracts ran in the regression matrix. [Activation run](experiments/phase4/evidence/activation.json)
- Chromium exercised registration, ordinary chat, PDF-assisted streaming, authenticated WebSocket echo and bidirectional synthetic PCM through the installed candidate. The browser used the local development origin. [Chromium run](experiments/phase4/evidence/chromium.txt)

## Deployment and recovery

Ubuntu 24.04 on WSL2 started an isolated PostgreSQL 16.15 cluster and served an installed a8 application through a loopback HTTPS proxy in production configuration. PostgreSQL `verify-full` negotiated TLS and rejected a mismatched hostname. The HTTPS responses set Secure CSRF and session cookies, HttpOnly and SameSite=Lax on the session, and rejected a missing CSRF token. The direct plain HTTP health request redirected. The local proxy serves the qualification HTTP checks. Production ingress, WSS, certificate rotation and browser enforcement under production TLS remain open.

One Uvicorn worker was killed in the isolated environment. The supervisor showed a replacement process after 108 ms, and the authenticated download remained available. During the separate 30-second PostgreSQL interruption, all 25 liveness/readiness observations returned 200/503. The uncommitted write rolled back, the committed row survived, and readiness returned 467 ms after PostgreSQL reported startup. A PostgreSQL dump and copy of local media restored successfully into separate storage and a separate database. The original session retrieved the restored private file. Recovery with sockets open and provider backlogs during a mixed production workload remains unqualified. [Deployment evidence](experiments/phase4/evidence/deployment.json)

Docker and the Compose plugin were unavailable on this host. Ubuntu testing used unpacked PostgreSQL packages and a temporary native Python environment, so it did not exercise the Compose network, named volumes or PostgreSQL 17 Linux image.

## Consumer rehearsal

| Installed-package application | Initial workflow | Added application-owned feature |
|---|---|---|
| Shop | Owned inventory and an idempotent reservation for the last unit. Two concurrent buyers produced one successful reservation. | A buyer-scoped return request with its own table and migration. |
| Document assistant | Private file storage and filename-independent synthetic AI summaries. Another user could not request a private summary. | A reviewer assignment table and reviewer-only endpoint. |
| Expense manager | Owner-scoped expenses, totals and an authenticated session. | A shared budget table with explicit membership authorization. |

Each exercise generated an app from the installed wheel, applied native migrations and passed `smackend verify`. A feature model without its migration failed the drift check. Removing an owner filter and removing the session middleware separately failed application tests in the expense project. Verification passed after both were restored. All changes lived in the consumer applications. The recorded timings measure automated execution. Human development time and the ten-hour rehearsal remain unmeasured. [Consumer evidence](experiments/phase4/evidence/rehearsal.json)

## Reproduce the checks

1. From a clean environment use `uv build`, install the current wheel or sdist, and run `smackend init qualification --ai --realtime --storage --admin`. Run `python manage.py migrate` and `smackend verify` with a disposable PostgreSQL instance. [Django async middleware](https://docs.djangoproject.com/en/5.2/topics/async/) · [psycopg pool statistics](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)
2. On Ubuntu 24.04 with the a7 and a8 wheels in `dist/`, run the commands below from the repository root in one active Linux shell. A new evidence directory preserves the historical run. The PostgreSQL clusters use synthetic data and temporary local credentials. The historical middleware-only wheel can be selected explicitly with `SMACKEND_QUAL_COMPARISON_WHEEL`. A fresh checkout uses the patched wheel.
3. With Docker installed, in the installed package's fresh scaffold run `docker compose config --quiet`, `docker compose up -d --wait postgres`, `python manage.py migrate` and `smackend verify`. Create a database row and an owned file. Recreate only the database container with `docker compose up -d --wait --force-recreate postgres` and verify both through the original session. Keep the named database volume and the dedicated storage mount persistent. [Docker Compose startup](https://docs.docker.com/reference/cli/docker/compose/up/) · [Named volumes](https://docs.docker.com/reference/compose-file/volumes/)
4. For production set `DJANGO_ENV=production`, `DJANGO_DEBUG=false`, a generated secret, exact host and origin lists, and `POSTGRES_SSLMODE=verify-full` with the issuer in `POSTGRES_SSLROOTCERT`. Bind Uvicorn on a private interface and trust the ingress address only. At ingress overwrite the forwarded scheme and address, preserve the validated host, set ingress body and WebSocket limits, and disable buffering for SSE. Set `DJANGO_TRUST_PROXY_SSL_HEADER=true` only on the private backend. Verify Secure/HttpOnly session cookies, CSRF, invalid PostgreSQL certificate rejection, and HTTPS and WSS from a browser. Check certificate rotation and worker replacement. Run `pg_dump -Fc`, back up the file store while writes are stopped, restore both into an isolated environment, and verify owned downloads and database records.
5. Upgrade existing apps by changing the Smackend pin, preserving generated business logic and secrets, and running `manage.py migrate --plan`, then reviewed migrations and `smackend verify`. This candidate does not add a toolkit migration. Older generated apps consume runtime configuration through the installed package. Application-specific middleware and project settings remain under the application owner's control.

```bash
export SMACKEND_QUAL_EVIDENCE_DIR="$(mktemp -d)"
bash experiments/phase4/linux-local.sh bootstrap
bash experiments/phase4/linux-matrix.sh
bash experiments/phase4/linux-checks.sh soak
bash experiments/phase4/linux-followup.sh
bash experiments/phase4/linux-recheck.sh
bash experiments/phase4/linux-release-checks.sh
```

## Live integrations

| Integration | Local verification | Live qualification |
|---|---|---|
| Google OAuth | Simulated state, linked callback and account-linking checks. | Open. Direct Google registration remains outstanding. |
| Email and SMS | Deterministic delivery, expiry, replay protection and delivery failure. | Open. SMTP/SMS configuration, actual deliverability and abuse controls need the selected providers. |
| S3-compatible storage | Moto through the real SDK, plus persistent local-file restore. | Open. Vendor endpoint, bucket policy, recovery and cost behavior need qualification. |
| OpenAI, Gemini, Anthropic and compatible APIs | Synthetic wire responses, capability errors, streaming, cancellation and quota contention. | Open. Test actual model capabilities and reconcile usage before relying on spending controls. |
| Gemini Live | Simulated provider transport, plus authenticated browser PCM echo. | Open. Real model voice, microphone conditions, interruption and billing require live tests. |

The live-provider column remains open because no live runs were authorized. The Phase 4 suite used local and deterministic simulated data throughout.

## Release assessment

Keep `0.1.0a8` as an alpha. Local verification supports installation, modular startup, basic security behavior, app extensions, upgrades and isolated recovery. Worker-owned outbound client reuse was the strongest measured optimization. The 100-user soak met resource and error thresholds while missing all three CRUD latency targets. The 500-client echo gate and time-to-first-event gate also missed. Preserve the existing targets and rerun on the reference Linux PostgreSQL 17 deployment with a separate load host and production-style TLS ingress. Compose, provider qualification, mixed recovery, sustained real-time security and end-to-end production capacity remain release gates.

## Publication references

The [changelog](CHANGELOG.md) records the a8 changes. The [roadmap](ROADMAP.md#alpha-release-qualification) carries the remaining gates forward. The [contributor guide](CONTRIBUTING.md) lists the test environment variables.
