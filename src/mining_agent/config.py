"""Central configuration, resolved from environment variables.

Every server and the agent read the same :class:`Settings` object so that a
single ``.env`` file drives the whole pipeline.  Prefix env vars with
``MINING_AGENT_`` (``ANTHROPIC_API_KEY`` and ``NEWS_API_KEY`` are read as-is so
they line up with what the vendors document).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

VALID_MODES = ("offline", "live", "auto")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _default_data_dir() -> Path:
    """Repo-relative ``data/`` when running from a checkout, else ``./data``."""
    here = Path(__file__).resolve()
    # src/mining_agent/config.py -> <root>/data
    for parent in here.parents:
        candidate = parent / "data"
        if (candidate / "fixtures").is_dir():
            return candidate
    return Path.cwd() / "data"


@dataclass(frozen=True)
class Settings:
    # ---- data source -------------------------------------------------
    data_mode: str = "auto"
    """``offline`` = bundled fixtures only, ``live`` = network providers only,
    ``auto`` = live when credentials are present, otherwise offline."""

    data_dir: Path = field(default_factory=_default_data_dir)

    # ---- mining-news-mcp --------------------------------------------
    news_provider: str = "auto"          # auto | newsapi | offline
    news_api_key: str | None = None
    news_api_url: str = "https://newsapi.org/v2/everything"

    # ---- mineral-pdf-mcp --------------------------------------------
    pdf_provider: str = "auto"           # auto | http | offline
    pdf_allow_network: bool = True
    pdf_max_bytes: int = 40 * 1024 * 1024

    # ---- lme-price-mcp ----------------------------------------------
    price_provider: str = "auto"         # auto | http | offline
    price_base_url: str | None = None
    price_api_key: str | None = None

    # ---- agent -------------------------------------------------------
    anthropic_api_key: str | None = None
    model: str = "claude-opus-5"
    effort: str = "high"                 # low | medium | high | xhigh | max
    max_tokens: int = 16000
    max_iterations: int = 8
    enable_refusal_fallback: bool = True
    llm_enabled: bool = True
    language: str = "zh"

    # ---- observability ----------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_mode=(_env("MINING_AGENT_DATA_MODE", "auto") or "auto").lower(),
            data_dir=Path(_env("MINING_AGENT_DATA_DIR") or _default_data_dir()),
            news_provider=(_env("MINING_AGENT_NEWS_PROVIDER", "auto") or "auto").lower(),
            news_api_key=_env("NEWS_API_KEY"),
            news_api_url=_env("MINING_AGENT_NEWS_API_URL", cls.news_api_url) or cls.news_api_url,
            pdf_provider=(_env("MINING_AGENT_PDF_PROVIDER", "auto") or "auto").lower(),
            pdf_allow_network=_env_bool("MINING_AGENT_PDF_ALLOW_NETWORK", True),
            pdf_max_bytes=_env_int("MINING_AGENT_PDF_MAX_BYTES", 40 * 1024 * 1024),
            price_provider=(_env("MINING_AGENT_PRICE_PROVIDER", "auto") or "auto").lower(),
            price_base_url=_env("MINING_AGENT_PRICE_BASE_URL"),
            price_api_key=_env("MINING_AGENT_PRICE_API_KEY"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            model=_env("MINING_AGENT_MODEL", "claude-opus-5") or "claude-opus-5",
            effort=(_env("MINING_AGENT_EFFORT", "high") or "high").lower(),
            max_tokens=_env_int("MINING_AGENT_MAX_TOKENS", 16000),
            max_iterations=_env_int("MINING_AGENT_MAX_ITERATIONS", 8),
            enable_refusal_fallback=_env_bool("MINING_AGENT_ENABLE_REFUSAL_FALLBACK", True),
            llm_enabled=_env_bool("MINING_AGENT_LLM", True),
            language=(_env("MINING_AGENT_LANGUAGE", "zh") or "zh").lower(),
            log_level=(_env("MINING_AGENT_LOG_LEVEL", "INFO") or "INFO").upper(),
            log_json=_env_bool("MINING_AGENT_LOG_JSON", False),
        )

    # ------------------------------------------------------------------
    def resolve_news_provider(self) -> str:
        if self.news_provider != "auto":
            return self.news_provider
        if self.data_mode == "offline":
            return "offline"
        return "newsapi" if self.news_api_key else "offline"

    def resolve_pdf_provider(self) -> str:
        if self.pdf_provider != "auto":
            return self.pdf_provider
        if self.data_mode == "offline" or not self.pdf_allow_network:
            return "offline"
        return "http"

    def resolve_price_provider(self) -> str:
        if self.price_provider != "auto":
            return self.price_provider
        if self.data_mode == "offline":
            return "offline"
        return "http" if self.price_base_url else "offline"

    @property
    def fixtures_dir(self) -> Path:
        return self.data_dir / "fixtures"

    def describe(self) -> dict[str, str]:
        """Non-secret summary, useful in logs and the report footer."""
        return {
            "data_mode": self.data_mode,
            "news_provider": self.resolve_news_provider(),
            "pdf_provider": self.resolve_pdf_provider(),
            "price_provider": self.resolve_price_provider(),
            "model": self.model,
        }
