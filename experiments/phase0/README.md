# Phase 0 · Private notes foundation experiment

This standalone Django project validates the stack selected in [the framework evaluation](../../FRAMEWORK_EVALUATION.md). See [RESULTS.md](RESULTS.md) for measured outcomes.

## Run locally

Prerequisites are Python 3.11 or 3.12, uv and a running Docker Compose installation. Commands here use PowerShell from this directory. The browser profile binds to loopback and uses HTTP. Use synthetic accounts and notes.

```powershell
python setup_env.py
uv sync --locked --python 3.11
docker compose up -d --wait postgres
uv run --locked python manage.py check
uv run --locked python manage.py migrate
```

`setup_env.py` creates `.env` with a random PostgreSQL password and Django signing key. Keep that file private. The Compose named volume persists through `docker compose stop` and `docker compose down`. Changing the password in `.env` does not change a role initialized on an existing volume. Use a different port in `.env` if 55432 is occupied. Keep saved local credentials together with their volume.

Start the provider in one terminal and the Django app in a second.

```powershell
uv run --locked uvicorn provider_stub:application --host 127.0.0.1 --port 8011 --lifespan off
```

```powershell
uv run --locked uvicorn lab.asgi:application --host 127.0.0.1 --port 8010 --workers 2 --lifespan off --no-proxy-headers
```

