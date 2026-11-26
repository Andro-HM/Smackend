# Licensing

Smackend is distributed under the standard [MIT License](LICENSE). Keep the copyright and permission notices with copies or substantial portions of Smackend. The project license is declared as `MIT` in the package metadata and included in both distribution formats. [MIT License reference](https://opensource.org/license/mit)

## Third-party components

The repository declares Python dependencies and references the upstream PostgreSQL image. The Smackend wheel and source distribution include Smackend code and templates. Package installation resolves the external dependencies separately. Their upstream license files and notices apply to those components.

| Components reviewed | License from installed metadata or upstream |
|---|---|
| Django, HTTPX, Uvicorn, and websockets | BSD-3-Clause |
| Django Ninja, django-allauth and JSON Schema | MIT |
| Channels and django-storages | BSD |
| Pillow | MIT-CMU |
| psycopg, psycopg-binary and psycopg-pool | LGPL-3.0-only |
| boto3 and botocore for the optional S3 backend | Apache-2.0 |
| certifi, via outbound dependencies | MPL-2.0 |
| PostgreSQL itself | PostgreSQL License |

This is a review of the repository and its current package layout. If distributing a bundled virtual environment, container image, database binaries or a dependency-inclusive installer, inventory the exact files and their upstream license, notice, and source obligations. Compiled wheels and base images can contain additional components. [Psycopg license](https://www.psycopg.org/psycopg3/docs/) · [PostgreSQL license](https://www.postgresql.org/about/licence/) · [certifi](https://github.com/certifi/python-certifi)
