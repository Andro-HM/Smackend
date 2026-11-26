"""Admission quotas use PostgreSQL's clock and one atomic upsert per budget."""

from ipaddress import ip_address

from django.conf import settings
from django.db import connection
from django.utils.crypto import salted_hmac


DEFAULT_LIMITS = {
    "global": 600, "ip": 60, "delivery": 5, "password": 10, "challenge": 20,
    "send_global": 120, "send_recipient": 6,
}
WINDOW_SECONDS = 60


def limits():
    return DEFAULT_LIMITS | getattr(settings, "SMACKEND_AUTH_LIMITS", {})


def client_address(request):
    try:
        ip = ip_address(request.META.get("REMOTE_ADDR", ""))
        return str(getattr(ip, "ipv4_mapped", None) or ip)
    except ValueError:
        return "unknown"


def spend(scope, subject):
    quota = limits()[scope]
    digest = salted_hmac(f"smackend.identity.rate.{scope}", subject, algorithm="sha256").hexdigest()
    # At a full bucket, requests neither increment the row nor create more keys.
    with connection.cursor() as cursor:
        cursor.execute("""
            WITH sample AS (
                SELECT floor(extract(epoch FROM statement_timestamp()))::bigint AS second
            ), admitted AS (
                INSERT INTO smackend_auth_rate_bucket (key, bucket, attempts)
                SELECT %s, second / %s, 1 FROM sample
                ON CONFLICT (key, bucket) DO UPDATE
                SET attempts = smackend_auth_rate_bucket.attempts + 1
                WHERE smackend_auth_rate_bucket.attempts < %s
                RETURNING 1
            )
            SELECT CASE WHEN EXISTS (SELECT 1 FROM admitted) THEN 0 ELSE %s - second %% %s END
            FROM sample
        """, [digest, WINDOW_SECONDS, quota, WINDOW_SECONDS, WINDOW_SECONDS])
        return cursor.fetchone()[0]
