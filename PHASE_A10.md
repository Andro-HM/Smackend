# a10 module activation

Smackend a10 adds CLI activation to generated projects. The existing
module registry resolves dependencies. AI enables HTTP and auth.
Storage, administration and real-time enable auth. Administration
uses Django and the auth extra. Each of the other modules declares its
own extra. Existing extras, including S3, remain in the dependency pin.

## Local use

Run the commands from the generated project's root. The CLI uses the
active process environment and the project's existing settings.
A project needs `manage.py`, `pyproject.toml`, `.env` and `.env.example`.

```powershell
smackend status
smackend add ai storage --dry-run
smackend add ai storage
```

Dry run reports the planned pin, final enabled list and files that
would change. It skips installation, database access and file writes.
Status reads environment flags and extras and uses an existing project
interpreter to check effective settings and pending database migrations.
When that interpreter or the database is unavailable, it reports which
checks were unavailable.

### Upgrade a9 from a local wheel on Windows

Build the a10 wheel from the toolkit checkout. This example uses the
workspace paths for the toolkit and practice project.

```powershell
Set-Location -LiteralPath 'C:\_VAULT\Techhh\full stack dev\Smackend'
uv build
$wheelDirectory = (Resolve-Path -LiteralPath 'dist').Path
$wheel = Join-Path $wheelDirectory 'smackend-0.1.0a10-py3-none-any.whl'
uv tool install --force $wheel
smackend --version

Set-Location -LiteralPath 'C:\_VAULT\Techhh\full stack dev\projects\practice'
smackend status
smackend add --upgrade --dry-run --find-links $wheelDirectory
docker compose up -d --wait postgres
smackend add --upgrade --find-links $wheelDirectory
smackend status
uv run --no-sync smackend verify
```

Use `smackend add ai storage --upgrade --find-links $wheelDirectory` on
the upgrade step to add modules at the same time. The CLI can find
matching local wheels in nearby `dist` and `wheelhouse` directories or
beside the local wheel used to install the CLI. Use `--find-links`
explicitly when the project and wheel live in separate checkouts.

## File ownership and recovery

- The CLI updates the Smackend requirement in `pyproject.toml` and
  selected flags in both dotenv files. uv manages its project
  environment and lockfile. Settings, migrations, application code,
  unrelated dependencies and existing credentials stay project-owned.
  An explicit false process override must be removed before enabling
  its module. For custom projects, the effective Django flags must
  agree with the plan. Existing projects' own routing and lifespan
  customizations remain their responsibility.
- The CLI acquires a project lock across changes. If installation or
  Django configuration checks fail, it restores the original project
  files and lockfile and attempts to sync the original dependencies
  back into an existing environment. A failed environment restore
  requires `uv sync` with the original wheel directory. Inexact sync
  preserves unrelated packages and may leave packages that the restored
  project no longer requires. A hard interruption can leave a lock.
  Confirm no activation is running, review the files and installed
  environment, then remove that lock and retry.
- Migration begins after valid configuration is saved. A connection
  failure leaves the activation ready to resume. Check PostgreSQL
  and rerun the same command when it comes back. The CLI stops before
  auto-migrating if the pending plan includes project-owned migrations,
  replaced migration modules, or reversals. Review
  `uv run --no-sync python manage.py migrate --plan` and apply approved
  changes through Django. A migration execution failure keeps the
  installed configuration and any committed migrations. Inspect the
  plan before retrying. The CLI checks migration application and model
  drift after successful automated migration.
- `uv run --no-sync smackend verify` runs the full verification suite
  on the installed environment. Stop and restart running application
  workers when changing the environment. Keep the local wheel directory
  available for future uv syncs. Compose needs separate project names,
  ports and volumes when running multiple projects.

Release and deployment qualification remains tracked in the
[roadmap](ROADMAP.md).


## Verification

The installed a10 wheel passed 91 focused tests for scaffolding,
configuration, activation and outbound lifecycle behavior. The tests
cover each module, combined activation, dependency closure, unchanged
metadata on repeat, dry runs, process overrides, and restoring files
after dependency and configuration failures.

On Windows, a disposable PostgreSQL 17.9 Compose project hosted a
customized app from the installed a9 wheel. The installed a10 CLI
discovered the local wheel, upgraded the project, enabled auth and
registered an account. Subsequent grouped activation enabled all six
modules. The CLI deferred migrations while PostgreSQL was stopped,
resumed after restart, and left an application migration pending until
manual approval. The pre-upgrade record, credentials, customized
settings and existing session survived. All six verification checks
passed in the final generated project. The test Compose project was
removed. The standalone CLI installation and a practice-project dry run
were also checked with isolated tool paths.
