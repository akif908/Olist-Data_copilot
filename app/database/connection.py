"""PostgreSQL connection helpers.

Two deliberately separate connections (refinement #9):

* ``get_connection`` — admin/import role (PGUSER). Used for setup, inspection and
  data import. NEVER used to execute generated queries.
* ``get_readonly_connection`` — dedicated read-only role (PG_RO_USER, default
  ``copilot_ro``). Used by the safe executor for generated queries. The role's
  grants and role-level settings enforce security independently of the validator.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from app.config import DatabaseSettings, ReadOnlyDatabaseSettings


@contextmanager
def get_connection(
    settings: DatabaseSettings | None = None,
) -> Iterator[psycopg.Connection]:
    """Yield an open admin PostgreSQL connection and close it afterwards.

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


@contextmanager
def get_readonly_connection(
    settings: ReadOnlyDatabaseSettings | None = None,
    *,
    read_only: bool = True,
) -> Iterator[psycopg.Connection]:
    """Yield a connection authenticated as the dedicated read-only role.

    ``read_only=True`` (default) additionally forces read-only transactions at
    the session level, on top of the role's grants and role-level GUCs. Pass
    ``read_only=False`` only for permission-isolation tests that need to observe
    the database's own permission denial rather than a read-only-transaction
    rejection (refinement #6). The password is never logged.
    """
    settings = settings or ReadOnlyDatabaseSettings.from_env()
    conn = psycopg.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.dbname,
        user=settings.user,
        password=settings.password,
        connect_timeout=10,
    )
    try:
        conn.read_only = read_only
        yield conn
    finally:
        conn.close()