Open [Private notes](http://127.0.0.1:8010/). Register a new username and a synthetic password of at least eight characters that passes Django’s validators. Confirm the browser reaches the notes page, create a note, refresh, follow the account-page link and return to notes. Edit and delete the note. Try the simulated provider. Sign out, then follow the link to notes again. To look for isolation manually, register a different user in a private window. That user’s list starts empty.

The habits feature uses the same login session. Its endpoints are `GET /api/habits/`, `POST /api/habits/` with `{"label":"Walk"}`, and `POST /api/habits/{id}/repeat`. Browser writes require the current `csrftoken` cookie value passed as an `X-CSRFToken` header.

## How a request works

In Flask you might set up the session and database in your application factory. Here Django’s settings specify those parts, Django’s URLconf connects paths to views, and Uvicorn passes HTTP requests to Django through ASGI.

1. The sign-in page asks Django to set a CSRF cookie. JavaScript sends the token in a header to one of the allowlisted allauth browser endpoints. Django checks CSRF first. Allauth validates credentials, delegates hashing and account storage to Django, and performs login. Django rotates session/CSRF state and stores the session in PostgreSQL. The session cookie is an opaque key and is HttpOnly.
2. Navigation and refresh send the cookie again. Session middleware and authentication middleware reconstruct the user from PostgreSQL. Ninja’s cookie authenticator checks CSRF for writes and gives handlers the verified user as `request.auth`. A note ID alone conveys no authority. Every detail/read/update/delete lookup starts with the caller’s queryset, and the server derives ownership at create time. Django ORM constructs parameterized queries and psycopg runs them. The response schema selects the fields sent back to the browser.
3. The provider route authenticates through Django’s async user lookup and awaits three calls to the local simulator. It has an overall deadline plus HTTPX operation timeouts. No transaction spans the external call. The per-request DB connection acquired by Django for authentication may stay checked out until request teardown. Check pool contention under mixed load before reusing this pattern on long-lived streams.
4. Habits adds a new Django app and router. Its only shared domain reference is the configured user model. Its increment is one PostgreSQL update, with the ownership condition and arithmetic in the same statement. The source for auth and notes stayed in place when the feature was introduced.

## File guide

| File | Role |
|---|---|
| `pyproject.toml`, `uv.lock` | Python version range and declared dependencies, plus exact transitive versions and artifact hashes. |
| `.env.example`, `setup_env.py`, `.gitignore` | Local settings template, credential generator and ignored files. |
| `compose.yaml` | Loopback PostgreSQL 17, persistent volume, health check and a 50-connection server cap. |
| `manage.py`, `lab/asgi.py` | Django management command entry point and Uvicorn entry point. |
| `lab/settings.py`, `lab/urls.py` | Middleware, cookie and pool policy, installed apps, browser/account allowlist and Ninja router registration. Only the three experiment account routes are mounted. |
| `lab/views.py` | HTML/asset responses, CSRF failure response, sanitized DB failure handling, worker ID header, liveness and DB connectivity readiness. |
| `accounts/models.py`, `accounts/adapter.py` | Custom user class, SQL canonical-name constraint, allauth signup validation and before-login admission hook. |
| `accounts/rate_limit.py` | Parameterized atomic budget update. PostgreSQL time assigns the minute bucket. An HMAC hides the username in the budget table. |
| `accounts/migrations/` | Reproducible user, allauth cache and budget schemas. |
| `notes/models.py`, `notes/api.py`, `notes/migrations/` | Note table, per-owner index, input/output schemas and owner-scoped CRUD. Edits lock the targeted row inside a short transaction. |
| `habits/models.py`, `habits/api.py`, `habits/migrations/` | Independent regression feature, its table and its endpoints. |
| `lab/provider.py`, `provider_stub.py` | Async Ninja handler and deterministic local upstream with success, slowness, failure and malformed responses. |
| `pages/login.html`, `pages/login.js` | Account form and account lifecycle calls. |
| `pages/notes.html`, `pages/notes.js` | Notes form, list, editor, logout and provider demo. Note text is written with `textContent`. |
| `pages/browser.js`, `pages/lab.css` | Read the newest CSRF token for each write, display errors and provide basic layout. |
| `tests/test_contracts.py`, `negative_control.py` | Integration suite and disposable-source ownership regression. |
| `live_checks.py`, `locustfile.py`, `failure_checks.py`, `browser_smoke.js` | Local Uvicorn smoke, Locust load, worker quota/recovery probes and optional Chrome browser flow. |
| `performance_audit.py`, `profile_load.py` | Matched benchmark runner, process and PostgreSQL sampling, quiet query plans and independent HTTPX load driver. |
| `lab/profile_asgi.py`, `lab/profile_db/base.py` | Opt-in, loopback-only audit hooks for timing thread scheduling, SQL and connection use. Request data and SQL values are excluded from the timing headers. |

## Verify and troubleshoot

Stop the manually started Uvicorn processes before running the live harness, which starts its own processes on 8010 and 8011.

```powershell
uv run --locked python manage.py makemigrations --check --dry-run
uv run --locked pytest -q
uv run --locked python negative_control.py
uv run --locked python live_checks.py
uv run --locked python live_checks.py --load --seconds 20
uv run --locked python failure_checks.py
```

- **Auth lifecycle** checks Argon2, session persistence, correct and incorrect credentials, logout, reuse of a logged-out session key and a new key at login.
- **Session invalidation** checks expiration, account disablement and a changed password independently.
- **Duplicate and invalid registration** checks mixed-case collision and rejected username syntax. **Concurrent registration and note creation** checks one canonical user after simultaneous signup and exactly ten notes after simultaneous authenticated writes on independent connections.
- **CSRF and anonymous writes** exercises unsafe verbs, absent proof, foreign Origin and anonymous access through Django’s assembled middleware stack.
- **Note CRUD and ownership** uses two real users and checks list/detail/update/delete isolation plus rejection of a client-supplied owner.
- **Habits coexistence** checks the new feature’s owner boundary and confirms that the same session still reads prior notes in the fully assembled app.
- **Negative control** copies the app into a temporary directory, removes the notes scope filter there, runs the existing ownership test against a separately named pytest PostgreSQL database, and accepts only the expected assertion failure. The working tree is never edited by the script.
- **Live smoke** exercises Uvicorn, real HTTPX networking, ten concurrent authenticated provider requests and an independent notes request while the simulated provider times out. It also checks upstream errors and malformed JSON. **Load** uses distinct synthetic accounts and reports the full ramp-inclusive run. The optional `--db-pid` samples native PostgreSQL RSS and CPU time. The scripts write local CSV, JSON and server logs under `results/local/`.
- **Rate probe** compares the shared Django database-cache profile with the atomic fixed-minute counter. Use a quiet lab database. **Restart probe** is available with `--restart-portable` only for the isolated Windows data directory `.local/pgdata`. With Compose, keep the app running, create a note, run `docker compose stop postgres`, check `/live`, `/ready` and the authenticated note request, then run `docker compose up -d --wait postgres` and read the same note.
- **Chrome smoke** checks registration, JavaScript CRUD, refresh, navigation back to the notes page, logout and login. With the app running, use `npx --yes --package @playwright/cli@0.1.25 playwright-cli -s=phase0 open http://127.0.0.1:8010/ --browser chrome`, then the same prefix through `-s=phase0` with `run-code --filename=browser_smoke.js`. Finish with `close` on that session.

If startup fails, inspect `docker compose ps`, `docker compose logs postgres` and `python manage.py showmigrations`. Readiness checks connectivity only. Migrate before serving requests. A 401 on notes means there is no valid session. A 403 on writes usually means the CSRF token is missing, stale or from the wrong origin. Keep the same host spelling on both pages. Check the browser Network panel for the actual response. A 404 for another user’s ID is intentional. A 503 means the DB is unavailable or the pool wait expired. Look in the Uvicorn log for pool warnings. Do not log request bodies, cookies or secrets.

### Experimental boundaries

The local database role creates and migrates tables and test databases. A production deployment needs least-privilege roles and separate migration credentials. The demo uses direct loopback HTTP, no recovery email nor admin interface, an eight-hour default DB session expiry and no cross-origin profile. Recovery and authentication fail closed on DB unavailability when handled by the tested paths. PostgreSQL pool ceilings apply per process. Note updates serialize by row and use last-write-wins. Concurrent replay after an ambiguous commit would need a command ID and idempotency policy.

The fixed-window limiter is per canonical identifier, counts successes as well as failures, allows another batch at a minute boundary and creates rows for unknown identifiers. The lab omits automatic pruning and a production IP/admission budget. Before public use, establish cardinality limits, retention and an aggregate hash-admission cap. Allauth’s signup cache limit is permissive for the loopback benchmark. Test thresholds, rates and resource samples from this machine do not establish production capacity.

### Reproduce the performance audit

Run against a disposable lab PostgreSQL cluster with Uvicorn stopped. Use the local postmaster PID from `postmaster.pid` for native PostgreSQL process sampling. The audit starts its own app and simulator, generates synthetic users, and writes per-request samples and summaries to `results/local/audit-*`. Use distinct labels for each run.

```powershell
$pgPid = [int](Get-Content -LiteralPath .local/pgdata/postmaster.pid -TotalCount 1)
$env:PHASE0_PROVIDER_RELEASE_DB = '0'
$env:PHASE0_INLINE_HEADERS = '0'
uv run --locked python performance_audit.py baseline --db-pid $pgPid
$env:PHASE0_PROVIDER_RELEASE_DB = 'sync'
$env:PHASE0_INLINE_HEADERS = '1'
uv run --locked python performance_audit.py candidate --db-pid $pgPid
Remove-Item Env:PHASE0_PROVIDER_RELEASE_DB,Env:PHASE0_INLINE_HEADERS
```

The value `1` for `PHASE0_PROVIDER_RELEASE_DB` uses the async Django session resolver followed by an explicit connection return. The `sync` variant does the standard synchronous session lookup and returns the connection in the same thread call. `PHASE0_INLINE_HEADERS=1` moves the lab's response-header assignment onto the calling thread and retains the same exception mapping and security middleware. Keep audit modes local and review measured tradeoffs in [RESULTS.md](RESULTS.md).

Add `--driver httpx --offered-actions 120` for a paced check after signup, `--crud-only` on the Locust driver for the no-provider control, or `--pool-size 12` for the wider-pool probe. For a redacted execution plan inspection on a populated lab DB, run `uv run --locked python performance_audit.py plans --plans-only`.

To reproduce the driver headroom control, start the simulator on port 8011, set `PHASE0_DRIVER_HEADROOM=1` in the load-driver terminal and run `uv run --locked locust --headless --only-summary --host http://127.0.0.1:8011 -u 100 -r 25 -t 12s DriverOnlyUser`. Remove the environment variable after the run.

## Later qualification

The [Phase 4 report](../../PHASE_4.md) covers measurements of the installed toolkit. The [Phase 0 results](RESULTS.md) preserve the historical baseline.
