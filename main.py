#!/usr/bin/env python3
"""
Orchestration: HKEX fetch → Notion dedupe → Grok summary → Notion create.
"""

from __future__ import annotations

import logging
import sys
import time

from dotenv import load_dotenv

import config as app_config
from hkex_fetcher import fetch_announcements
from llm_summarizer import GrokSummarizer
from notion_client import check_if_exists, create_announcement_page

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Small delay between external calls (Notion / xAI / PDF).
REQUEST_GAP_S = 0.55


def main() -> int:
    load_dotenv()

    token = app_config.require_env("NOTION_TOKEN")
    database_id = app_config.require_env("NOTION_DATABASE_ID")
    # Warm-check Grok key early
    app_config.grok_api_key()

    summarizer = GrokSummarizer()

    logger.info("Fetching HKEX announcements…")
    try:
        items = fetch_announcements()
    except Exception as e:
        logger.error("HKEX fetch failed: %s", e)
        return 1
    logger.info("Fetched %d row(s) after merge", len(items))

    items.sort(key=lambda x: (x.get("release_time") or ""))

    created = 0
    skipped_existing = 0
    errors = 0

    for item in items:
        uid = item.get("unique_id")
        if not uid:
            continue
        try:
            if check_if_exists(database_id, token, uid):
                skipped_existing += 1
                time.sleep(REQUEST_GAP_S)
                continue
        except Exception as e:
            logger.error("Notion query failed for %s: %s", uid, e)
            errors += 1
            time.sleep(REQUEST_GAP_S)
            continue

        pdf_url = item.get("pdf_url") or ""
        if not pdf_url:
            logger.warning("Skipping item without pdf_url: %s", uid)
            continue

        try:
            summary = summarizer.summarize(
                pdf_url=pdf_url,
                document_title=item.get("document_title", ""),
                category_text=item.get("category_text", ""),
            )
            time.sleep(REQUEST_GAP_S)
            create_announcement_page(database_id, token, item, summary)
            created += 1
            logger.info("Created Notion page for %s", uid)
        except Exception as e:
            logger.error("Failed processing %s: %s", uid, e)
            errors += 1

        time.sleep(REQUEST_GAP_S)

    print(
        f"Done. Fetched {len(items)} announcement(s); "
        f"{skipped_existing} already in Notion; {created} new page(s); "
        f"errors={errors}."
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
