"""Phase 5 Step 5 — LIVE integration tests for the safe executor.

Runs through get_readonly_connection() as copilot_ro against the real database.
Skipped automatically when PG_RO_PASSWORD is not configured. Verifies successful
SELECT, empty results, exact truncation boundaries, the hard row cap, statement
timeout, read-only/permission rejection, audit emission, and that data is unchanged.
"""
from __future__ import annotations

import json
import logging

import pytest

from app.config import ReadOnlyDatabaseSettings
from app.database.executor import MAX_ROWS, ExecutionError, execute_sql
from app.security.sql_validator import SQLValidationError

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not ReadOnlyDatabaseSettings.from_env().password,
        reason="PG_RO_PASSWORD not configured in .env",
    ),
]


def test_successful_select():
    r = execute_sql("SELECT count(*) AS n FROM orders")
    assert r.status == "ok"
    assert r.columns == ("n",)
    assert r.row_count == 1
    assert r.rows[0][0] == 99441
    assert r.truncated is False
    assert r.tables == ("orders",)
    assert r.duration_ms >= 0


def test_empty_result():
    r = execute_sql("SELECT order_id FROM orders WHERE order_id = '__none__'")
    assert r.row_count == 0
    assert r.rows == ()
    assert r.truncated is False
    assert r.columns == ("order_id",)


@pytest.mark.parametrize("limit,expect_count,expect_trunc", [
    (4, 4, False),   # max_rows - 1
    (5, 5, False),   # max_rows
    (6, 5, True),    # max_rows + 1
])
def test_truncation_boundaries(limit, expect_count, expect_trunc):
    r = execute_sql(f"SELECT order_id FROM orders LIMIT {limit}", max_rows=5)
    assert r.row_count == expect_count
    assert r.truncated is expect_trunc
    assert len(r.rows) == expect_count
    assert r.max_rows == 5


def test_hard_cap_at_default_max_rows():
    r = execute_sql("SELECT order_id FROM orders")
    assert r.max_rows == MAX_ROWS
    assert r.row_count == MAX_ROWS
    assert r.truncated is True


def test_caller_limit_cannot_exceed_cap():
    r = execute_sql("SELECT order_id FROM orders", max_rows=5000)
    assert r.max_rows == MAX_ROWS
    assert r.row_count == MAX_ROWS
    assert r.truncated is True


def test_statement_timeout_fires():
    with pytest.raises(ExecutionError) as ei:
        execute_sql("SELECT pg_sleep(3)", validate=False, statement_timeout_ms=1000)
    assert ei.value.code == "statement_timeout"
    assert ei.value.sqlstate == "57014"


def test_write_rejected_by_validator():
    with pytest.raises(SQLValidationError):
        execute_sql("UPDATE orders SET order_status = 'delivered'")


def test_write_rejected_by_readonly_txn_when_validation_bypassed():
    # Even with the L2 validator bypassed, the read-only transaction (L4) and the
    # role grants (L3) still block the write.
    with pytest.raises(ExecutionError) as ei:
        execute_sql(
            "INSERT INTO orders (order_id, customer_id, order_status, "
            "order_purchase_timestamp, order_estimated_delivery_date) "
            "VALUES ('__probe__', '__probe__', 'created', now(), now())",
            validate=False,
        )
    assert ei.value.code in {"read_only_transaction", "permission_denied"}


def test_audit_emitted_on_success_and_error(caplog):
    caplog.set_level(logging.INFO, logger="copilot.audit")
    execute_sql("SELECT count(*) FROM orders")
    ok_records = [json.loads(r.message) for r in caplog.records if r.name == "copilot.audit"]
    assert any(rec["event"] == "execution" and rec["status"] == "ok" for rec in ok_records)

    caplog.clear()
    with pytest.raises(ExecutionError):
        execute_sql("SELECT pg_sleep(3)", validate=False, statement_timeout_ms=1000)
    err_records = [json.loads(r.message) for r in caplog.records if r.name == "copilot.audit"]
    assert any(rec["status"] == "error" and rec["error_code"] == "statement_timeout" for rec in err_records)
    # no raw result rows are ever logged (metadata keys like max_rows are fine)
    assert all("rows" not in rec for rec in err_records)
    assert all("rows" not in rec for rec in ok_records)


def test_data_unchanged_after_execution():
    tables = ["customers", "order_items", "order_payments", "order_reviews",
              "orders", "product_category_translation", "products"]
    before = {t: execute_sql(f"SELECT count(*) FROM {t}").rows[0][0] for t in tables}
    # a capped read and a failing write attempt
    execute_sql("SELECT order_id FROM orders", max_rows=10)
    with pytest.raises(ExecutionError):
        execute_sql("DELETE FROM orders WHERE order_id = '__none__'", validate=False)
    after = {t: execute_sql(f"SELECT count(*) FROM {t}").rows[0][0] for t in tables}
    assert before == after