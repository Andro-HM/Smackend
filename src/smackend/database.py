"""Map expected database failures at the HTTP boundary."""

from django.db import IntegrityError, InterfaceError, OperationalError
from psycopg import OperationalError as DriverOperationalError
from psycopg_pool import PoolTimeout, TooManyRequests

DATABASE_ERRORS = (OperationalError, InterfaceError, DriverOperationalError)


def database_error(exc):
    driver_error = exc.__cause__ or exc
    sqlstate = getattr(driver_error, "sqlstate", None)
    if isinstance(driver_error, (PoolTimeout, TooManyRequests)):
        status, code = 503, "database_busy"
    elif sqlstate == "57014":
        status, code = 504, "database_query_cancelled"
    elif sqlstate in {"55P03", "40001", "40P01", "23503", "23505", "23514", "23P01"}:
        status, code = 409, "database_conflict"
    elif isinstance(exc, IntegrityError):
        status, code = 500, "internal_error"
    else:
        status, code = 503, "database_unavailable"
    return status, code
