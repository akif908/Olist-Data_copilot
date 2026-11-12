"""Create the configured LLM service.

The single place that knows which providers exist. To add a provider:
implement LLMService, then register it here.
"""
from __future__ import annotations

from app.config import LLMSettings
from app.llm.base import LLMError, LLMService
from app.llm.openrouter import DEFAULT_BASE_URL, DEFAULT_MODEL, OpenRouterService


def create_llm_service(settings: LLMSettings | None = None) -> LLMService:
    settings = settings or LLMSettings.from_env()
    if settings.provider == "openrouter":
        return OpenRouterService(
            api_key=settings.api_key,
            model=settings.model or DEFAULT_MODEL,
            base_url=settings.base_url or DEFAULT_BASE_URL,
        )
    raise LLMError(
        f"Unsupported LLM_PROVIDER '{settings.provider}'. Supported: openrouter."
    )