"""
Application config: watchlist, search window, categories, and URLs.

Secrets load from environment (and .env via main.py / dotenv).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List

import yaml

# --- Watchlist & search ---

_WATCHLIST_YAML = Path(__file__).resolve().parent / "watchlist.yaml"


def _normalize_stock_code(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.isdigit() and len(s) < 5:
        return s.zfill(5)
    return s


def _load_watchlist_from_yaml(path: Path) -> List[str]:
    """Load deduplicated stock codes from repo-root watchlist.yaml."""
    if not path.is_file():
        raise RuntimeError(
            f"Watchlist file not found: {path}. "
            "Add watchlist.yaml at the repo root or restore it from git."
        )
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        raise RuntimeError(f"Watchlist file is empty: {path}")

    codes: List[str] = []
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                codes.append(_normalize_stock_code(entry.get("code")))
            else:
                codes.append(_normalize_stock_code(entry))
    elif isinstance(data, dict):
        stocks = data.get("stocks")
        if stocks is None:
            stocks = data.get("watchlist")
        if stocks is None:
            raise RuntimeError(
                f"Watchlist file {path} must contain a 'stocks' or 'watchlist' list."
            )
        if not isinstance(stocks, list):
            raise RuntimeError(
                f"Watchlist 'stocks' in {path} must be a list, got {type(stocks).__name__}."
            )
        for entry in stocks:
            if isinstance(entry, dict):
                codes.append(_normalize_stock_code(entry.get("code")))
            else:
                codes.append(_normalize_stock_code(entry))
    else:
        raise RuntimeError(
            f"Watchlist file {path} must be a YAML list or mapping with 'stocks'."
        )

    deduped = list(dict.fromkeys(c for c in codes if c))
    if not deduped:
        raise RuntimeError(f"Watchlist file {path} contains no stock codes.")
    return deduped


# Edit watchlist.yaml (not this file) to change tracked codes; commit to apply.
WATCHLIST: List[str] = _load_watchlist_from_yaml(_WATCHLIST_YAML)

# HKEX search "from" date and post-filter: only keep rows whose release_time is within
# the last N days from now (Asia/Hong_Kong).
DAYS_BACK: int = 7

# HKEX "Headline Category" labels (tier2). One search is run per label, then merged.
TARGET_CATEGORIES: List[str] = ["Announcements and Notices", "All"]

TITLE_SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en"

# Allowed host for PDF downloads (SSRF guard in llm_summarizer)
PDF_HOST_ALLOWLIST = ("www1.hkexnews.hk", "www2.hkexnews.hk", "hkexnews.hk")

SUMMARIZER_MODES = frozenset({"none", "grok", "openai"})


def get_env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is not None and v.strip() != "":
        return v
    return default


def require_env(name: str) -> str:
    v = get_env(name)
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v


def _normalize_summarizer_mode(raw: str) -> str:
    mode = raw.strip().lower()
    if mode in ("openai-compatible", "openai_compatible"):
        return "openai"
    return mode


def resolve_summarizer_mode() -> str:
    """
    Summarizer backend selection.

    - Explicit SUMMARIZER or LLM_PROVIDER wins (none | grok | openai | openai-compatible).
    - If unset and GROK_API_KEY is set → grok (backward compatible).
    - Otherwise → none (zero API cost).
    """
    raw = get_env("SUMMARIZER") or get_env("LLM_PROVIDER")
    if raw:
        mode = _normalize_summarizer_mode(raw)
        if mode not in SUMMARIZER_MODES:
            allowed = ", ".join(sorted(SUMMARIZER_MODES | {"openai-compatible"}))
            raise RuntimeError(
                f"Invalid SUMMARIZER/LLM_PROVIDER {raw!r}; expected one of: {allowed}"
            )
        return mode
    if get_env("GROK_API_KEY"):
        return "grok"
    return "none"


def grok_api_key() -> str:
    """xAI / Grok API key (required when SUMMARIZER=grok)."""
    return require_env("GROK_API_KEY")


def openai_compat_chat_url() -> str:
    """OpenAI-compatible Chat Completions URL (required when SUMMARIZER=openai)."""
    base = require_env("LLM_BASE_URL").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def openai_compat_model() -> str:
    return require_env("LLM_MODEL")


def openai_compat_api_key() -> str:
    return get_env("LLM_API_KEY") or ""
