#!/usr/bin/env python3
"""
Orchestration: HKEX fetch → Notion dedupe → Grok summary → Notion create.
Interactive prompts when stdin is a TTY (unless --use-config or HKEX_USE_CONFIG=1).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv

import config as app_config
from hkex_fetcher import HEADLINE_GROUP_DATA_VALUE, fetch_announcements
from llm_summarizer import GrokSummarizer
from notion_client import check_if_exists, create_announcement_page

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Small delay between external calls (Notion / xAI / PDF).
REQUEST_GAP_S = 0.55


def _env_use_config() -> bool:
    return os.environ.get("HKEX_USE_CONFIG", "").strip().lower() in ("1", "true", "yes")


def _category_help_text() -> str:
    groups = ", ".join(f'"{k}"' for k in sorted(HEADLINE_GROUP_DATA_VALUE.keys()))
    return (
        "HKEX Headline Category labels must match the Title Search UI exactly (English).\n"
        "  Open Document type → Headline Category, tier-1 “Headline Category”, then copy the tier-2\n"
        "  link text from the dropdown.\n"
        f"  Built-in shortcuts in this tool for tier-2 groups: {groups}\n"
        "  After a primary group name, add a line All (case-insensitive) to include all subtypes under\n"
        "  that group in one search—the same rule as TARGET_CATEGORIES in config.py."
    )


# Interactive menu 2–3; 1 = config, 4 = custom. Tier-2 Headline Category labels; see hkex_fetcher.
_CATEGORY_MENU: list[tuple[str, list[str]]] = [
    ("Circulars", ["Circulars"]),
    ("Listing Documents", ["Listing Documents"]),
]


def _prompt_target_categories(default: list[str]) -> list[str]:
    print("\nHeadline category (HKEX Title Search → Document type → Headline Category, English tier-2):")
    print(f"  1 — Use config TARGET_CATEGORIES {default!r}")
    for i, (label, _) in enumerate(_CATEGORY_MENU, start=2):
        print(f"  {i} — {label}")
    print("  4 — Custom (enter one tier-2 label per line; empty line ends)")
    print(_category_help_text())

    line = input("Choice [1]: ").strip()
    if not line or line == "1":
        return list(default)
    try:
        choice = int(line)
    except ValueError:
        logger.warning("Invalid choice; using config TARGET_CATEGORIES")
        return list(default)
    if choice == 4:
        first = input("Category line 1: ").strip()
        if not first:
            return list(default)
        lines = [first]
        while True:
            nxt = input(f"Category line {len(lines) + 1} (empty to finish): ").strip()
            if not nxt:
                break
            lines.append(nxt)
        return lines
    if 2 <= choice <= 1 + len(_CATEGORY_MENU):
        return list(_CATEGORY_MENU[choice - 2][1])
    logger.warning("Choice out of range; use 1–4. Using config TARGET_CATEGORIES")
    return list(default)


def _prompt_watchlist(default: list[str]) -> list[str]:
    codes_s = ", ".join(default)
    line = input(
        f"Stock codes (comma-separated), or Enter for config watchlist [{codes_s}]: "
    ).strip()
    if not line:
        return list(default)
    return [c.strip() for c in line.split(",") if c.strip()]


def _prompt_days_back(default: int) -> int:
    line = input(
        f"Days back N (HKEX date range and release-time filter; only rows released in the last N days, HK time) [{default}]: "
    ).strip()
    if not line:
        return default
    try:
        n = int(line)
        if n < 0:
            logger.warning("Negative days not allowed; using %s", default)
            return default
        return n
    except ValueError:
        logger.warning("Invalid integer; using %s", default)
        return default


def _resolve_run_settings(args: argparse.Namespace) -> tuple[list[str], int, list[str]]:
    force_cfg = args.use_config or _env_use_config()
    if force_cfg or not sys.stdin.isatty():
        return (
            list(app_config.WATCHLIST),
            app_config.DAYS_BACK,
            list(app_config.TARGET_CATEGORIES),
        )
    print("Interactive mode (config defaults in parentheses). --use-config skips these prompts.\n")
    wl = _prompt_watchlist(app_config.WATCHLIST)
    db = _prompt_days_back(app_config.DAYS_BACK)
    cats = _prompt_target_categories(app_config.TARGET_CATEGORIES)
    return wl, db, cats


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="HKEX title search → Notion, with optional Grok summaries.",
    )
    p.add_argument(
        "--use-config",
        action="store_true",
        help="Use WATCHLIST, DAYS_BACK, and TARGET_CATEGORIES from config only (no prompts).",
    )
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    token = app_config.require_env("NOTION_TOKEN")
    database_id = app_config.require_env("NOTION_DATABASE_ID")
    # Warm-check Grok key early
    app_config.grok_api_key()

    summarizer = GrokSummarizer()

    watchlist, days_back, target_categories = _resolve_run_settings(args)

    logger.info("Fetching HKEX announcements…")
    try:
        items = fetch_announcements(
            watchlist=watchlist,
            days_back=days_back,
            target_categories=target_categories,
        )
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
