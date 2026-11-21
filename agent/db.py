"""Read-only database access with a SQL guard.

Three layers of protection, so one mistake doesn't open the database to writes:
1. The connection is opened read-only (SQLite URI mode=ro) with query_only on.
2. guard_sql() accepts a single SELECT/WITH statement and rejects write or admin keywords.
3. A progress handler aborts queries that run longer than the timeout, and results are capped.
"""

from __future__ import annotations

import re
import sqlite3
import time

from . import config

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex|"
    r"truncate|grant|load_extension)\b",
    re.IGNORECASE,
)


class SQLRejected(ValueError):
    """The query was refused by the guard."""


def guard_sql(query: str) -> str:
    """Return the cleaned query, or raise SQLRejected."""
    q = re.sub(r"--[^\n]*", " ", query)          # strip line comments
    q = re.sub(r"/\*.*?\*/", " ", q, flags=re.S)  # strip block comments
    q = q.strip().rstrip(";").strip()
    if not q:
        raise SQLRejected("Empty query.")
    if ";" in q:
        raise SQLRejected("Only one statement is allowed.")
    if not re.match(r"^(select|with)\b", q, re.IGNORECASE):
        raise SQLRejected("Only SELECT (or WITH ... SELECT) queries are allowed.")
    m = _FORBIDDEN.search(re.sub(r"'(?:[^']|'')*'", "''", q))  # ignore words inside string literals
    if m:
        raise SQLRejected(f"Keyword not allowed: {m.group(0).upper()}.")
    return q


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only = ON")
    con.row_factory = sqlite3.Row
    return con


def run_query(query: str, row_limit: int = config.SQL_ROW_LIMIT,
              timeout_s: float = config.SQL_TIMEOUT_SECONDS) -> dict:
    """Run a guarded query. Returns columns, rows (as dicts), and whether rows were truncated."""
    q = guard_sql(query)
    con = connect()
    deadline = time.monotonic() + timeout_s
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = con.execute(q)
        rows = cur.fetchmany(row_limit + 1)
        columns = [d[0] for d in cur.description] if cur.description else []
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise SQLRejected(f"Query exceeded the {timeout_s}s time limit.") from e
        raise
    finally:
        con.close()
    truncated = len(rows) > row_limit
    return {
        "columns": columns,
        "rows": [dict(r) for r in rows[:row_limit]],
        "row_count": min(len(rows), row_limit),
        "truncated": truncated,
    }


def fetch_all(query: str, params: tuple = ()) -> list[dict]:
    """Trusted internal reads (used by verify.py and the UI), not exposed to the model."""
    con = connect()
    try:
        return [dict(r) for r in con.execute(query, params).fetchall()]
    finally:
        con.close()
