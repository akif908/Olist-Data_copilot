"""Phase 2 tests: every SQL example executes and matches its verified record.

The verified record (verified_results.json) is produced by
`python -m scripts.verify_sql_examples` from real database output.
"""
from __future__ import annotations

import json
from typing import Iterator

import psycopg
import pytest

from app.config import PROJECT_ROOT
from app.database.connection import get_connection
from scripts.verify_sql_examples import check_select_only, load_examples

RESULTS_PATH = PROJECT_ROOT / "knowledge_base" / "sql_examples" / "verified_results.json"


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection]:
    with get_connection() as c:
        c.read_only = True
        yield c


@pytest.fixture(scope="module")
def examples() -> list[dict]:
    return load_examples()


@pytest.fixture(scope="module")
def verified() -> dict:
    return json.loads(RESULTS_PATH.read_text(encoding="utf-8"))["results"]


def test_examples_are_single_selects(examples: list[dict]) -> None:
    for ex in examples:
        check_select_only(ex["sql"])  # raises on anything else


def test_every_example_has_verified_record(examples: list[dict], verified: dict) -> None:
    assert {e["id"] for e in examples} == set(verified)


@pytest.mark.parametrize("example", load_examples(), ids=lambda e: e["id"])
def test_example_executes_and_matches_record(
    conn: psycopg.Connection, example: dict, verified: dict
) -> None:
    cur = conn.execute(example["sql"])
    rows = cur.fetchall()
    record = verified[example["id"]]
    assert len(rows) == record["row_count"], f"{example['id']}: row count drifted"
    if record["scalar"] is not None:
        assert len(rows) == 1 and len(rows[0]) == 1
        actual, expected = rows[0][0], record["scalar"]
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            assert float(actual) == pytest.approx(float(expected), rel=1e-9)
        else:
            assert str(actual) == str(expected)