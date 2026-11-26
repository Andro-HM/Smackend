from django.db import models


class Invocation(models.Model):
    class State(models.TextChoices):
        RESERVED = "reserved"
        COMPLETE = "complete"
        UNKNOWN = "unknown"
        REJECTED = "rejected"

    subject = models.CharField(max_length=64)
    request_key = models.UUIDField()
    day = models.DateField(db_index=True)
    active_until = models.DateTimeField(db_index=True)
    model_alias = models.CharField(max_length=50)
    state = models.CharField(max_length=10, choices=State, default=State.RESERVED)
    tokens = models.PositiveBigIntegerField()
    cost_micros = models.PositiveBigIntegerField()
    input_tokens = models.PositiveBigIntegerField(null=True)
    output_tokens = models.PositiveBigIntegerField(null=True)

    class Meta:
        indexes = [models.Index(fields=("subject", "day"), name="smackend_ai_subject_day")]
        constraints = [models.UniqueConstraint(fields=("subject", "request_key"), name="smackend_ai_request_unique")]
