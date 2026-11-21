import sqlite3

import pytest

from agent import db


@pytest.mark.parametrize("query", [
    "INSERT INTO suppliers VALUES ('S99','x','y','z')",
    "DROP TABLE invoices",
    "SELECT 1; DROP TABLE invoices",
    "PRAGMA table_info(invoices)",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "UPDATE purchase_orders SET amount = 0",
    "WITH x AS (SELECT 1) DELETE FROM invoices",
    "",
])
def test_guard_rejects_non_select(query):
    with pytest.raises(db.SQLRejected):
        db.guard_sql(query)


def test_guard_allows_select_with_keyword_in_string_literal():
    q = db.guard_sql("SELECT * FROM credits WHERE reason = 'delete me'; -- trailing comment")
    assert q.startswith("SELECT")


def test_connection_is_read_only_even_without_guard():
    con = db.connect()
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO suppliers VALUES ('S99','x','y','z')")
    con.close()


def test_row_limit_truncates():
    res = db.run_query("SELECT * FROM purchase_orders", row_limit=10)
    assert res["row_count"] == 10 and res["truncated"] is True


def test_timeout_stops_runaway_query():
    runaway = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT COUNT(*) FROM n"
    with pytest.raises(db.SQLRejected, match="time limit"):
        db.run_query(runaway, timeout_s=0.5)
