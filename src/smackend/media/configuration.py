from dataclasses import dataclass
from importlib.util import find_spec
from urllib.parse import urlsplit
import re

from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import Storage
from django.utils.module_loading import import_string


TYPES = {"png", "jpeg", "webp", "text"}


@dataclass(frozen=True)
class StoragePolicy:
    file_bytes: int
    files_per_request: int
    quota_bytes: int
    quota_files: int
    kinds: frozenset[str]


def size(environment, name, default, limit):
    key = f"SMACKEND_STORAGE_{name}"
    value = environment.get(key, str(default))
    if not re.fullmatch(r"[0-9]{1,12}", value) or not 1 <= int(value) <= limit:
        raise ImproperlyConfigured(f"{key} must be between 1 and {limit}.")
    return int(value)


def configure(settings, environment, *, production):
    if not settings.get("SMACKEND_AUTH_ENABLED"):
        raise ImproperlyConfigured("Storage requires SMACKEND_AUTH_ENABLED and the auth extra.")
    if find_spec("PIL") is None:
        raise ImproperlyConfigured("Install the Smackend storage extra before enabling storage.")
    kinds = frozenset(item.strip() for item in environment.get(
        "SMACKEND_STORAGE_TYPES", "png,jpeg,webp,text",
    ).split(","))
    if not kinds <= TYPES:
        raise ImproperlyConfigured("SMACKEND_STORAGE_TYPES accepts png, jpeg, webp and text.")
    policy = StoragePolicy(
        size(environment, "FILE_BYTES", 5 << 20, 32 << 20),
        size(environment, "FILES_PER_REQUEST", 4, 10),
        size(environment, "QUOTA_BYTES", 50 << 20, 1 << 40),
        size(environment, "QUOTA_FILES", 200, 100_000),
        kinds,
    )
    if policy.quota_bytes < policy.file_bytes:
        raise ImproperlyConfigured("SMACKEND_STORAGE_QUOTA_BYTES must cover one maximum-sized file.")
    backend = environment.get("SMACKEND_STORAGE_BACKEND", "local")
    if backend == "local":
        location = (settings["BASE_DIR"] / environment.get("SMACKEND_STORAGE_ROOT", ".smackend-media")).resolve()
        frontend = settings["FRONTEND_DIR"].resolve()
        if location == frontend or location.is_relative_to(frontend) or frontend.is_relative_to(location):
            raise ImproperlyConfigured("Use a dedicated SMACKEND_STORAGE_ROOT outside the frontend.")
        options = {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": location, "file_permissions_mode": 0o600,
                        "directory_permissions_mode": 0o700, "allow_overwrite": False},
        }
    elif backend == "s3":
        options = s3_backend(environment, production)
    else:
        try:
            if not issubclass(import_string(backend), Storage):
                raise TypeError
        except (ImportError, TypeError, ValueError):
            raise ImproperlyConfigured("SMACKEND_STORAGE_BACKEND must be local, s3, or a Django storage class.") from None
        options = {"BACKEND": backend}
    settings.setdefault("STORAGES", {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })["smackend_media"] = options
    settings["SMACKEND_STORAGE_POLICY"] = policy
    settings["INSTALLED_APPS"] += ["smackend.media"]
    settings["FILE_UPLOAD_HANDLERS"] = [
        "smackend.media.uploads.MediaUploadLimit",
        "django.core.files.uploadhandler.MemoryFileUploadHandler",
        "django.core.files.uploadhandler.TemporaryFileUploadHandler",
    ]


def s3_backend(environment, production):
    if find_spec("storages") is None or find_spec("boto3") is None:
        raise ImproperlyConfigured("Install the Smackend s3 extra for S3-compatible storage.")
    bucket = environment.get("SMACKEND_STORAGE_BUCKET", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ImproperlyConfigured("SMACKEND_STORAGE_BUCKET must name a private bucket.")
    endpoint = environment.get("SMACKEND_STORAGE_S3_ENDPOINT", "")
    if endpoint:
        try:
            url = urlsplit(endpoint)
            invalid = (url.scheme not in ({"https"} if production else {"http", "https"}) or not url.hostname
                       or url.username is not None or url.path not in {"", "/"} or url.query or url.fragment)
            if invalid or (url.port is not None and not 1 <= url.port <= 65535):
                raise ValueError
        except ValueError:
            raise ImproperlyConfigured("SMACKEND_STORAGE_S3_ENDPOINT must be a valid provider origin.") from None
    from botocore.config import Config

    return {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": {
        "bucket_name": bucket, "region_name": environment.get("SMACKEND_STORAGE_S3_REGION") or None,
        "endpoint_url": endpoint or None, "default_acl": None, "file_overwrite": False,
        "client_config": Config(connect_timeout=3, read_timeout=8, retries={"total_max_attempts": 2},
                                s3={"addressing_style": "path"}),
    }}
