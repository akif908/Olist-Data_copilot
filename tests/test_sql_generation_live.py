"""Live Phase 3 verification against the real LLM API.

Skipped automatically when no API key is configured. Consumes API quota
(~7 calls). Generated SQL is executed in a read-only transaction and compared
against the Phase 2 verified reference results — execution success alone is
not the acceptance bar; the numbers must match.
"""
from __future__ import annotations

import json

import pytest

from app.agent.sql_generator import generate_sql
from app.config import PROJECT_ROOT, LLMSettings
from app.database.connection import get_connection
from app.llm.provider_factory import create_llm_service

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not LLMSettings.from_env().api_key, reason="no LLM API key configured in .env"
    ),
]

VERIFIED = json.loads(
    (PROJECT_ROOT / "knowledge_base" / "sql_examples" / "verified_results.json")
    .read_text("utf-8")
)["results"]


@pytest.fixture(scope="module")
def llm():
    return create_llm_service()


def run_readonly(sql: str):
    with get_connection() as conn:
        conn.read_only = True
        conn.execute("SET statement_timeout = '30s'")
        cur = conn.execute(sql)
        return [d.name for d in cur.description], cur.fetchall()


SCALAR_CASES = [
    ("How many orders are in the database?", "ex_order_count"),
    ("How many unique customers exist?", "ex_customer_count"),
    ("How many orders were cancelled?", "ex_canceled_orders"),
    ("What is the average order value under the documented metric definition?",
     "ex_average_order_value"),
]


@pytest.mark.parametrize("question,example_id", SCALAR_CASES)
def test_starter_scalar_question(llm, question: str, example_id: str) -> None:
    result = generate_sql(question, llm)
    assert result.answerable, f"unexpectedly unanswerable: {result.unsupported_reason}"
    _, rows = run_readonly(result.sql)
    assert len(rows) == 1 and len(rows[0]) == 1, "expected a single scalar result"
    expected = VERIFIED[example_id]["scalar"]
    assert float(rows[0][0]) == pytest.approx(float(expected), rel=0.01), (
        f"generated SQL returned {rows[0][0]}, verified reference is {expected}"
    )


def test_top_categories_question(llm) -> None:
    result = generate_sql(
        "What are the top five product categories by order-item count?", llm
    )
    assert result.answerable, result.unsupported_reason
    _, rows = run_readonly(result.sql)
    assert len(rows) == 5
    ref = VERIFIED["ex_top_categories_by_items"]["rows"][0]
    assert str(rows[0][0]) == str(ref[0]), f"top category: {rows[0][0]} != {ref[0]}"
    assert int(rows[0][1]) == int(ref[1])


@pytest.mark.parametrize("question", [
    "What is the net profit margin per product?",
    "What will the weather be in Sao Paulo tomorrow?",
])
def test_unsupported_questions_identified(llm, question: str) -> None:
    result = generate_sql(question, llm)
    assert not result.answerable, "should have been identified as unanswerable"
    assert result.unsupported_reason