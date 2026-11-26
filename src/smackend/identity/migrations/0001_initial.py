from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
            name="PhoneAddress",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("number", models.CharField(max_length=16)),
                ("verified", models.BooleanField(default=False)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="+",
                                              to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [
                models.UniqueConstraint(fields=("number",), condition=models.Q(verified=True),
                                        name="smackend_verified_phone"),
                models.CheckConstraint(condition=models.Q(number__regex=r"^[+][1-9][0-9]{6,14}$"),
                                       name="smackend_phone_e164"),
            ]},
        ),
        migrations.CreateModel(
            name="RateBucket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=64)),
                ("bucket", models.BigIntegerField(db_index=True)),
                ("attempts", models.PositiveIntegerField()),
            ],
            options={
                "db_table": "smackend_auth_rate_bucket",
                "constraints": [models.UniqueConstraint(fields=("key", "bucket"), name="smackend_auth_rate_key_bucket")],
            },
        ),
    ]
