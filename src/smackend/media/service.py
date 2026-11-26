from datetime import timedelta
import logging
from pathlib import PurePosixPath
import re
import warnings

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import storages
from django.db import connection, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from PIL import Image

from smackend.errors import APIError

from .models import Asset, Blob


logger = logging.getLogger("smackend.media")
FORMATS = {"png": ("image/png", ".png"), "jpeg": ("image/jpeg", ".jpg"),
           "webp": ("image/webp", ".webp"), "text": ("text/plain", ".txt")}


def unavailable():
    return APIError(503, "storage_unavailable", "File storage is temporarily unavailable.")


def report_failure(exc):
    logger.warning("Storage operation failed", extra={
        "event": "storage.failure", "error_type": type(exc).__name__,
    })


def inspect_upload(upload):
    policy = settings.SMACKEND_STORAGE_POLICY
    if not 0 < upload.size <= policy.file_bytes:
        raise APIError(413, "file_size", "The file exceeds the allowed size or is empty.")
    image_formats = sorted(kind.upper() for kind in policy.kinds - {"text"})
    try:
        if not image_formats:
            raise Image.UnidentifiedImageError()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(upload, formats=image_formats) as image:
                kind = (image.format or "").lower()
                if image.width * image.height > 16_000_000 or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Unsupported image dimensions or frame count")
                image.verify()
            upload.seek(0)
            with Image.open(upload, formats=image_formats) as image:
                image.load()
    except (Image.UnidentifiedImageError, UnicodeError):
        kind = "text"
        upload.seek(0)
        try:
            decoded = upload.read().decode("utf-8")
            if any(ord(char) < 32 and char not in "\t\n\r" for char in decoded):
                raise ValueError
        except (ValueError, UnicodeError):
            kind = ""
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        kind = ""
    finally:
        upload.seek(0)
    if kind not in policy.kinds:
        raise APIError(415, "unsupported_file", "The file content does not match an allowed type.")
    content_type, suffix = FORMATS[kind]
    # Names are display metadata only. The blob key is generated separately.
    leaf = PurePosixPath(upload.name.replace("\\", "/")).name
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", PurePosixPath(leaf).stem).strip("_-")[:100] or "download"
    return stem + suffix, content_type


def visible_files(user):
    scope = Q(public=True)
    if user.is_authenticated and user.is_active:
        scope |= Q(owner=user)
    return Asset.objects.filter(scope).select_related("blob")


def store_files(owner, uploads, *, public=False, replace=None):
    policy = settings.SMACKEND_STORAGE_POLICY
    if not 1 <= len(uploads) <= policy.files_per_request or (replace is not None and len(uploads) != 1):
        raise APIError(400, "upload_count", "Supply the permitted number of files.")
    metadata = [inspect_upload(upload) for upload in uploads]
    with transaction.atomic(durable=True):
        get_user_model().objects.select_for_update().get(pk=owner.pk, is_active=True)
        old = None
        if replace is not None:
            old = Asset.objects.filter(pk=replace, owner=owner).first()
            if old is None:
                raise APIError(404, "not_found", "File not found.")
        usage = Blob.objects.filter(owner=owner).aggregate(size=Sum("size", default=0), count=Count("id"))
        if (usage["size"] + sum(upload.size for upload in uploads) > policy.quota_bytes
                or usage["count"] + len(uploads) > policy.quota_files):
            raise APIError(409, "storage_quota", "There is insufficient storage quota for this upload.")
        blobs = Blob.objects.bulk_create([Blob(owner=owner, size=upload.size) for upload in uploads])
    connection.close()
    try:
        storage = storages["smackend_media"]
        for blob, upload in zip(blobs, uploads):
            actual = storage.save(blob.storage_name, upload)
            if actual != blob.storage_name:
                storage.delete(actual)
                raise RuntimeError("The backend changed a reserved blob key")
        with transaction.atomic(durable=True):
            get_user_model().objects.select_for_update().get(pk=owner.pk, is_active=True)
            if old:
                item = Asset.objects.filter(pk=old.pk, owner=owner, blob_id=old.blob_id).first()
                if item is None:
                    raise APIError(409, "file_changed", "The file changed while the replacement was uploading.")
                item.blob, (item.name, item.content_type), item.public = blobs[0], metadata[0], public
                item.save(update_fields=["blob", "name", "content_type", "public"])
                Blob.objects.filter(pk=old.blob_id).update(state=Blob.State.GARBAGE, retired_at=timezone.now())
                result = [item]
            else:
                result = Asset.objects.bulk_create([
                    Asset(owner=owner, blob=blob, name=name, content_type=kind, public=public)
                    for blob, (name, kind) in zip(blobs, metadata)
                ])
            Blob.objects.filter(pk__in=[blob.pk for blob in blobs]).update(state=Blob.State.ACTIVE)
    except Exception as exc:
        # A failed database commit may have succeeded on the server. Only rows
        # still pending may be reclaimed. If PostgreSQL is down, the journal stays.
        try:
            Blob.objects.filter(pk__in=[blob.pk for blob in blobs], state=Blob.State.PENDING).update(
                state=Blob.State.GARBAGE, retired_at=timezone.now(),
            )
        except Exception as cleanup_error:
            report_failure(cleanup_error)
        if isinstance(exc, APIError):
            raise
        report_failure(exc)
        raise unavailable() from None
    return result


def delete_file(owner, asset_id):
    with transaction.atomic(durable=True):
        get_user_model().objects.select_for_update().get(pk=owner.pk, is_active=True)
        asset = Asset.objects.filter(pk=asset_id, owner=owner).first()
        if asset is None:
            raise APIError(404, "not_found", "File not found.")
        retire_asset(asset)


def retire_asset(asset):
    """Retire metadata and queue bytes for cleanup while the caller holds the owner lock."""
    blob_id = asset.blob_id
    asset.delete()
    Blob.objects.filter(pk=blob_id).update(state=Blob.State.GARBAGE, retired_at=timezone.now())


def open_file(asset):
    connection.close()
    try:
        return storages["smackend_media"].open(asset.blob.storage_name, "rb")
    except Exception as exc:
        report_failure(exc)
        raise unavailable() from None


def cleanup(*, abandoned=False, limit=100, grace=300):
    if abandoned:
        # Maintenance-only operation. Stop upload workers before invoking it.
        Blob.objects.filter(state=Blob.State.PENDING, created_at__lt=timezone.now() - timedelta(hours=24)).update(
            state=Blob.State.GARBAGE, retired_at=timezone.now(),
        )
    # A brief grace period protects downloads that already resolved the old object.
    cutoff = timezone.now() - timedelta(seconds=grace)
    blobs = list(Blob.objects.filter(state=Blob.State.GARBAGE, retired_at__lte=cutoff).order_by("retired_at", "pk")[:limit])
    connection.close()
    removed, failed = 0, 0
    for blob in blobs:
        try:
            storages["smackend_media"].delete(blob.storage_name)
        except Exception as exc:
            report_failure(exc)
            failed += 1
            continue
        count, _ = Blob.objects.filter(pk=blob.pk, state=Blob.State.GARBAGE).delete()
        removed += count
        connection.close()
    return removed, failed
