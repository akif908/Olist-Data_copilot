"""Application configuration loaded from environment variables (.env).

Secrets are never hard-coded. See .env.example for the required variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class DatabaseSettings:
    """PostgreSQL connection settings (Phase 1: admin/import user)."""

    host: str
    port: int
    dbname: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> DatabaseSettings:
        return cls(
            host=os.getenv("PGHOST", "localhost"),
            port=int(os.getenv("PGPORT", "5432")),
            dbname=os.getenv("PGDATABASE", "olist_copilot"),
            user=os.getenv("PGUSER", "postgres"),
            password=os.getenv("PGPASSWORD", ""),
        )


@dataclass(frozen=True)
class LLMSettings:
    """Provider-independent LLM settings.

    Reads LLM_PROVIDER and then <PROVIDER>_API_KEY / <PROVIDER>_MODEL /
    <PROVIDER>_BASE_URL, so adding a provider needs no config changes.
    The API key is never logged or serialized.
    """

    provider: str
    api_key: str
    model: str
    base_url: str

    @classmethod
    def from_env(cls) -> LLMSettings:
        provider = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()
        prefix = provider.upper()
        return cls(
            provider=provider,
            api_key=os.getenv(f"{prefix}_API_KEY", ""),
            model=os.getenv(f"{prefix}_MODEL", ""),
            base_url=os.getenv(f"{prefix}_BASE_URL", ""),
        )