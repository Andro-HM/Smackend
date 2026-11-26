from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("identity", "0001_initial"), ("account", "0009_emailaddress_unique_primary_email")]
    operations = [
        migrations.RunSQL(
            """
            CREATE UNIQUE INDEX smackend_verified_email_lower
            ON account_emailaddress (lower(email)) WHERE verified
            """,
            reverse_sql="DROP INDEX smackend_verified_email_lower",
        ),
    ]
