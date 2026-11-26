from django.contrib.auth.models import AbstractUser
from django.db import models

USERNAME_PATTERN = r"^[a-z0-9_]{3,30}$"


class User(AbstractUser):
    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(username__regex=USERNAME_PATTERN),
                name="phase0_canonical_username",
            ),
        ]
