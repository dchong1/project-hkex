"""
LLM summarizer — Grok (xAI) by default, structured so you can swap providers.

Flow:
  1. Download PDF (host allowlist) with size cap.
  2. Extract text from the first N pages with pdfplumber.
  3. Call OpenAI-compatible Chat Completions at api.x.ai.
  4. If PDF fails, fall back to title + category_text only (commented in code below).
"""

from __future__ import annotations

import io
import logging
import os
import time
from typing import Protocol
from urllib.parse import urlparse

import pdfplumber
import requests

import config as app_config

logger = logging.getLogger(__name__)

DEFAULT_GROK_MODEL = "grok-3-mini"
MAX_WORDS = 70
MAX_PDF_BYTES = 25 * 1024 * 1024
FIRST_PAGES = 3
MAX_EXCERPT_CHARS = 8000
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"


class Summarizer(Protocol):
    def summarize(
        self,
        *,
        pdf_url: str,
        document_title: str,
        category_text: str,
    ) -> str: ...


def _truncate_words(text: str, max_words: int = MAX_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).rstrip(",.;:") + "…"


def _pdf_host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in app_config.PDF_HOST_ALLOWLIST)


def _download_pdf(url: str) -> bytes:
    if not _pdf_host_allowed(url):
        raise ValueError(f"PDF host not in allowlist: {url!r}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = 0
        chunks: list[bytes] = []
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise ValueError("PDF exceeds MAX_PDF_BYTES cap")
            chunks.append(chunk)
    return b"".join(chunks)


def _extract_pdf_text(data: bytes) -> str:
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        n = min(FIRST_PAGES, len(pdf.pages))
        for i in range(n):
            try:
                t = pdf.pages[i].extract_text() or ""
            except Exception as e:
                logger.warning("pdfplumber extract error page %s: %s", i, e)
                t = ""
            if t:
                parts.append(t)
    text = "\n".join(parts)
    return text[:MAX_EXCERPT_CHARS]


class GrokSummarizer:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or app_config.grok_api_key()
        self.model = model or os.environ.get("GROK_MODEL") or DEFAULT_GROK_MODEL

    def _chat(self, system: str, user: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": 220,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        last_err: Exception | None = None
        for attempt in range(4):
            r = requests.post(XAI_CHAT_URL, headers=headers, json=body, timeout=120)
            if r.status_code == 200:
                data = r.json()
                choices = data.get("choices") or []
                if choices and choices[0].get("message", {}).get("content"):
                    return str(choices[0]["message"]["content"]).strip()
                raise RuntimeError(f"Unexpected Grok response: {data!r}")
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 2**attempt + 0.2 * attempt
                logger.warning("Grok API retry (%s) in %.1fs", r.status_code, wait)
                time.sleep(wait)
                last_err = RuntimeError(r.text)
                continue
            raise RuntimeError(f"Grok API error {r.status_code}: {r.text}")
        raise last_err or RuntimeError("Grok API failed")

    def summarize(
        self,
        *,
        pdf_url: str,
        document_title: str,
        category_text: str,
    ) -> str:
        system = (
            "You summarize Hong Kong stock exchange (HKEX) regulatory filings for a busy reader. "
            "Be factual and neutral. No markdown or bullet points. English only. "
            "Prioritize concrete details when the text supports them: type of corporate action, "
            "key amounts and currency, important dates or deadlines, and main parties or instruments. "
            f"At most {MAX_WORDS} words."
        )

        excerpt = ""
        try:
            data = _download_pdf(pdf_url)
            excerpt = _extract_pdf_text(data)
        except Exception as e:
            # Fallback path: title + category only (no PDF body).
            logger.warning("PDF download/extract failed, using title fallback: %s", e)

        if excerpt.strip():
            user = (
                f"Title: {document_title}\n"
                f"Category: {category_text}\n\n"
                f"Excerpt from filing (first pages):\n{excerpt}\n\n"
                f"Summarize in {MAX_WORDS} words or fewer."
            )
        else:
            user = (
                f"Title: {document_title}\n"
                f"Category: {category_text}\n\n"
                "No PDF text available. Summarize what this filing likely concerns "
                f"from the title and category only, in {MAX_WORDS} words or fewer."
            )

        raw = self._chat(system, user)
        # Strip quotes / formatting artifacts
        raw = raw.replace("\n", " ").strip()
        return _truncate_words(raw, MAX_WORDS)
