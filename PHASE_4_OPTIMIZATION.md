# Phase 4 performance remediation

This follow-up starts from the published a8 checkpoint, `0d45cf8`. The
[Phase 4 report](PHASE_4.md) and its evidence describe the original trials.
The Django, Ninja, PostgreSQL, allauth and optional Channels architecture
continues to own the same application contracts.

## Measurement changes

- The driver counts operations completed within the offered window and
  completions during drain separately. For paced workloads it records missed
  offers by operation and reason. A compound operation can use more than
  one HTTP request, so throughput is reported as completed operations per second.
- Sample serialization and gzip run after the measurement window and drain.
  In-memory sample collection and application-level validation still contribute
  client CPU. Keep raw and summary output outside the measured server's disks
  for reference qualification.
- Use `--trace` to record HTTPX transport milestones, connection setup when
  performed, client response headers and body read completion, and operation
  end. Request scheduling lateness remains a separate field. The interval
  before the first HTTP transport event can include client connection assignment
  and event-loop scheduling. The optional `--wire-timing` probe separately
  measures HTTPX's wait for connection assignment and Uvicorn H11's time
  from parsed headers to ASGI dispatch. These probes depend on private
  interfaces of the pinned HTTP clients and test server. Socket acceptance
  before parsing and one-way wire timing still need an instrumented ingress.
- Use `--profile` to pair the client result with an ASGI-to-headers sample,
  SQL execution by table group, executor timing, checkout and lease timing,
  plus psycopg availability and waiters observed before checkout. Checkout
  includes the configured health check. Lease timing at response headers
  omits later cleanup and any work performed while streaming. Ingress accept
  and proxy queues require a separate timed proxy or server trace. Client and
  server intervals should be correlated per request. Percentiles from
  separate populations cannot be subtracted to infer a stage's percentile.
- The workload tracks acknowledged updates per synthetic owner, checks
  aggregate totals and compares final values with PostgreSQL. Anonymous
  reads and access to another actor's private file are checked before load.
  A failed integrity check causes the runner to fail.
- `django-read`, `ninja-read` and `read` run the same authenticated list
  query and result shape. They compare plain Django response serialization,
  a native Ninja API and the Smackend-configured API inside the same project
  middleware and database settings. They do not estimate the startup or
  middleware overhead of three separately deployed services.

## Outbound HTTP lifecycle

The generated ASGI application wraps its final Django or Channels instance
when outbound HTTP is enabled. At startup Smackend creates event-loop-owned
clients and hands them to request scopes through ASGI lifespan state. At
shutdown it closes the clients, including any initialized before a partial
startup failure. Uvicorn's normal graceful shutdown drains application work
before lifespan shutdown. Operator shutdown timeouts still govern how long
streams can drain.

Use `async with outbound_client("name") as client` from
`smackend.outbound.asgi`. The context borrows a worker client without closing
it. It owns a temporary client for callers outside the managed ASGI scope.
Built-in wire AI adapters receive managed clients through their existing
service injection point. AI transport variants use separate clients where
headers or retry rules differ and share the named-provider admission budget.
Each provider name has its own credential boundary. Async outbound clients
discard upstream cookies. Explicitly constructed `AsyncClient` objects and
custom adapters keep ownership of their transports. Limits count concurrent
upstream calls per provider per worker, including each call in a fan-out.
The lifetime and limit of a client started with lifespan disabled are local
to that invocation.

Existing applications can add the following after assembling their final
ASGI application and run Uvicorn with `--lifespan auto` or `--lifespan on`.

```python
from django.conf import settings

if settings.SMACKEND_HTTP_ENABLED:
    from smackend.outbound.asgi import with_outbound

    application = with_outbound(application)
```

## Reproduce local comparisons

