import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
            name="Blob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("size", models.PositiveBigIntegerField()),
                ("state", models.CharField(choices=[("pending", "Pending"), ("active", "Active"), ("garbage", "Garbage")],
                                           default="pending", max_length=8)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("retired_at", models.DateTimeField(null=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={"indexes": [models.Index(fields=["state", "retired_at"], name="smackend_blob_cleanup")]},
        ),
        migrations.CreateModel(
            name="Asset",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=140)),
                ("content_type", models.CharField(max_length=40)),
                ("public", models.BooleanField(db_index=True, default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("blob", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to="media.blob")),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at", "id")},
        ),
    ]
