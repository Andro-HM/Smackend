from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Invocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("subject", models.CharField(max_length=64)),
                ("request_key", models.UUIDField()),
                ("day", models.DateField(db_index=True)),
                ("active_until", models.DateTimeField(db_index=True)),
                ("model_alias", models.CharField(max_length=50)),
                ("state", models.CharField(choices=[("reserved", "Reserved"), ("complete", "Complete"),
                                                   ("unknown", "Unknown"), ("rejected", "Rejected")],
                                           default="reserved", max_length=10)),
                ("tokens", models.PositiveBigIntegerField()),
                ("cost_micros", models.PositiveBigIntegerField()),
                ("input_tokens", models.PositiveBigIntegerField(null=True)),
                ("output_tokens", models.PositiveBigIntegerField(null=True)),
            ],
            options={
                "indexes": [models.Index(fields=["subject", "day"], name="smackend_ai_subject_day")],
                "constraints": [models.UniqueConstraint(fields=("subject", "request_key"),
                                                       name="smackend_ai_request_unique")],
            },
        ),
    ]
