"""
Application config: watchlist, search window, categories, and URLs.

Secrets load from environment (and .env via main.py / dotenv).
"""

from __future__ import annotations

import os
from typing import List

# --- Watchlist & search (edit for your portfolio) ---

WATCHLIST: List[str] = ["09888"]

# How many days back from "today" (Asia/Hong_Kong) for the date range
DAYS_BACK: int = 10

# HKEX "Headline Category" labels (tier2). One search is run per label, then merged.
TARGET_CATEGORIES: List[str] = ["Announcements and Notices", "All"]

TITLE_SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en"

# Allowed host for PDF downloads (SSRF guard in llm_summarizer)
PDF_HOST_ALLOWLIST = ("www1.hkexnews.hk", "www2.hkexnews.hk", "hkexnews.hk")


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


def grok_api_key() -> str:
    """xAI / Grok API key (required for summaries)."""
    return require_env("GROK_API_KEY")
