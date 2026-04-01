"""
Notion database helpers: dedupe by Unique ID and create announcement pages.

Category handling:
  category_text often looks like: "Announcements and Notices - [Inside Information]"
  We split only on the first " - " so subtypes with hyphens stay intact.
  Brackets around subtypes are stripped for cleaner multi_select tags.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, TypedDict

import requests

logger = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"

# Must match your Notion database property names exactly
PROP_TITLE = "Title"
PROP_RELEASE_DATE = "Release Date"
PROP_STOCK_CODE = "Stock Code"
PROP_COMPANY_NAME = "Company Name"
PROP_DOCUMENT_URL = "Document URL"
PROP_UNIQUE_ID = "Unique ID"
PROP_CATEGORY = "Category"
PROP_SUMMARY = "Summary"
PROP_STATUS = "Status"

STATUS_NEW = "New"

# Notion multi_select option names are capped in practice; keep tags short.
_MAX_TAG_LEN = 100


class AnnouncementItem(TypedDict, total=False):
    release_time: str | None
    stock_code: str
    company_short_name: str
    document_title: str
    category_text: str
    pdf_url: str
    unique_id: str


def _notion_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def parse_category_tags(category_text: str) -> list[str]:
    """
    Build multi_select option names from the HKEX document/headline column.
    Split on the first ' - ' only; strip bracket wrappers from fragments.
    """
    text = " ".join((category_text or "").split())
    if not text:
        return []

    main, sep, rest = text.partition(" - ")
    tags: list[str] = []
    for part in (main, rest):
        if not part:
            continue
        p = part.strip()
        # Remove wrapping [ ... ] if the whole fragment is bracketed
        if p.startswith("[") and p.endswith("]"):
            p = p[1:-1].strip()
        p = re.sub(r"^\[+|\]+$", "", p).strip()
        if p:
            tags.append(p[:_MAX_TAG_LEN])

    # Dedupe case-insensitively, preserve first-seen casing
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _primary_stock_code(stock: str) -> str:
    """If HKEX puts multiple codes in one cell, Notion gets the first token only."""
    s = (stock or "").strip()
    if not s:
        return ""
    for sep in (",", "，", ";"):
        if sep in s:
            return s.split(sep, 1)[0].strip()
    return s


def _rich_text(content: str) -> dict[str, Any]:
    return {
        "rich_text": [
            {
                "type": "text",
                "text": {"content": content[:2000]},
            }
        ]
    }


def _title_prop(content: str) -> dict[str, Any]:
    return {
        "title": [
            {
                "type": "text",
                "text": {"content": content[:2000]},
            }
        ]
    }


def _date_prop(iso_start: str | None) -> dict[str, Any]:
    if not iso_start:
        return {"date": None}
    # Notion accepts date or datetime in start
    return {"date": {"start": iso_start}}


def check_if_exists(database_id: str, token: str, unique_id: str) -> bool:
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    body: dict[str, Any] = {
        "filter": {
            "property": PROP_UNIQUE_ID,
            "rich_text": {"equals": unique_id},
        },
        "page_size": 1,
    }
    last_err: Exception | None = None
    for attempt in range(4):
        r = requests.post(url, headers=_notion_headers(token), json=body, timeout=60)
        if r.status_code == 200:
            data = r.json()
            return len(data.get("results", [])) > 0
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 2**attempt + 0.25 * attempt
            logger.warning("Notion query retry (%s) in %.1fs", r.status_code, wait)
            time.sleep(wait)
            last_err = RuntimeError(r.text)
            continue
        raise RuntimeError(f"Notion query failed {r.status_code}: {r.text}")
    if last_err:
        raise last_err
    return False


def create_announcement_page(
    database_id: str,
    token: str,
    item: AnnouncementItem,
    summary: str,
) -> str:
    """Create a page; return new page id."""
    url = "https://api.notion.com/v1/pages"
    tags = parse_category_tags(item.get("category_text", ""))
    multi = [{"name": t} for t in tags]

    props: dict[str, Any] = {
        PROP_TITLE: _title_prop(item.get("document_title", "Untitled")),
        PROP_RELEASE_DATE: _date_prop(item.get("release_time")),
        PROP_STOCK_CODE: _rich_text(_primary_stock_code(item.get("stock_code", ""))),
        PROP_COMPANY_NAME: _rich_text(item.get("company_short_name", "")),
        PROP_DOCUMENT_URL: {"url": item.get("pdf_url") or None},
        PROP_UNIQUE_ID: _rich_text(item.get("unique_id", "")),
        PROP_CATEGORY: {"multi_select": multi},
        PROP_SUMMARY: _rich_text(summary),
        PROP_STATUS: {"select": {"name": STATUS_NEW}},
    }

    body = {"parent": {"database_id": database_id}, "properties": props}

    last_err: Exception | None = None
    for attempt in range(4):
        r = requests.post(url, headers=_notion_headers(token), json=body, timeout=60)
        if r.status_code == 200:
            return str(r.json().get("id", ""))
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 2**attempt + 0.25 * attempt
            logger.warning("Notion create retry (%s) in %.1fs", r.status_code, wait)
            time.sleep(wait)
            last_err = RuntimeError(r.text)
            continue
        raise RuntimeError(f"Notion create failed {r.status_code}: {r.text}")
    raise last_err or RuntimeError("Notion create failed")
