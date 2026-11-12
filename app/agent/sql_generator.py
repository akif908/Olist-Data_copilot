"""Natural-language to SQL generation, grounded in verified knowledge.

Context comes from the Phase 2 knowledge base (live-inspected schema metadata,
business glossary, verified SQL examples). The model is instructed to use only
that knowledge, and its output is post-checked: referenced tables must exist,
answerable questions must produce a single SELECT, and unanswerable questions
must carry a reason instead of a guessed query.

This module generates SQL only. Safe execution is a separate concern
(Phase 5); the light SELECT guard here is a quality check, not the security
boundary.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT
from app.llm.base import LLMResult, LLMService

KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge_base"


class SQLGenerationError(Exception):
    """The model output is unusable or references fabricated schema objects."""


@dataclass(frozen=True)
class SQLGenerationResult:
    answerable: bool
    sql: str | None
    explanation: str
    tables_used: tuple[str, ...]
    metrics_used: tuple[str, ...]
    unsupported_reason: str | None
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(frozen=True)
class KnowledgeBase:
    metadata: dict
    glossary: dict
    examples: list[dict]

    @classmethod
    def load(cls, root: Path = KNOWLEDGE_DIR) -> KnowledgeBase:
        return cls(
            metadata=json.loads(
                (root / "schema_docs" / "schema_metadata.json").read_text("utf-8")
            ),
            glossary=json.loads(
                (root / "business_glossary" / "glossary.json").read_text("utf-8")
            ),
            examples=json.loads(
                (root / "sql_examples" / "examples.json").read_text("utf-8")
            )["examples"],
        )

    @property
    def table_names(self) -> set[str]:
        return set(self.metadata["tables"])


SYSTEM_PROMPT = """\
You are the SQL generation module of a data analyst copilot for the Olist
Brazilian e-commerce database (PostgreSQL 18, database olist_copilot).

Rules:
1. Use ONLY the tables and columns listed in the provided schema. Never invent
   tables, columns, or relationships.
2. Follow the business glossary definitions exactly. When a question involves
   a metric (revenue, average order value, customers, cancellation rate, ...),
   apply its documented definition and state the interpretation in the
   explanation. If a metric is listed as unsupported, do not compute it.
3. Generate a single PostgreSQL SELECT statement. Never generate INSERT,
   UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, multiple statements, or
   data-modifying CTEs.
4. Prefer explicit column lists over SELECT *. Add LIMIT 100 to queries that
   return detail rows. Always LEFT JOIN product_category_translation (it is
   incomplete) and COALESCE to the Portuguese name, then '(unknown)'.
5. If the question cannot be answered from the schema and glossary (missing
   data, unsupported metric, out of scope), set answerable=false and give the
   reason in unsupported_reason. Never guess.
6. Monetary values are BRL; round monetary results to 2 decimals.

