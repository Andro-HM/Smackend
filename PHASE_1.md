# Phase 1 toolkit contracts

## Models and migrations

The generated project owns `accounts.User`, an ordinary Django `AbstractUser` subclass. `accounts/0001_initial` creates it before app migrations depend on it. Add application models in regular Django apps. Use `settings.AUTH_USER_MODEL` for a model relationship and `migrations.swappable_dependency(settings.AUTH_USER_MODEL)` for its migration dependency. Use `get_user_model()` in running Python code.

Decide deletion behavior for each foreign key. Use PostgreSQL constraints for invariants that must hold across concurrent writes, and indexes that match the application's scoped queries and ordering. Use short `transaction.atomic()` blocks where operations must commit together. Catch expected database errors outside the atomic block.

Run `python manage.py makemigrations --check --dry-run` to find unrecorded model changes, and `python manage.py migrate --check` for unapplied migrations. Pass the app label to `makemigrations` when checking a hand-built app without a migrations package. `smackend verify` includes those app labels automatically. These commands compare models, migration files and applied migration history. Direct SQL changes to the live schema require a separate inspection. Use `migrate --plan` and `sqlmigrate <app> <migration>` to review proposed work. Apply migrations explicitly after review. Rehearse a reverse migration with disposable data. Even a reversible schema operation can discard values added after the upgrade. For deployed data, prefer phased changes such as adding a nullable field, backfilling and validating it, switching application reads, then removing the old field in a separately reviewed release. [Django migrations](https://docs.djangoproject.com/en/5.2/topics/migrations/) · [Custom users](https://docs.djangoproject.com/en/5.2/topics/auth/customizing/)

## Routing, schemas and list endpoints

The installed package supplies shared health routes. The project owns its `NinjaAPI` instance in `config/api.py`. Each consumer app can register a native `Router` under its own prefix with `api.add_router("/inventory", inventory_router)`. Native Django URL patterns remain available in `config/urls.py`.

Use Ninja input and response schemas to validate public data and document operations in OpenAPI. Avoid putting private model fields in public schemas. Use synchronous views for ordinary transactions. Async views can await external I/O or native async ORM operations. Run a multi-statement atomic operation inside one synchronous function when invoking it from an async view.

For list endpoints use Ninja's `@paginate` and `FilterSchema` with named, bounded inputs and explicit allowed lookups. Apply ownership and visibility scope first, then filters and a deterministic order including a unique tie-breaker. The default page limit is 25 and the maximum is 100. Offset pagination still permits deep offsets and may count large sets. Move demanding or frequently changing lists to a suitable indexed cursor strategy. Use separate explicit count scopes for any aggregates.

## API errors

`smackend.errors.install_error_handlers(api)` attaches default handlers to native Ninja. `smackend.errors.api_error_responses` covers error responses under `SMACKEND_API_PREFIX` (default `/api`), including unresolved routes and rejected HTTP methods outside an operation. The response body has a stable `code` and public `message`. Invalid input may also include source categories such as `query` or `body`. Internal exception messages, rejected inputs and traceback details stay out of the response. Existing headers such as `Allow`, `WWW-Authenticate` and `Retry-After` are carried forward. Errors use `Cache-Control: no-store`.

Use `APIError(status, code, public_message, headers=...)` for an application error designed to be public. Application handlers registered after the defaults can return the shared `error_response()`. Use `ErrorBody` in a native Ninja response mapping to document an operation's expected errors. An unexpected failure is logged by exception class. Database conflicts return 409, canceled queries 504, and pool or availability failures 503. A write may have committed before an error was observed. No error handler replays a write.

## Logging and diagnostics

`smackend.diagnostics.request_diagnostics` generates a fresh request ID and adds it to `X-Request-ID`. The same ID is included in API errors. Request logs go to stderr as JSON and include the method, matched route template, status and duration. Duration measures time until the view and middleware return a response. Streaming body transmission can continue afterward. Readiness, database and unexpected API failures emit named events with safe exception-class context. Set `SMACKEND_LOG_LEVEL` to choose the logging level. The formatter emits selected metadata and omits raw messages, URLs, request data, headers, credentials and tracebacks. Configure custom handlers and server logs to the same standard. Use Uvicorn's `--no-access-log` with this configuration to avoid duplicate access logs containing raw request URLs.

`/api/health` checks app liveness without borrowing a database connection. `/api/ready` checks the active PostgreSQL pool, returns a sanitized 503 on failure, and includes the request ID for correlation. Neither endpoint exposes database or server details. The readiness check is a connectivity check; run migration verification separately.

## Automated verification

From the directory containing `manage.py`, run `uv run smackend verify`. Use `--settings module.name` to select settings explicitly, or supply ordinary Django test labels such as `smackend verify inventory.tests` to narrow the application tests. The core contract tests run alongside the selected tests.

The command checks configuration, Django system checks, PostgreSQL connectivity, unrecorded model changes, applied migrations, then native Django application and core tests. It stops at the first failure and names the relevant check or native follow-up command. Exit codes are 0 for success, 1 for a failed or timed-out step, and 2 for invalid invocation. A step has up to two minutes. To avoid accidentally broadcasting secrets from custom app settings or tests, the summary omits child output. Run the indicated ordinary Django command locally when you need diagnostic details.

Extend verification with regular Django tests in your application. The core tests assert DB-independent liveness, a correlated JSON 404, and readiness against a native PostgreSQL test database. Verification never generates or applies migrations to the configured application database. Django builds the test schema in fresh uniquely named PostgreSQL databases. Smackend replaces existing `TEST.NAME` settings for this invocation and refuses an occupied generated name. Run it with a role allowed to create databases on a disposable database server or CI service. As with `manage.py test`, custom app code and test runners execute in that environment.

## Upgrading older generated projects

Installing the toolkit generates no files in an existing project. Update ordinary Django settings and project-owned files deliberately. For a project whose database has never been migrated, add the new `accounts` app and initial migration and select `AUTH_USER_MODEL = "accounts.User"` before migrating. For a project already using `auth.User`, keep that user model during the infrastructure upgrade. A later change needs a separately designed migration of user rows, foreign keys, permissions and sessions, plus a recovery rehearsal. Changing `AUTH_USER_MODEL` on that database is not an automatic upgrade.

Add `smackend[backend]==0.1.0a4` to your project dependencies, install from the local wheel directory if needed, and merge the shared router and error handler registration into your existing `NinjaAPI` instance. Keep project routes and settings. Merge the generated middleware entries, logging settings and pagination limits selectively. Clients using the old Ninja `detail` error field should switch to `code` and `message`. Readiness continues to return `status: unavailable` on a 503. Existing `.env` and database pool options remain supported.

## Current release

Use the version and installation commands in the [root README](README.md) for a new checkout. The upgrade instructions above describe the Phase 1 milestone. The [a8 qualification report](PHASE_4.md#regression-verification) records the later upgrade rehearsal.
