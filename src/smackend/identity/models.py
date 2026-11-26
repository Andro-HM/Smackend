from django.conf import settings
from django.db import models


class PhoneAddress(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    number = models.CharField(max_length=16)
    verified = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["number"], condition=models.Q(verified=True), name="smackend_verified_phone"),
            models.CheckConstraint(condition=models.Q(number__regex=r"^[+][1-9][0-9]{6,14}$"),
                                   name="smackend_phone_e164"),
        ]


class RateBucket(models.Model):
    key = models.CharField(max_length=64)
    bucket = models.BigIntegerField(db_index=True)
    attempts = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["key", "bucket"], name="smackend_auth_rate_key_bucket"),
        ]
        db_table = "smackend_auth_rate_bucket"
