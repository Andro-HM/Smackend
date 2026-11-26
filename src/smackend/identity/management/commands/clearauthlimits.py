from django.core.management.base import BaseCommand
from django.db import connection

from smackend.identity.rate_limit import WINDOW_SECONDS


class Command(BaseCommand):
    help = "Remove expired Smackend rate-limit rows in bounded batches."

    def handle(self, *args, **options):
        removed = 0
        # Cap the work per invocation. Schedule periodically on one operator process.
        for _ in range(100):
            with connection.cursor() as cursor:
                cursor.execute("""
                    DELETE FROM smackend_auth_rate_bucket WHERE id IN (
                        SELECT id FROM smackend_auth_rate_bucket
                        WHERE bucket < floor(extract(epoch FROM statement_timestamp()))::bigint / %s
                        ORDER BY bucket, id LIMIT 1000 FOR UPDATE SKIP LOCKED
                    )
                """, [WINDOW_SECONDS])
                count = cursor.rowcount
            removed += count
            if count < 1000:
                break
        self.stdout.write(f"Removed {removed} expired rate-limit rows.")
