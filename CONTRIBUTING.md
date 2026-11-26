# Contributing to Smackend

Smackend is an alpha. Start with the [architecture](ARCHITECTURE.md), [roadmap](ROADMAP.md), and [latest qualification](PHASE_4.md). Keep toolkit infrastructure in `src/smackend/` and consumer business behavior in generated projects. Prefer Django and Ninja primitives. Include migration and compatibility implications when changing a public contract.

## Run checks

From the repository root with Python 3.11 or 3.12 and uv installed:

```sh
uv sync --locked --no-editable --all-extras --group integration
uv run --no-sync python -m pytest
uv build
```

For the integration suite, use a disposable loopback PostgreSQL server with a role allowed to create databases. Provide the following process environment variables privately before running pytest.

| Variable | Value |
|---|---|
| `SMACKEND_SMOKE_DSN` | Connection string for the disposable PostgreSQL server. |
| `SMACKEND_SMOKE_PYTHON` | Core generated-app interpreter with the generated project's dependencies installed. |
| `SMACKEND_AUTH_SMOKE_PYTHON` | Interpreter with the authentication extra and generated-app dependencies. |
| `SMACKEND_MODULE_SMOKE_PYTHON` | Interpreter with the Phase 2 extras, including `s3` and the integration tooling. |
| `SMACKEND_PHASE3_SMOKE_PYTHON` | Interpreter with AI and real-time extras and the generated-app dependencies. The all-extras interpreter can serve all optional probes. |
| `SMACKEND_POSTGRES_BIN` | Directory containing PostgreSQL utilities for the restart probe. |

Without the relevant variables PostgreSQL probes skip. Use the commands and environment details in [Phase 4](PHASE_4.md#reproduce-the-checks) for the full qualification workflow. Run `uv run smackend verify` from an installed generated application and use its output to locate failing Django checks. For browser or live-provider work, use synthetic accounts and obtain explicit authorization before sending data or incurring costs.

## Prepare a contribution

- Keep the change focused. Update the relevant module guide and add regression coverage where an existing check cannot catch the change.
- Record measured benchmark results alongside the workload, dependencies, and environment. Keep historical evidence and label simulations clearly.
- Review `git status --short` and `git diff --cached` before publication. Keep private environment files, database dumps, generated apps and raw logs outside Git.
- Follow [SECURITY.md](SECURITY.md) for a vulnerability report.
