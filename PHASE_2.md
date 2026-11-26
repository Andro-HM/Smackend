# Optional modules in Smackend

The first-party module flags are `--auth`, `--storage`, `--admin` and `--http` on `smackend init`. Storage and admin enable authentication. Outbound HTTP can run independently. Dependencies and credentials are needed only for the selected modules and providers. See the [identity guide](PHASE_2A.md) for email, SMS and Google settings.

## File storage

Managed file routes live under `/api/files/` and appear in Ninja's OpenAPI docs. Upload one or several files with repeated multipart `files` fields. Replace a file at `POST /api/files/<id>/replace` with one multipart `file` field. The optional `public` flag defaults to false on both requests. GET metadata and downloads respect visibility. Lists, replacements and deletions are owner-scoped. An unauthorized lookup of a private file returns 404.

| Setting | Default |
|---|---|
| `SMACKEND_STORAGE_BACKEND` | `local`. Set `s3` or supply a dotted Django storage class. |
| `SMACKEND_STORAGE_ROOT` | `.smackend-media` under the project. Keep it outside public/static directories and mount persistent storage in a deployment. |
| `SMACKEND_STORAGE_FILE_BYTES` | 5 MiB per file, up to 32 MiB. |
| `SMACKEND_STORAGE_FILES_PER_REQUEST` | 4, up to 10. |
| `SMACKEND_STORAGE_QUOTA_BYTES` | 50 MiB across active, pending and retired objects per owner. |
| `SMACKEND_STORAGE_QUOTA_FILES` | 200 objects per owner across the same states. |
| `SMACKEND_STORAGE_TYPES` | `png,jpeg,webp,text`. Allow a subset. Only bounded single-frame images and UTF-8 text are accepted. |

Django parses multipart uploads with its native memory and temporary-file handlers. The module applies byte and file count limits while parsing and checks actual content. Stored names are random keys. Download names are sanitized metadata. Every download uses an attachment response with sniffing disabled. Configure an ingress body limit and appropriate upload timeouts at deployment. The module performs content validation. Malware scanning and external distribution are outside the built-in backend.

File metadata stays in PostgreSQL. A short transaction locks the owner and reserves quota and blob keys. Storage writes happen after that transaction. A second transaction publishes metadata only after every file has been written. A replacement swaps the existing file reference and queues the old blob. Deletions remove application access immediately and queue the bytes. Replacements need room for both the old and new bytes until cleanup.

Schedule `python manage.py cleanupmedia` to reclaim retired objects after a five-minute grace period. Failed deletions remain recorded for another attempt. If an upload process dies, its pending reservation remains known. Use `cleanupmedia --abandoned` during maintenance with all upload workers stopped to reclaim pending reservations older than 24 hours. Keep a coordinated backup of PostgreSQL and the object store and reconcile after restoring either one independently.

For S3-compatible storage, install the `s3` extra and configure `SMACKEND_STORAGE_BUCKET`, optional `SMACKEND_STORAGE_S3_REGION` and `SMACKEND_STORAGE_S3_ENDPOINT`. Use the SDK's normal credential chain and a private bucket. Both public and private application files are served through the authorized Django route. The bucket must keep public access blocked. A custom Django backend should preserve the requested unique storage key and support idempotent delete. Supply custom backend options through the `STORAGES["smackend_media"]` alias. [Django storage API](https://docs.djangoproject.com/en/5.2/ref/files/storage/) · [django-storages S3 settings](https://django-storages.readthedocs.io/en/latest/backends/amazon-S3.html)

## Administration

Admin uses a native Django `AdminSite` at `/admin/`. Sign in at `/account/` and explicitly confirm your password or a supported verification code. Admin access requires an active staff account and recent proof. An operator with trusted shell access can run `python manage.py grantadministrator <user_id>` for an existing active account. For the default username/password profile, Django's `createsuperuser` is also available.

Restricted staff receive ordinary Django permissions. The stock account view is read-only for staff. Superusers can manage roles and permissions, update staff status, activate or deactivate ordinary accounts, and revoke sessions. Superuser accounts are protected from bulk activation and deactivation. Password hashes and OAuth tokens are excluded from the supplied admin pages. The administrator audit records actions, actor IDs, target IDs and timestamps. File retirement uses the same cleanup journal as the API.

