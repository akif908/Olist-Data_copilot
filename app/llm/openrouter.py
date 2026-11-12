"""OpenRouter LLM service (OpenAI-compatible chat completions API).

Uses plain HTTP (httpx) — no provider SDK lock-in. Structured output is
prompt-based because model support for response_format varies across
OpenRouter models; callers parse JSON defensively.

Credentials are read from configuration and never logged or included in
error messages.
"""
from __future__ import annotations

import time

import httpx

from app.llm.base import LLMError, LLMResult, LLMService

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "cohere/north-mini-code:free"
APP_TITLE = "Data Analyst Copilot"


class OpenRouterService(LLMService):
    provider = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 90.0,
    ) -> None:
        if not api_key:
            raise LLMError(
                "OPENROUTER_API_KEY is not set. Add it to .env (see .env.example)."
            )
        self._api_key = api_key
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResult:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Title": APP_TITLE,
        }

        # Free models are frequently rate-limited: one short retry on 429.
        for attempt in range(2):
            try:
                resp = httpx.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                    # Ignore environment proxy settings: some Windows setups
                    # have NO_PROXY entries (e.g. bare ::1) that crash httpx's
                    # URL parser. The app connects directly; if you are behind
                    # a corporate proxy, configure it explicitly here.
                    trust_env=False,
                )
            except httpx.HTTPError as exc:
                raise LLMError(f"OpenRouter request failed: {type(exc).__name__}") from exc
            if resp.status_code == 429 and attempt == 0:
                time.sleep(3)
                continue
            break

        if resp.status_code != 200:
            raise LLMError(self._sanitize_error(resp))

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("OpenRouter returned an unexpected response shape") from exc

        usage = data.get("usage") or {}
        return LLMResult(
            text=text or "",
            model=data.get("model", self.model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    @staticmethod
    def _sanitize_error(resp: httpx.Response) -> str:
        """Status + provider error message only. Never includes the API key."""
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        detail = (detail or resp.text or "")[:300]
        return f"OpenRouter API error {resp.status_code}: {detail}"