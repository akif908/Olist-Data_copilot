"""Abstract LLM interface.

The application depends only on this interface — never on a concrete
provider. Adding Groq, Gemini, or another provider means implementing
LLMService and registering it in provider_factory; no other module changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class LLMError(Exception):
    """LLM configuration, transport, or response failure (sanitized message)."""


@dataclass(frozen=True)
class LLMResult:
    """A completed LLM generation with token usage when available."""

    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMService(ABC):
    """Minimal text-generation interface used by the SQL generator."""

    provider: str = "abstract"

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResult:
        """Generate a completion. Implementations must not leak credentials."""
        raise NotImplementedError