from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=60)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-id")


def record(action, target, *, actor=None):
    return AuditEvent.objects.create(
        actor=actor, action=action, object_type=target._meta.label_lower, object_id=str(target.pk),
    )
