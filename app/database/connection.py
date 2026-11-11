"""PostgreSQL connection helpers.

Phase 1 provides a plain admin/import connection. The dedicated read-only
application connection and safe executor are added in Phase 5.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from app.config import DatabaseSettings


@contextmanager
def get_connection(
    settings: DatabaseSettings | None = None,
) -> Iterator[psycopg.Connection]:
    """Yield an open PostgreSQL connection and close it afterwards.

    The password is read from the environment; never log the settings object.
    """
    settings = settings or DatabaseSettings.from_env()
    conn = psycopg.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.dbname,
        user=settings.user,
        password=settings.password,
        connect_timeout=10,
    )
    try:
        yield conn
    finally:
        conn.close()