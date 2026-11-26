from django.conf import settings
from django.db import models


class Note(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=80)
    body = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["owner", "id"])]
