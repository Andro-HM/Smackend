from time import perf_counter

from django.db.backends.postgresql.base import DatabaseWrapper as PostgreSQLWrapper

from lab.profile_asgi import request_profile

TABLE_GROUPS = {
    '"django_session"': "session",
    '"accounts_user"': "user",
    '"notes_note"': "notes",
    '"phase0_allauth_cache"': "allauth_cache",
    "phase0_login_budget": "login_budget",
}


def measure_query(execute, sql, params, many, context):
    sample = request_profile.get()
    if sample is None:
        return execute(sql, params, many, context)
    start = perf_counter()
    try:
        return execute(sql, params, many, context)
    finally:
        elapsed = perf_counter() - start
        group = next((label for table, label in TABLE_GROUPS.items() if table in str(sql)), "other")
        count, total = sample["queries"].get(group, (0, 0))
        sample["queries"][group] = (count + 1, round(total + elapsed * 1000, 3))
        sample["db"] += elapsed


class DatabaseWrapper(PostgreSQLWrapper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.execute_wrappers.append(measure_query)

    def get_new_connection(self, conn_params):
        sample = request_profile.get()
        pool = self.pool
        if sample is not None and pool is not None:
            stats = pool.get_stats()
            sample["pool_available"] = stats.get("pool_available", 0)
            sample["pool_waiting"] = stats.get("requests_waiting", 0)
        start = perf_counter()
        try:
            conn = super().get_new_connection(conn_params)
            if sample is not None:
                sample["lease_started"] = perf_counter()
            return conn
        finally:
            if sample is not None:
                sample["checkout"] += perf_counter() - start

    def _close(self):
        sample = request_profile.get()
        if sample is not None and "lease_started" in sample:
            sample["lease"] += perf_counter() - sample.pop("lease_started")
        return super()._close()
