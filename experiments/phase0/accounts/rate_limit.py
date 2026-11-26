from django.db import connection
from django.utils.crypto import salted_hmac


def admit_login(username):
    """Returns the wait in seconds when the shared fixed-window quota is full."""
    key = salted_hmac("phase0.login", username, algorithm="sha256").hexdigest()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH sample AS (
                SELECT floor(extract(epoch FROM statement_timestamp()))::bigint AS now_s
            ), admitted AS (
                INSERT INTO phase0_login_budget (key, bucket, attempts)
                SELECT %s, now_s / 60, 1 FROM sample
                ON CONFLICT (key, bucket) DO UPDATE SET attempts = phase0_login_budget.attempts + 1
                WHERE phase0_login_budget.attempts < 10
                RETURNING 1
            )
            SELECT CASE WHEN EXISTS (SELECT 1 FROM admitted) THEN 0 ELSE 60 - now_s %% 60 END
            FROM sample
            """,
            [key],
        )
        return cursor.fetchone()[0]
