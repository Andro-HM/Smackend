from uuid import uuid4

from django.conf import settings
from django.db import models


class Blob(models.Model):
    class State(models.TextChoices):
        PENDING = "pending"
        ACTIVE = "active"
        GARBAGE = "garbage"

    key = models.UUIDField(default=uuid4, editable=False, unique=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    size = models.PositiveBigIntegerField()
    state = models.CharField(max_length=8, choices=State, default=State.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    retired_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=("state", "retired_at"), name="smackend_blob_cleanup")]

    @property
    def storage_name(self):
        return f"smackend/{self.key.hex[:2]}/{self.key.hex}"


class Asset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    blob = models.OneToOneField(Blob, on_delete=models.PROTECT)
    name = models.CharField(max_length=140)
    content_type = models.CharField(max_length=40)
    public = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "id")
