"""Phase 3 tests with a fake LLM and mocked HTTP — no network, no API key.

Covers: provider factory, config, JSON parsing, SQL-generation validation
(anti-fabrication, unsupported questions), and OpenRouter transport behavior.
"""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from app.agent.sql_generator import (
    KnowledgeBase,
    SQLGenerationError,
    build_prompt,
    generate_sql,
    parse_json_response,
)
from app.config import LLMSettings
from app.llm.base import LLMError, LLMResult, LLMService
from app.llm.openrouter import OpenRouterService
from app.llm.provider_factory import create_llm_service


class FakeLLM(LLMService):
    provider = "fake"

    def __init__(self, response: dict | str) -> None:
        self.response = response
        self.last_prompt: str | None = None
        self.last_system: str | None = None

    def generate(self, prompt, *, system=None, temperature=0.0, max_tokens=2048):
        self.last_prompt = prompt
        self.last_system = system
        text = self.response if isinstance(self.response, str) else json.dumps(self.response)
        return LLMResult(text=text, model="fake-model", prompt_tokens=100, completion_tokens=50)


VALID_ANSWER = {
    "answerable": True,
    "sql": "SELECT count(*) AS order_count FROM orders",
    "explanation": "Counts all orders per the order_count metric.",
    "tables_used": ["orders"],
    "metrics_used": ["order_count"],
    "unsupported_reason": None,
}


@pytest.fixture(scope="module")
def kb() -> KnowledgeBase:
    return KnowledgeBase.load()


# --- Config & factory --------------------------------------------------------

def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "some/model:free")
    s = LLMSettings.from_env()
    assert s.provider == "openrouter"
    assert s.api_key == "test-key"
    assert s.model == "some/model:free"


def test_factory_creates_openrouter() -> None:
    svc = create_llm_service(LLMSettings("openrouter", "k", "", ""))
    assert isinstance(svc, OpenRouterService)
    assert svc.model == "cohere/north-mini-code:free"  # default


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(LLMError, match="Unsupported LLM_PROVIDER"):
        create_llm_service(LLMSettings("nosuchprovider", "k", "m", ""))


def test_openrouter_requires_key() -> None:
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        OpenRouterService(api_key="")


# --- JSON parsing ------------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"answerable": true}',
    '```json\n{"answerable": true}\n```',
    'Here is the result:\n{"answerable": true}\nHope this helps!',
])
def test_parse_json_variants(text: str) -> None:
    assert parse_json_response(text) == {"answerable": True}


def test_parse_json_invalid_raises() -> None:
    with pytest.raises(SQLGenerationError):
        parse_json_response("sorry, I cannot help with that")


# --- SQL generation ----------------------------------------------------------

def test_prompt_contains_verified_context(kb: KnowledgeBase) -> None:
    prompt = build_prompt("How many orders?", kb)
    for table in kb.table_names:
        assert table in prompt
    assert "average_order_value" in prompt
    assert "customer_unique_id" in prompt
    assert "VERIFIED SQL EXAMPLES" in prompt
    assert "How many orders?" in prompt


def test_generate_valid_sql(kb: KnowledgeBase) -> None:
    llm = FakeLLM(VALID_ANSWER)
    result = generate_sql("How many orders?", llm, kb)
    assert result.answerable
    assert result.sql == "SELECT count(*) AS order_count FROM orders"
    assert result.tables_used == ("orders",)
    assert result.model == "fake-model"


def test_fabricated_table_rejected(kb: KnowledgeBase) -> None:
    bad = {**VALID_ANSWER, "tables_used": ["orders", "ghost_table"]}
    with pytest.raises(SQLGenerationError, match="do not exist"):
        generate_sql("How many orders?", FakeLLM(bad), kb)


def test_answerable_without_sql_rejected(kb: KnowledgeBase) -> None:
    bad = {**VALID_ANSWER, "sql": None}
    with pytest.raises(SQLGenerationError, match="no SQL"):
        generate_sql("How many orders?", FakeLLM(bad), kb)


def test_non_select_rejected(kb: KnowledgeBase) -> None:
    bad = {**VALID_ANSWER, "sql": "DELETE FROM orders"}
    with pytest.raises(SQLGenerationError, match="single SELECT"):
        generate_sql("delete everything", FakeLLM(bad), kb)


def test_multiple_statements_rejected(kb: KnowledgeBase) -> None:
    bad = {**VALID_ANSWER, "sql": "SELECT 1; SELECT 2"}
    with pytest.raises(SQLGenerationError, match="single SELECT"):
        generate_sql("two queries", FakeLLM(bad), kb)


def test_unanswerable_question(kb: KnowledgeBase) -> None:
    response = {
        "answerable": False, "sql": None,
        "explanation": "Profit cannot be computed.",
        "tables_used": [], "metrics_used": [],
        "unsupported_reason": "No cost or refund fields exist in the dataset.",
    }
    result = generate_sql("What is the profit per product?", FakeLLM(response), kb)
    assert not result.answerable
    assert result.sql is None
    assert "cost" in result.unsupported_reason


def test_unanswerable_without_reason_rejected(kb: KnowledgeBase) -> None:
    bad = {"answerable": False, "sql": None, "explanation": "",
           "tables_used": [], "metrics_used": [], "unsupported_reason": None}
    with pytest.raises(SQLGenerationError, match="no reason"):
        generate_sql("???", FakeLLM(bad), kb)


# --- OpenRouter transport (mocked HTTP) --------------------------------------

def _response(status: int, body: dict) -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


def test_transport_success(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"choices": [{"message": {"content": "hello"}}],
            "model": "cohere/north-mini-code:free",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    post = Mock(return_value=_response(200, body))
    monkeypatch.setattr("app.llm.openrouter.httpx.post", post)
    svc = OpenRouterService(api_key="secret-key")
    result = svc.generate("hi", system="be nice")
    assert result.text == "hello"
    assert result.prompt_tokens == 10
    # key travels in the header, never in the payload
    _, kwargs = post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer secret-key"
    assert "secret-key" not in json.dumps(kwargs["json"])
    assert kwargs["json"]["messages"][0] == {"role": "system", "content": "be nice"}


def test_transport_error_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    post = Mock(return_value=_response(401, {"error": {"message": "Invalid key"}}))
    monkeypatch.setattr("app.llm.openrouter.httpx.post", post)
    svc = OpenRouterService(api_key="secret-key")
    with pytest.raises(LLMError) as excinfo:
        svc.generate("hi")
    assert "401" in str(excinfo.value)
    assert "secret-key" not in str(excinfo.value)


def test_transport_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = _response(200, {"choices": [{"message": {"content": "ok"}}]})
    post = Mock(side_effect=[_response(429, {"error": {"message": "slow down"}}), ok])
    monkeypatch.setattr("app.llm.openrouter.httpx.post", post)
    monkeypatch.setattr("app.llm.openrouter.time.sleep", lambda s: None)
    result = OpenRouterService(api_key="k").generate("hi")
    assert result.text == "ok"
    assert post.call_count == 2