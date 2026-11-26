from django.conf import settings
from django.db import models


class Habit(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    label = models.CharField(max_length=80)
    repetitions = models.PositiveIntegerField(default=0)
