from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_auth_cache")]
    operations = [
        migrations.RunSQL(
            [
                """CREATE TABLE phase0_login_budget (
                    key varchar(64) NOT NULL,
                    bucket bigint NOT NULL,
                    attempts integer NOT NULL CHECK (attempts BETWEEN 1 AND 10),
                    PRIMARY KEY (key, bucket)
                )""",
                "CREATE INDEX phase0_login_budget_bucket ON phase0_login_budget (bucket)",
            ],
            "DROP TABLE phase0_login_budget",
        )
    ]
