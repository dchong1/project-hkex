"""
LLM summarizer — pluggable backends (none / Grok / OpenAI-compatible).

Flow (LLM modes):
  1. Download PDF (host allowlist) with size cap.
  2. Extract text from the first N pages with pdfplumber.
  3. Call Chat Completions (xAI or any OpenAI-compatible base URL).
  4. If PDF fails, fall back to title + category_text only.

SUMMARIZER=none skips PDF download and uses a deterministic title/category blurb.
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
NO_LLM_NOTE = " (No LLM; title/category only.)"


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


def none_fallback_summary(document_title: str, category_text: str) -> str:
    """Deterministic summary when SUMMARIZER=none (no network)."""
    title = (document_title or "HKEX announcement").strip()
    cat = (category_text or "General").strip()
    body = f"{title}. Category: {cat}.{NO_LLM_NOTE}"
    return _truncate_words(body, MAX_WORDS)


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


def _system_prompt() -> str:
    return (
        "You summarize Hong Kong stock exchange (HKEX) regulatory filings for a busy reader. "
        "Be factual and neutral. No markdown or bullet points. English only. "
        "Prioritize concrete details when the text supports them: type of corporate action, "
        "key amounts and currency, important dates or deadlines, and main parties or instruments. "
        f"At most {MAX_WORDS} words."
    )


def _user_prompt(document_title: str, category_text: str, excerpt: str) -> str:
    if excerpt.strip():
        return (
            f"Title: {document_title}\n"
            f"Category: {category_text}\n\n"
            f"Excerpt from filing (first pages):\n{excerpt}\n\n"
            f"Summarize in {MAX_WORDS} words or fewer."
        )
    return (
        f"Title: {document_title}\n"
        f"Category: {category_text}\n\n"
        "No PDF text available. Summarize what this filing likely concerns "
        f"from the title and category only, in {MAX_WORDS} words or fewer."
    )


def _pdf_excerpt(pdf_url: str) -> str:
    try:
        data = _download_pdf(pdf_url)
        return _extract_pdf_text(data)
    except Exception as e:
        logger.warning("PDF download/extract failed, using title fallback: %s", e)
        return ""


class NoneSummarizer:
    def summarize(
        self,
        *,
        pdf_url: str,
        document_title: str,
        category_text: str,
    ) -> str:
        del pdf_url  # no PDF fetch in zero-cost mode
        return none_fallback_summary(document_title, category_text)


class _ChatCompletionsSummarizer:
    def __init__(self, *, api_key: str, model: str, chat_url: str) -> None:
        self.api_key = api_key
        self.model = model
        self.chat_url = chat_url

    def _chat(self, system: str, user: str) -> str:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
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
            r = requests.post(self.chat_url, headers=headers, json=body, timeout=120)
            if r.status_code == 200:
                data = r.json()
                choices = data.get("choices") or []
                if choices and choices[0].get("message", {}).get("content"):
                    return str(choices[0]["message"]["content"]).strip()
                raise RuntimeError(f"Unexpected chat response: {data!r}")
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 2**attempt + 0.2 * attempt
                logger.warning("Chat API retry (%s) in %.1fs", r.status_code, wait)
                time.sleep(wait)
                last_err = RuntimeError(r.text)
                continue
            raise RuntimeError(f"Chat API error {r.status_code}: {r.text}")
        raise last_err or RuntimeError("Chat API failed")

    def summarize(
        self,
        *,
        pdf_url: str,
        document_title: str,
        category_text: str,
    ) -> str:
        excerpt = _pdf_excerpt(pdf_url)
        user = _user_prompt(document_title, category_text, excerpt)
        raw = self._chat(_system_prompt(), user)
        raw = raw.replace("\n", " ").strip()
        return _truncate_words(raw, MAX_WORDS)


class GrokSummarizer(_ChatCompletionsSummarizer):
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        super().__init__(
            api_key=api_key or app_config.grok_api_key(),
            model=model or os.environ.get("GROK_MODEL") or DEFAULT_GROK_MODEL,
            chat_url=XAI_CHAT_URL,
        )


class OpenAICompatibleSummarizer(_ChatCompletionsSummarizer):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        chat_url: str | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key if api_key is not None else app_config.openai_compat_api_key(),
            model=model or app_config.openai_compat_model(),
            chat_url=chat_url or app_config.openai_compat_chat_url(),
        )


def create_summarizer() -> Summarizer:
    mode = app_config.resolve_summarizer_mode()
    if mode == "none":
        return NoneSummarizer()
    if mode == "grok":
        return GrokSummarizer()
    if mode == "openai":
        return OpenAICompatibleSummarizer()
    raise RuntimeError(f"Unsupported summarizer mode: {mode!r}")


def summarizer_mode_label() -> str:
    return app_config.resolve_summarizer_mode()