Native search, filters and pagination apply. Register additional model admins on `admin_site` in the project-owned `config/admin.py`. Keep application permissions and sensitive field exclusions in that project. Run `collectstatic` and serve Django's admin assets from the deployment's static asset server. For a local development check, Django's staticfiles runserver can serve assets directly. [Django admin documentation](https://docs.djangoproject.com/en/5.2/ref/contrib/admin/)

## Outbound HTTP

Enable the `http` extra and `SMACKEND_HTTP_ENABLED`. A project can create clients directly or define named provider defaults in `SMACKEND_HTTP_PROVIDERS`. The template shows a JSON example. Each provider supplies a trusted `base_url` origin. Optional `credentials` map header names to an `env` variable and optional `prefix`. Supply actual credentials privately and choose the provider and path in application code.

```python
from smackend.outbound.client import Client
from smackend.outbound.django import provider, release_django_connections

# Complete authorization and database work in this synchronous unit first.
release_django_connections()
with Client(provider("catalog")) as catalog:
    response = catalog.request("GET", "/v1/items", params={"limit": 20})
    items = response.json()
```

Use `AsyncClient` with `async with` and `await client.request(...)` for async endpoints. Release Django connections in the same synchronous helper that obtained them before awaiting external work. The helper rejects open transactions. Reuse one client across calls for keep-alive, close it at the end of its owning service lifecycle, and share async clients only within their owning event loop. Clients support ordered query arguments, JSON or bytes bodies, optional per-call headers, and JSON validation through `response.json(validator)`.

Defaults bound connection time to 3 seconds, per-operation response inactivity to 5 seconds, response bodies to 2 MiB, and concurrent operations to 8 per client. Async requests have a 15-second overall deadline with cancellation propagation. Sync requests enforce socket timeouts and check the remaining budget at response boundaries. A blocking socket operation can run until its own timeout. Responses must be identity-encoded. Redirects are disabled and request paths stay on the trusted provider origin. Live custom transports and credentials are application configuration. [HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/)

Retryable status and transport failures use bounded exponential backoff with jitter. `Retry-After` is honored within the configured wait budget. Higher delays are returned to the caller. GET, HEAD, OPTIONS, PUT and DELETE can retry according to HTTP idempotency semantics. For a non-idempotent write, explicit retry opt-in requires an idempotency key that the provider actually supports. Errors expose a stable code and omit upstream response bodies and credentials. The default Ninja integration reports sanitized 502, 503 or 504 responses as appropriate.

## Upgrade and verification

Upgrade the project pin to `0.1.0a6` and include the needed extras. Keep existing application code, URL registrations, settings, data, and the private `.env`. Merge the module settings call and relevant URL/API registration blocks from a fresh scaffold. Copy `config/admin.py` if adding admin. Enable only the intended `SMACKEND_*_ENABLED` flags and run native Django checks and migrations. An operator adding storage or admin to an existing project also enables authentication. If the project overrides its admin site namespace, set `SMACKEND_ADMIN_INDEX` to its index URL name for verification.

`smackend verify` checks the assembled enabled modules and their configuration on its disposable PostgreSQL databases. It performs no live-provider network calls. The integration suite uses real PostgreSQL with local files, Moto for S3-compatible SDK calls, and HTTPX mock transports or a loopback stub. The upgrade scenario retains the existing session and a consumer-owned table, then exercises storage, native admin, and both HTTP clients. Live bucket policies, S3 compatibility with a particular vendor, and live outbound integrations require deployment-specific verification.

Run the regression suite with `uv sync --all-extras --group integration`, then set the smoke interpreter variables described in the root README and supply a private disposable PostgreSQL DSN. Use `SMACKEND_MODULE_SMOKE_PYTHON` for the module matrix. The tests allocate fresh databases per scenario.

## Current storage dependency

The a8 storage extra requires Pillow 13 and selects decoders from the configured file types. See the [a8 security and regression findings](PHASE_4.md#implementation-audit). The upgrade instructions above describe the Phase 2 milestone.
