from uuid import uuid4

from django.db import models


class SocketLease(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    subject = models.CharField(max_length=64, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