Respond with ONLY a JSON object — no markdown fences, no prose — with exactly
these fields:
{
  "answerable": true or false,
  "sql": "the SELECT statement" or null,
  "explanation": "short explanation of the query and metric interpretation",
  "tables_used": ["tables actually referenced"],
  "metrics_used": ["glossary metric ids applied, if any"],
  "unsupported_reason": "why it is not answerable" or null
}
"""


def schema_context(metadata: dict) -> str:
    lines = ["=== DATABASE SCHEMA (verified by live inspection) ==="]
    for name, t in metadata["tables"].items():
        lines.append(f"\nTABLE {name} ({t['row_count']} rows): {t['description']}")
        lines.append(f"  PRIMARY KEY ({', '.join(t['primary_key'])})")
        for fk in t["foreign_keys"]:
            lines.append(
                f"  FOREIGN KEY ({', '.join(fk['columns'])}) REFERENCES "
                f"{fk['referenced_table']} ({', '.join(fk['referenced_columns'])})"
            )
        for cname, c in t["columns"].items():
            null = "NULL" if c["is_nullable"] else "NOT NULL"
            lines.append(f"  - {cname} {c['data_type']} {null}: {c['description']}")
    return "\n".join(lines)


def glossary_context(glossary: dict) -> str:
    lines = ["=== BUSINESS GLOSSARY (authoritative metric definitions) ==="]
    for m in glossary["metrics"]:
        lines.append(f"\nMETRIC {m['id']} — {m['name']}: {m['definition']}")
        lines.append(f"  SQL: {m['sql']}")
        lines.append(f"  Filters: {m['filters']}")
        for c in m["caveats"]:
            lines.append(f"  Caveat: {c}")
    lines.append("\nUNSUPPORTED METRICS (must not be computed):")
    for m in glossary["unsupported_metrics"]:
        lines.append(f"  - {m['id']}: {m['reason']}")
    return "\n".join(lines)


def examples_context(examples: list[dict]) -> str:
    lines = ["=== VERIFIED SQL EXAMPLES (executed against this database) ==="]
    for ex in examples:
        lines.append(f"\nQ: {ex['question']}\nSQL: {ex['sql']}")
    return "\n".join(lines)


def build_prompt(question: str, kb: KnowledgeBase) -> str:
    return "\n\n".join([
        schema_context(kb.metadata),
        glossary_context(kb.glossary),
        examples_context(kb.examples),
        f"=== USER QUESTION ===\n{question}",
    ])


def parse_json_response(text: str) -> dict:
    """Parse a JSON object from model output, tolerating fences/prose."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise SQLGenerationError("LLM did not return a valid JSON object")


def _validate_result(data: dict, kb: KnowledgeBase, llm_result: LLMResult) -> SQLGenerationResult:
    answerable = bool(data.get("answerable"))
    tables_used = tuple(str(t) for t in (data.get("tables_used") or []))
    metrics_used = tuple(str(m) for m in (data.get("metrics_used") or []))
    explanation = str(data.get("explanation") or "")

    unknown = set(tables_used) - kb.table_names
    if unknown:
        raise SQLGenerationError(
            f"model referenced tables that do not exist: {sorted(unknown)}"
        )

    sql: str | None = None
    unsupported_reason: str | None = None
    if answerable:
        raw_sql = data.get("sql")
        if not isinstance(raw_sql, str) or not raw_sql.strip():
            raise SQLGenerationError("answerable=true but no SQL was produced")
        sql = raw_sql.strip().rstrip(";").strip()
        # Light quality guard — full SQLGlot validation arrives in Phase 5.
        if not sql.lower().startswith("select") or ";" in sql:
            raise SQLGenerationError("model produced something other than a single SELECT")
    else:
        unsupported_reason = str(data.get("unsupported_reason") or explanation or "")
        if not unsupported_reason:
            raise SQLGenerationError("answerable=false but no reason was given")

    return SQLGenerationResult(
        answerable=answerable,
        sql=sql,
        explanation=explanation,
        tables_used=tables_used,
        metrics_used=metrics_used,
        unsupported_reason=unsupported_reason,
        model=llm_result.model,
        prompt_tokens=llm_result.prompt_tokens,
        completion_tokens=llm_result.completion_tokens,
    )


def generate_sql(
    question: str,
    llm: LLMService,
    kb: KnowledgeBase | None = None,
) -> SQLGenerationResult:
    """Generate SQL for a natural-language question. Does not execute it.

    Reasoning models sometimes spend their token budget on reasoning and
    emit truncated or non-JSON output. We allow one extra attempt with a
    stricter reminder when JSON parsing fails. (Full SQL error correction
    with metadata re-retrieval is Phase 6.)
    """
    kb = kb or KnowledgeBase.load()
    base_prompt = build_prompt(question, kb)
    prompt = base_prompt
    last_error: SQLGenerationError | None = None
    for attempt in range(2):
        llm_result = llm.generate(
            prompt, system=SYSTEM_PROMPT, temperature=0.0, max_tokens=4096
        )
        try:
            data = parse_json_response(llm_result.text)
            break
        except SQLGenerationError as exc:
            last_error = exc
            prompt = (
                base_prompt
                + "\n\nYour previous reply was not valid JSON. Respond again with "
                "ONLY the JSON object — no markdown, no prose, no truncation."
            )
    else:
        raise last_error  # type: ignore[misc]
    return _validate_result(data, kb, llm_result)