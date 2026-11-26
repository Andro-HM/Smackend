from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]
    operations = [
        migrations.RunSQL(
            [
                """CREATE TABLE phase0_allauth_cache (
                    cache_key varchar(255) PRIMARY KEY,
                    value text NOT NULL,
                    expires timestamp with time zone NOT NULL
                )""",
                "CREATE INDEX phase0_allauth_cache_expires ON phase0_allauth_cache (expires)",
            ],
            "DROP TABLE phase0_allauth_cache",
        ),
    ]