Use separate virtual environments with the same dependency pins. Install the
published a8 wheel in one and the updated distribution in the other. With
both environments, use a disposable local PostgreSQL cluster, supply
`SMACKEND_SMOKE_DSN` for its administrative database, and install the full
set of extras plus Uvicorn and psutil. Run from the repository root. Every
server run needs fresh scratch and evidence directories.

```text
python experiments/phase4/driver_control.py
python experiments/phase4/run.py --scratch <new-dir> --evidence <new-dir> --users 40 --profile --trace --matrix external:30:10:25 ai:30:10:20 stream:30:10:15 mixed:30:12:35 django-read:30:6:25 ninja-read:30:6:25 read:30:6:25 persistent:40:10
```

Use the same command with `--managed-outbound` on the updated distribution.
Supply `--database-pid` for PostgreSQL process telemetry. The curl calibration
compares 1,500 requests at up to 32 concurrent transfers against HTTPX with
32 closed-loop workers on the same simulator. This bounds a local burst
comparison. It does not qualify paced mixed traffic on a separate generator.

Add `--wire-timing` for the targeted HTTP transport probe,
`--ledger-rows 50000` for an already populated AI ledger,
`--file-bytes 2097152` for private download memory measurements, and compare
`persistent` with `reconnect` while varying `--socket-stagger`. File fixtures
increase disk usage in proportion to the seeded user count.


## Matched Windows pilot

The control ran the installed published a8 wheel. The candidate ran the
updated source with the same installed dependencies. Both used Windows,
Python 3.11.9, loopback PostgreSQL 17.9, two Uvicorn workers, eight pool
connections per worker, seeded owners and the same driver and local provider.
Each scenario ran once per variant, control first. See the
[sanitized trial summaries](experiments/phase4/remediation-results.json). The short runs characterize
these workloads on this host. The setup did not use TLS or independent hosts.

| Operation and offered rate | a8 valid / offered | Managed valid / offered | a8 p95 (ms) | Managed p95 (ms) |
|---|---:|---:|---:|---:|
| Three-way external fan-out, 25/s | 78 / 250 | 250 / 250 | 5,653 | 169 |
| AI completion, 20/s | 90 / 200 | 200 / 200 | 4,210 | 97 |
| SSE first event, 15/s | 85 / 150 | 150 / 150 | 3,482 | 152 |
| Mixed protected read, 35/s overall | 403 / 420 overall | 420 / 420 overall | 546 | 29 |
| Mixed protected write | same mixed run | same mixed run | 556 | 36 |
| Mixed protected aggregate | same mixed run | same mixed run | 489 | 25 |

