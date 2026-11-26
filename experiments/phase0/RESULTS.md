# Phase 0 validation record

25 November 2026 · Windows 11 environment reporting build 26300, Intel Core i5-11400H, 12 logical CPUs and 15.74 GiB RAM. Python 3.11.9 and uv 0.10.11. Docker was absent from PATH and the standard install location checked. WSL inventory was unavailable in the sandbox. A portable native PostgreSQL 17.9 server was used on loopback with SCRAM authentication, persistent local data and a 50-connection cap. It came from the [Zonky Windows PostgreSQL binaries](https://github.com/zonkyio/embedded-postgres-binaries). No Docker service or Windows database service was installed. The Compose manifest is provided for reproduction and its actual startup remains unverified.

## Resolved stack

| Component | Installed version |
|---|---|
| Django, Django Ninja, django-allauth | 5.2.19, 1.8.0, 65.22.1 |
| PostgreSQL, psycopg, psycopg-pool | 17.9, 3.3.9, 3.3.3 |
| Uvicorn, HTTPX, Argon2 library | 0.54.0, 0.28.1, 25.1.0 |
| pytest, pytest-django, Locust, psutil | 9.2.0, 4.14.0, 2.47.0, 7.2.2 |

The lockfile records exact transitive dependencies. Flask appears in the development install through Locust’s web UI dependencies. The application uses Django for HTTP, sessions, ORM and migrations.

## Functional and regression results

- Applied migrations to a new PostgreSQL database and generated the note and user migrations. After adding habits and the login budget, `makemigrations --check --dry-run` reported no changes.
- Baseline notes and account suite **8 passed in 5.21 s**. The first attempt hit pool exhaustion because pytest’s HTTP clients retained connections from concurrent login while waiting at the write barrier. The corrected test prepares independent authenticated clients before the barrier and concurrently performs the writes.
- After the habits migration and router were added, **9 passed in 5.00 s**. The original note ownership test failed at the expected assertion in the disposable negative control when user B’s list returned user A’s note. The actual app kept its scoping rule.
- A second virtual environment was installed from `uv.lock` and a fresh pytest PostgreSQL database was migrated. The final suite **passed 10 tests in 3.87 s**, including static asset MIME types. Django’s system check and migration drift check passed.
- Headless Chrome through Playwright CLI 0.1.25 passed registration, navigation, refresh, note CRUD, logout, a return to the sign-in page after logout, and a fresh login. The browser found an actual frontend bug during validation. A successful delete returned an empty 204 response, and the JavaScript tried to parse it as JSON before refreshing the list. The fix handles the empty response. An earlier browser attempt also needed an exact button selector to distinguish Edit from Cancel edit.
- In the live ASGI smoke, ten overlapping authenticated requests each received three simulated provider responses in **0.672 s total**. A notes read completed in **16 ms** while the deliberately slow call was still pending. The slow call returned 504, and an upstream 503 and malformed payload returned 502. The first live attempt yielded eight 504s during client startup. The succeeding run used the explicitly loopback HTTP-only simulator without per-request TLS certificate loading. These timings include authentication and client setup and do not measure a real provider.

## Locust characterization

Single ramp-inclusive run per level, with 5 new users/s and a planned 15 seconds at target concurrency. The configured total time limits were 17, 19, 25 and 35 seconds. Each simulated user registered with real Argon2 and received a browser session. The task mix was 60% notes list, 20% note create followed by delete, 10% provider fan-out and 10% session read, with 250–750 ms think times. The application ran with two Uvicorn workers and at most eight PostgreSQL pool connections per worker. The DB, app, simulator and load generator shared the same machine. The table uses the last Locust CSV snapshot. The terminal shutdown summary counted a few extra completed requests. All four runs exited successfully with zero recorded failures.

| Users | Requests | Requests/s | Aggregate p95 / p99 (ms) | List p95 | Create p95 | Provider p95 | Signup p95 | Peak sampled RSS app / DB (MiB) | Sampled CPU app / DB (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 359 | 22.25 | 170 / 400 | 39 | 60 | 190 | 440 | 457.7 / 276.2 | 10.92 / 1.12 |
| 20 | 770 | 42.57 | 170 / 320 | 51 | 47 | 220 | 350 | 572.2 / 311.8 | 21.03 / 1.25 |
| 50 | 1,903 | 78.81 | 410 / 740 | 350 | 350 | 600 | 860 | 693.9 / 386.2 | 56.73 / 4.44 |
| 100 | 3,576 | 107.85 | 640 / 830 | 590 | 580 | 840 | 820 | 609.4 / 370.9 | 102.47 / 6.95 |

RSS is a half-second process-tree sample summed across processes. It can double-count shared pages. CPU figures are sums of process CPU time accumulated between first and last samples and are approximate. App resources exclude the simulator and load driver. No DB connection high-water mark or per-route pool wait was measured in this run.

At 100 users, the measured list and create p95 exceeded the blueprint’s proposed 250 ms and 500 ms targets. The workload was short, local and ramp-inclusive, with a small dataset and no TLS proxy. It differs from the blueprint’s reference environment and cannot certify or rule out the reference envelope. An earlier Locust invocation exited before generating a CSV. The recorded rerun wrote the child process output to per-level log files. A later phase should separate registration load from steady-state traffic, instrument pool acquisition and event-loop lag, run longer on Linux/Compose, and repeat before attributing the higher latency to a particular component.

## Rate limits and PostgreSQL recovery

Two independently started Uvicorn worker processes pointed at the same PostgreSQL database. The driver fired 100 simultaneous login attempts, 50 per worker, using canonical-equivalent username variants and a common HTTP Host value. Fresh keys were used for each profile. The following observations describe one controlled run per profile.

| PostgreSQL-backed profile | Incorrect-password results | Limit responses | Unrelated correct login |
|---|---:|---|---:|
| allauth database cache only | 10 | 61 allauth rate-denied validation responses (400) and 29 HTTP 429s without Retry-After | 200 |
| Atomic login counter in isolation | 10 | 90 HTTP 429s, all with Retry-After. One stored window row with count 10. | 200 |

The first cache comparison accidentally used distinct Host values, which keyed two separate allauth budgets. It was discarded as a comparison of a single identity budget. The first combined run also showed that allauth’s outer cache protection can return a 429 before the atomic hook and omit Retry-After. The isolated atomic profile disables those outer login checks only for the probe. The default app retains both checks. The corrected test establishes cross-worker enforcement for these attempts and configurations. It does not establish behavior at a window boundary, under cache eviction, under high identifier churn or under production-scale concurrency. [allauth rate-limit requirements](https://docs.allauth.org/en/latest/common/rate_limits.html)

For the restart probe, a signed-in client saved a note, then the isolated PostgreSQL process was stopped while both app workers kept running. `/ready` and the authenticated note read returned **503**, while `/live` returned **200**. The three outage probes took **8.047 s combined**, including two bounded waits for a connection. From the start command, readiness and the same authenticated note read recovered in **4.438 s**. The saved record and existing session survived. The probe exercises a read across restart and does not establish correctness of a write interrupted mid-commit.

## Focused performance audit

The audit used the same Windows/localhost topology, synthetic accounts, PostgreSQL 17.9, two Uvicorn workers and an eight-connection pool in each worker unless specified. The matched Locust runs used 100 users, a 5 users/s signup ramp, the original mixed workload and a 38-second limit. The reported steady slice starts 23 seconds after the driver starts and spans about 14–15 seconds. Signup still runs with real Argon2 during ramp. The independent HTTPX driver uses the same task weights and think-time range with its own scheduling and a seeded action order. The runs characterize this workstation and dataset.

The opt-in ASGI probes measured request time through response headers, time at the sync/async boundary, time before a worker thread started, application SQL execution time by table group, connection checkout and hold times, and a 25 ms event-loop heartbeat. Checkout includes the pool's liveness check. SQL wall time includes driver activity and scheduling on this host. The PostgreSQL plans below time execution inside the server. Neither type of SQL timing includes the wait to obtain a pool connection. Per-route percentiles come from different distributions and should not be added. Worker CPU and peak RSS/threads are sampled process-tree figures with the same shared-page limitation as above. [Django database instrumentation](https://docs.djangoproject.com/en/5.2/topics/db/instrumentation/) · [psycopg pooling](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)

**SQL plans and session cost.** Each notes list made three queries: one DB session lookup, one user lookup and one owner-scoped notes query. Per-request p95 execution in the first steady baseline was 6.5 ms for session SQL, 7.0 ms for user SQL and 6.3 ms for notes SQL. Total SQL p95 was 16.3 ms. PostgreSQL itself executed the quiet indexed session, user and notes lookups in 0.030, 0.039 and 0.027 ms respectively. A separate temporary table with 100,000 synthetic notes took 0.441 ms to return 100 rows for one owner using an index-backed bitmap scan and a small sort. The original load still used the small notes table. Those measurements do not justify changing indexes or weakening session checks.

**Connection contention.** The two mixed baselines put list checkout p95 at 242–270 ms, with 16 connections configured across the two workers. The first baseline sampled 16 sessions at peak, while some samples showed all 16 waiting on the client. A provider request used just two identity queries yet held its loaned connection for 379–414 ms at p95, including outbound HTTP. PostgreSQL can see a connection as idle while Django has it reserved. Replacing provider calls with protected list calls cut list checkout to 104 ms and list p95 to 371 ms. That control raises throughput and changes the workload, so it helps identify the source of contention without serving as a drop-in speed comparison.

**Thread and worker utilization.** The baseline list took a median 11 thread handoffs. Its p95 executor dispatch wait was 21–23 ms in total. In baseline B, the p95 summed sync-to-async call time was 478 ms, versus 259 ms spent inside the executor callbacks. Some callback time was the checkout wait above. Calculating the residual per request gave 195 ms p95 outside executor wait and execution, which includes the time needed to resume the awaiting coroutine between handoffs and bridge bookkeeping. In the larger pool run, lowering the checkout delay still left 538 ms p95 bridge time and greater heartbeat lag. This is evidence of Python-side scheduling and contention. The traces do not identify a particular Windows kernel, GIL or Django internal function as the sole remaining cause. [Django async request stack](https://docs.djangoproject.com/en/5.2/topics/async/)

**Driver control.** In baseline A the two workers handled 1,043 and 1,016 steady responses. The app process tree used 111.4 CPU seconds and peaked at 500 MiB RSS and 97 threads over 39.9 seconds. PostgreSQL used 8.6 seconds and peaked at 391 MiB. Locust used 13.3 CPU seconds and peaked at 84 MiB. Its p95 client time outside ASGI was about 40 ms for lists. HTTPX reached 142.5 requests/s versus Locust's 142.9, and still missed the read target at 450 ms. HTTPX used 32.6 CPU seconds over 39.8 seconds. Against the lightweight simulator alone, Locust sustained 575.7 requests/s across the 12-second run, with 6,693 requests and zero failures. That control tests driver headroom without Django or PostgreSQL in the path. This evidence points away from Locust saturation as the primary explanation. The driver still affects the arrival pattern and shares CPU with the server. A separate load host remains part of qualification.

### Before and after

The first candidate returned the DB connection immediately after Django’s normal async session lookup. The second combined candidate resolved the same Django session identity and returned the connection in one synchronous call, then skipped an unnecessary thread handoff for two ordinary lab response headers. The view still awaited the provider asynchronously. DB sessions, password hashing, CSRF, scoped queries, timeouts and rate limits were the same throughout. The checkout health check remained enabled. The wide-pool probe changed only the connection allowance relative to the first candidate.

| 100-user Locust mixed run | Steady requests/s | Notes list p95 (ms) | Create p95 (ms) | Provider p95 (ms) | Provider DB hold p95 (ms) |
|---|---:|---:|---:|---:|---:|
| Baseline A | 142.9 | 543 | 553 | 817 | 379 |
| Baseline B | 148.4 | 537 | 500 | 791 | 414 |
| Early return A | 132.1 | 614 | 599 | 921 | 79 |
| Early return B | 164.8 | 312 | 298 | 551 | 55 |
| Early return + 12 connections/worker | 116.0 | 628 | 610 | 972 | 129 |
| Combined A | 109.9 | 696 | 692 | 1,068 | 24 |
| Combined B | 145.5 | 486 | 497 | 757 | 16 |

Early return directly reduced the time the provider reserved a connection. The combined change reduced median list handoffs from 11 to 10 and provider handoffs from 12 to 10. Neither outcome made the mixed latency improvement repeatable. The wider pool cut checkout delay relative to baseline and grew sampled DB RSS from about 391–393 MiB to 540 MiB without improving request latency. Its 24 application connections would also leave little headroom at rollout overlap against this lab’s 50-connection cap. One early-return run recorded one provider timeout during ramp. All steady Locust slices above recorded zero HTTP failures.

To control the feedback from response times, HTTPX then scheduled **120 mixed user actions per second** after the signup ramp. A create action includes a follow-up delete. Each user waited for its previous action to finish, so the driver reported late starts and actions missed at the end of the window. Request latencies below are measured from actual send time and exclude the separate action-start delay. The run order was baseline, candidate, candidate, baseline.

| Paced run | Completed requests/s | Notes list p95 (ms) | Create p95 (ms) | Provider p95 (ms) | List action-start delay p95 (ms) | Missed actions |
|---|---:|---:|---:|---:|---:|---:|
| Baseline A | 131.8 | 802 | 845 | 989 | 1,548 | 81 |
| Combined A | 123.3 | 985 | 985 | 1,294 | 1,673 | 146 |
| Combined B | 128.0 | 861 | 925 | 1,134 | 1,475 | 112 |
| Baseline B | 127.4 | 839 | 884 | 1,066 | 1,740 | 125 |

Each paced run recorded one failed provider request without an ASGI timing sample. The saved samples cannot distinguish a transport error from a response that bypassed app timing. The combined variant again held the provider connection for only 20–21 ms at p95, yet list latency was higher in both repeated comparisons. High action-start delays show that this offered workload built a backlog. Driver CPU was roughly 16–17 seconds over 38–39 seconds in the sampled runs. Both early-return modes and the inline-header variant remain available for local profiling. The experiment defaults to the original request path and the eight-connection pool. The measurements establish pool contention and thread handoff overhead as contributors to the original miss. Neither tuning change qualified as a latency fix.

**Reverification after the changes.** The combined variant passed all ten PostgreSQL tests, Django system checks and migration drift checks. Its disposable negative control failed at the expected ownership assertion. The Chrome signup/navigation/CRUD/logout/login journey passed. The live ASGI smoke checked that the provider route denied unauthenticated clients before login and after logout. Ten concurrent authenticated provider requests finished in 0.625 s, and a notes read completed in 16 ms during a deliberately timed-out call. The two-worker atomic limiter again admitted ten of 100 attempts and returned 90 rate-limit responses with Retry-After. PostgreSQL restart again preserved the session and saved note, with 0.875 s to recovery from the start command in that run. The default configuration also passed all ten tests and the live ASGI smoke after the audit.

**Recommended qualification run.** Start with Linux/Compose on the stated resource envelope and an external load generator. Measure authenticated CRUD alone across fixed arrival rates, then add provider calls and admitted logins separately before combining them. Compare pool availability and post-query thread handoff time on each route. Use a production-like dataset, run long enough to observe steady behavior, track dropped arrivals and test outage/recovery under load. If the mixed run still misses its targets, test provider connection reuse and reducing measured handoffs one change at a time. Keep the owner and authentication contracts fixed across all experiments.

### Coverage limits carried forward

| Area | Evidence already recorded | Original condition still untested or only partly tested |
|---|---|---|
| Installation and persistence | Locked clean Windows install and fresh PostgreSQL migrations. Native restart preserved sessions and notes. | Docker Compose startup, restart and container recreation against the named volume. Linux reference setup and HTTPS/proxy behavior, including browser enforcement of Secure cookies. |
| Auth, sessions and row access | Real login/logout, invalid credentials, case-equivalent concurrent signup, password/disable revocation, database expiry, CSRF, and two-user owner isolation. | Natural eight-hour wall-clock expiry and writes already in flight at session revocation. Auth and ownership were tested in separate assertions, not checked after every operation in the 100-user workload. |
| Concurrency and load | Original 10/20/50/100-user ramp runs, a duplicate-signup barrier test, concurrent creates, 100-attempt invalid-login burst and the focused 100-user audit. | Sustained 5 admitted successful logins/s independent of registration. Concurrent conflicting edits/deletes of the same note, writes interrupted during commit, 30-minute soak and saturation/recovery beyond 100 users. |
| PostgreSQL rate limiting | Same-host, two-worker tests of allauth’s shared DB cache and the isolated atomic per-key budget. | Repeatability across minute boundaries, mixed IP/identifier churn, cache eviction or PostgreSQL loss in the admission path. The exploratory burst is not a global hash-admission limit. |
| Provider and failure isolation | Normal, malformed, timed-out and failed synthetic HTTP responses. Notes responded while one provider request timed out. | Sustained upstream failure under 100-user mixed traffic, connection refusal and restart as separate injected failure modes. The simulated calls do not qualify a real provider or streaming workload. |
| Regression and comparison | Existing tests passed after adding habits. Expected failure in a disposable copy confirmed the notes test detects a broken owner scope. | Timed developer comparison with stock Django, mentioned in the blueprint Phase 0 gate. Production migration upgrade/rollback, high availability, Channels and voice remain outside this lab. |
