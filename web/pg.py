"""PostgreSQL access helpers (psycopg)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def _psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise RuntimeError(
            "psycopg is required for PostgreSQL access; pip install 'psycopg[binary]'"
        ) from error
    return psycopg, dict_row


def connect(**kwargs):
    psycopg, dict_row = _psycopg()
    return psycopg.connect(database_url(), row_factory=dict_row, **kwargs)


@contextmanager
def connection(**kwargs) -> Iterator:
    conn = connect(**kwargs)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def cursor(**kwargs) -> Iterator:
    with connection(**kwargs) as conn:
        with conn.cursor() as cur:
            yield cur