Fan-out completed 58 requests in-window and 20 during drain on a8.
The managed version completed 248 in-window and two during drain. The
measured interval inside ASGI fell from 5,451 to 167 ms at p95. The
control used 234 distinct upstream connections for 234 calls, and the
managed run used 14 connections for 750 calls. The two policies served
different percentages of offered traffic, so the latency ratio describes
the observed completed populations. It is also consistent with the
published [isolated, equal-completion crossover](PHASE_4.md#outbound-connection-reuse).
Both new runs passed the dataset and ownership checks.

The no-DB simulator completed all 1,500 requests with both generators
at up to 32 concurrent operations. In the final clean calibration, curl
wall time was 0.312 s with 6.85 ms p95. HTTPX wall time was 9.406 s with
562 ms p95. The result includes differences in scheduling, connection
handling and timing boundaries between the two clients. HTTPX remains
useful for cookies, SSE and sockets. Its local timings require an
independent cross-check. An earlier invocation measured similar results
and failed during Windows temporary log cleanup. The clean rerun verified
the process cleanup fix.

### Pool and executor observations

The a8 fan-out p95 experienced about 300 ms of checkout time, 2,092 ms
of database lease time through headers and 20.1 ms of measured executor
dispatch wait. The managed workload measured 0.4, 2.6 and 2.3 ms,
respectively. Both variants explicitly release their resolved auth
connection before external HTTP. The longer a8 leases are consistent with
concurrent checkout and worker scheduling delays around that release.
In the mixed read sample, checkout p95 was 3.4 ms while lease p95 was
425 ms on a8. That combination points toward time spent with a checked-out
connection between stages, without identifying the exact scheduler delay.
Managed values were 0.4 and 5.9 ms. A wider pool is not justified by this
pilot.

The isolated protected Django, Ninja and configured API reads all
completed 150/150 at 25/s in both runs. Client p95 was 29/29/29 ms in
the a8 run and 30/34/37 ms in the candidate run. Server p95 was about
10–12 ms in both. These short, sequential comparisons provide no
evidence that replacing Ninja would materially address the loaded tail.


## Remaining bottlenecks

| Probe | Local observation | Decision |
|---|---|---|
| AI global lock and aggregation | At 20/s, both fresh and 50,000-row ledgers returned all 240 offers. AI p95 rose from 97 to 119 ms. SQL grouped under the ledger rose from 3.34 to 15.32 ms p95. The measured advisory lock statement went from 0.45 to 0.61 ms p95. | Repeated aggregation has a measured volume cost. Keep atomic admission and the durable ledger. Test a representative day and peak mixed concurrency before considering counters or schema changes. |
| Socket admission and reconnects | The 40-socket hold saw 94 ms handshake p95 with 3 ms spacing and 31 ms with 100 ms spacing. The slower-ramp reconnect run completed 178 echoes at 31 ms handshake p95. | Pace reconnects and profile admission at 500 held sockets in the reference environment. The pilot changes both the ramp and socket profiling, so the handshake difference is diagnostic. |
| Periodic authorization and Channels cleanup | Across the staggered hold, reconnect and 20-actor duplex trials, 278 socket profiles recorded 819 session lookups, 819 user lookups and 6,974 dispatch-cleanup calls. Cleanup spent 10.69 elapsed seconds summed over sockets, including async dispatch and waiting. The duplex trial checked 6,000 frames with 16 ms sampled RTT p95. | The cleanup bridge runs much more often than authorization queries. Keep revocation and cleanup. Isolate per-message executor waiting at sustained full-rate load before changing the receive path. |
| Private downloads | All 150 responses passed at 15/s for both 4 KiB and 2 MiB. Client p95 was 33 and 44 ms. Sampled app RSS peaks were 193 and 200 MiB, respectively. Both trials used fast loopback clients. | Repeat with slow readers, larger concurrency and production ingress. Django's synchronous file iterator can buffer under ASGI. This pilot does not justify changing private delivery semantics. |

The file and socket follow-ups used later driver revisions to add optional
instrumentation and validation. Environment records preserve the driver
hashes. The matched a8 and candidate comparisons used the same driver hash.
The Windows Python 3.11 client clock is coarse at the scale of a local echo,
so millisecond-sized differences need a finer timing control.

The original Linux PostgreSQL 17 deployment, separate load generator and
provider simulators, HTTPS/WSS, 30-minute mixed load, 500 held sockets,
revocation under stress, live-provider behavior and failure/recovery gates
remain open. Keep measurements from this pilot out of production capacity
claims. The next meaningful capacity test is a repeated off-host protected
CRUD baseline with matched pacing, followed by the original mixed-load
contract. Base any further runtime changes on the queue observed in those
runs.


## Verification

A 16-request protected read smoke recorded a correlated client pool
assignment, Uvicorn header-parse-to-dispatch time, ASGI and database
timing, and client response-body completion for all 16 requests. The
first full regression run detected an existing introspection contract
that the new wrapper initially broke. The wrapper now preserves the
underlying Channels application's metadata, and the lifecycle regression
test checks that contract.

The corrected implementation passed all 140 regression tests against a
disposable PostgreSQL 17.9 cluster. Package metadata was then advanced
to `0.1.0a9`. This remains an alpha.
The offline lock check, wheel and source builds, separate local installs,
and generated AI and Channels projects passed their package and Django
system-check smoke tests.
