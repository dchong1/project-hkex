"""
HKEX Title Search fetcher using Playwright (JS-heavy UI) + BeautifulSoup for parsing.

Tricky bits:
  - The filter panel lives in titleSearchSearchPanel.html; wait for #searchStockCode.
  - Document type is two-step: tier1 must be "Headline Category", then tier2 picks the label.
  - Some headline groups use a second tier2 step (e.g. Announcements and Notices → All), encoded
    as tierTwoId=-2 in HKEX hidden fields; we merge consecutive (X, "All") in TARGET_CATEGORIES
    into one search.
  - Stock codes should be chosen from autocomplete so internal stockId is set (comma-only text
    often yields no matches).
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from zoneinfo import ZoneInfo

import config as app_config

logger = logging.getLogger(__name__)

HKEX_ORIGIN = "https://www1.hkexnews.hk"
TZ_HK = ZoneInfo("Asia/Hong_Kong")

# Playwright timeouts (ms)
PANEL_READY_MS = 60_000
AFTER_CLICK_MS = 15_000
SEARCH_RESULT_MS = 90_000


def _hk_today_range(days_back: int) -> tuple[str, str]:
    """Return (from_date, to_date) as yyyy/mm/dd strings in Asia/Hong_Kong."""
    now = datetime.now(TZ_HK).date()
    start = now - timedelta(days=days_back)
    return start.strftime("%Y/%m/%d"), now.strftime("%Y/%m/%d")


def _maybe_save_debug_html(page, reason: str) -> None:
    if os.environ.get("DEBUG_SAVE_HTML") not in ("1", "true", "yes"):
        return
    path = os.path.join(os.getcwd(), "_hkex_fetch_debug.html")
    try:
        path = os.path.abspath(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(page.content())
        logger.error("Wrote debug HTML to %s (%s)", path, reason)
    except OSError as e:
        logger.error("Could not save debug HTML: %s", e)


def _wait_panel_ready(page) -> None:
    page.locator("#searchStockCode").wait_for(state="visible", timeout=PANEL_READY_MS)


def _dismiss_onetrust_if_present(page) -> None:
    """Cookie banner can block focus/clicks on the search panel (stock autocomplete)."""
    for sel in ("#onetrust-accept-btn-handler", "button:has-text('Accept All')"):
        btn = page.locator(sel)
        try:
            if btn.count() > 0 and btn.first.is_visible(timeout=1200):
                btn.first.click()
                time.sleep(0.55)
                return
        except Exception:
            continue


def _clear_filters(page) -> None:
    clear = page.locator("a.filter__btn-clearallFilters-js")
    if clear.count() and clear.first.is_visible():
        clear.first.click()
        time.sleep(0.4)


def _set_stock_codes(page, watchlist: list[str]) -> None:
    """Pick each ticker from the autocomplete so HKEX binds stockId (required for real results)."""
    box = page.locator("#searchStockCode")
    codes = [c.strip() for c in watchlist if c.strip()]
    if not codes:
        return
    _dismiss_onetrust_if_present(page)
    box.click()
    time.sleep(0.12)
    page.evaluate(
        """() => {
            const el = document.getElementById("searchStockCode");
            if (!el) return;
            el.value = "";
            el.dispatchEvent(new Event("input", { bubbles: true }));
        }""",
    )
    time.sleep(0.15)
    for idx, code in enumerate(codes):
        if idx > 0:
            box.type(", ")
            time.sleep(0.15)
        box.type(code, delay=45)
        sugg = page.locator("#autocomplete-list-0")
        try:
            sugg.wait_for(state="visible", timeout=8000)
            base = sugg.locator("tr.autocomplete-suggestion.narrow:not(.suggestion-viewall)")
            row = base.filter(has_text=re.compile(rf"\b{re.escape(code)}\b"))
            if row.count() == 0:
                row = base.first
            row.click(timeout=10_000)
            time.sleep(0.35)
        except Exception as ex:
            logger.warning("Stock autocomplete failed for %r: %s", code, ex)
    box.press("Tab")
    time.sleep(0.35)


def _expand_category_runs(target_categories: list[str]) -> list[tuple[str, str | None]]:
    """
    Merge (Primary, All) into one HKEX run — matches the UI where All is a second tier2 pick
    under a headline group (tierTwoId=-2).
    """
    out: list[tuple[str, str | None]] = []
    i = 0
    while i < len(target_categories):
        c = target_categories[i].strip()
        if not c:
            i += 1
            continue
        nxt = target_categories[i + 1].strip() if i + 1 < len(target_categories) else ""
        if nxt.lower() == "all":
            out.append((c, "All"))
            i += 2
        else:
            out.append((c, None))
            i += 1
    return out


# HKEX li.droplist-item data-value for top-level Headline Category groups (tier2 first pick).
# "All" under a group is always data-value="-2" immediately after that row in the open list.
HEADLINE_GROUP_DATA_VALUE: dict[str, str] = {
    "Announcements and Notices": "10000",
}


def _hkex_sync_headline_tier_hidden(page, *, tier_two_id: str = "-2", tier_two_gp_id: str = "-2") -> None:
    """Align hidden tier fields with Headline Category + 'All' subtype (see HKEX results page inputs)."""
    page.evaluate(
        """([t1, t2, t2g]) => {
            const sync = (id, val) => {
                const el = document.getElementById(id);
                if (!el) return;
                el.value = val;
                el.dispatchEvent(new Event("change", { bubbles: true }));
                el.dispatchEvent(new Event("input", { bubbles: true }));
            };
            sync("tierOneId", t1);
            sync("tierTwoId", t2);
            sync("tierTwoGpId", t2g);
            sync("searchType", "rbAfter2006");
        }""",
        ["10000", tier_two_id, tier_two_gp_id],
    )
    time.sleep(0.15)


def _tier2_select_headline_group_then_all_js(page, group_data_value: str) -> None:
    """
    Pick a headline group (e.g. Announcements and Notices = 10000), reopen tier2, then pick the first
    following data-value=-2 row (ALL under that group). Playwright-visible clicks fail on this UI; DOM
    events match manual selection.
    """
    page.evaluate(
        """(gv) => {
            const rb = document.querySelector("#rbAfter2006");
            if (!rb) return;
            const fire = (el) => {
                el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
            };
            const open = () => {
                const c = rb.querySelector("a.combobox-field");
                if (c) fire(c);
            };
            open();
            const groupLi = rb.querySelector(`li.droplist-item[data-value="${gv}"]`);
            if (groupLi) fire(groupLi);
        }""",
        group_data_value,
    )
    time.sleep(0.55)
    page.evaluate(
        """(gv) => {
            const rb = document.querySelector("#rbAfter2006");
            if (!rb) return;
            const fire = (el) => {
                el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
            };
            const open = () => {
                const c = rb.querySelector("a.combobox-field");
                if (c) fire(c);
            };
            open();
            const items = rb.querySelectorAll("li.droplist-item");
            for (let i = 0; i < items.length; i++) {
                if (items[i].getAttribute("data-value") !== gv) continue;
                for (let j = i + 1; j < items.length; j++) {
                    if (items[j].getAttribute("data-value") === "-2") {
                        fire(items[j]);
                        return;
                    }
                }
                return;
            }
        }""",
        group_data_value,
    )
    time.sleep(0.45)


def _tier2_click_label_js(page, label: str) -> None:
    """Fallback: open tier2 and click first item whose text equals label (trimmed)."""
    page.evaluate(
        """(label) => {
            const rb = document.querySelector("#rbAfter2006");
            if (!rb) return;
            const combo = rb.querySelector("a.combobox-field");
            if (combo) combo.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
            const links = rb.querySelectorAll(".droplist-item a");
            for (const a of links) {
                if ((a.textContent || "").trim() === label) {
                    a.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                    return;
                }
            }
            for (const li of rb.querySelectorAll("li.droplist-item")) {
                if ((li.textContent || "").trim().startsWith(label)) {
                    li.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                    return;
                }
            }
        }""",
        label,
    )
    time.sleep(0.45)


def _set_date_range(page, date_from: str, date_to: str) -> None:
    # HKEX uses readonly text inputs wired to a datepicker; .fill() is not allowed.
    page.evaluate(
        """([dateFrom, dateTo]) => {
            const fire = (el) => {
                el.dispatchEvent(new Event("input", { bubbles: true }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
                el.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
            };
            for (const [id, val] of [
                ["searchDate-From", dateFrom],
                ["searchDate-To", dateTo],
            ]) {
                const el = document.getElementById(id);
                if (!el) continue;
                el.value = val;
                fire(el);
            }
        }""",
        [date_from, date_to],
    )
    time.sleep(0.25)


def _choose_headline_category_mode(page) -> None:
    """
    Tier1 combobox: ALL | Headline Category | Document Type.
    We need "Headline Category" so tier2 (rbAfter2006) lists modern headlines.
    """
    page.locator("#tier1-select a.combobox-field").click()
    time.sleep(0.25)
    # Bound list is a sibling structure; link text is stable in the panel HTML.
    page.get_by_role("link", name="Headline Category").click()
    time.sleep(0.5)
    page.locator("#rbAfter2006").wait_for(state="visible", timeout=AFTER_CLICK_MS)


def _choose_tier2_primary_and_sub(page, primary: str, sub: str | None) -> None:
    """Pick tier2 headline group; if sub is All, pick ALL under that group via data-value (-2)."""
    gval = HEADLINE_GROUP_DATA_VALUE.get(primary)
    if sub and sub.lower() == "all" and gval:
        _tier2_select_headline_group_then_all_js(page, gval)
        return
    if sub and sub.lower() == "all":
        logger.warning(
            "No data-value mapping for headline group %r + All; set HEADLINE_GROUP_DATA_VALUE or pick a leaf label.",
            primary,
        )
        _tier2_click_label_js(page, primary)
        return
    if gval and not sub:
        page.evaluate(
            """(gv) => {
                const rb = document.querySelector("#rbAfter2006");
                if (!rb) return;
                const combo = rb.querySelector("a.combobox-field");
                if (combo) combo.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                const li = rb.querySelector(`li.droplist-item[data-value="${gv}"]`);
                if (li) li.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
            }""",
            gval,
        )
        time.sleep(0.45)
        return
    _tier2_click_label_js(page, primary)
    if sub:
        rb = page.locator("#rbAfter2006")
        rb.locator("a.combobox-field").first.click()
        time.sleep(0.35)
        try:
            rb.get_by_role("link", name=sub, exact=True).click(timeout=8_000)
        except Exception:
            rb.locator(".droplist-item").filter(has_text=sub).first.click(timeout=8_000, force=True)
        time.sleep(0.4)


def _click_search(page) -> None:
    page.locator("a.filter__btn-applyFilters-js").filter(has_text="SEARCH").click()


def _norecords_message_shown(page) -> bool:
    """HKEX shows .result_norecords for empty results; it may not count as Playwright-visible."""
    loc = page.locator(".result_norecords")
    if loc.count() == 0:
        return False
    try:
        txt = (loc.first.inner_text(timeout=500) or "").lower()
    except Exception:
        return False
    return "no match" in txt


def _wait_for_search_outcome(page) -> None:
    """
    After SEARCH, wait for either data rows or the empty state.
    HKEX renders a hidden sticky-header clone plus the visible grid — match PDF links, not one table.
    """
    deadline = time.time() + SEARCH_RESULT_MS / 1000
    while time.time() < deadline:
        if _norecords_message_shown(page):
            return
        if page.locator("table.sticky-header-table tbody a[href*='.pdf']").count() > 0:
            return
        time.sleep(0.25)
    raise PlaywrightTimeoutError("Timed out waiting for HKEX search results")


def _parse_release_time(text: str) -> str | None:
    """
    Normalize release time cell to ISO 8601 for Notion date properties.
    HKEX often shows DD/MM/YYYY HH:MM (may vary).
    """
    text = " ".join(text.split())
    if not text:
        return None
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2})", text)
    if m:
        d, mo, y, hh, mm = m.groups()
        try:
            return datetime(
                int(y), int(mo), int(d), int(hh), int(mm), tzinfo=TZ_HK
            ).isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
    if m:
        d, mo, y = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), tzinfo=TZ_HK).isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{1,2}):(\d{2})", text)
    if m:
        y, mo, d, hh, mm = m.groups()
        try:
            return datetime(
                int(y), int(mo), int(d), int(hh), int(mm), tzinfo=TZ_HK
            ).isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{4})/(\d{2})/(\d{2})", text)
    if m:
        y, mo, d = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), tzinfo=TZ_HK).isoformat()
        except ValueError:
            pass
    return text


def _basename_no_pdf(url: str) -> str | None:
    path = urlparse(url).path
    base = path.rsplit("/", 1)[-1]
    if base.lower().endswith(".pdf"):
        return base[: -4]
    return None


def _parse_rows(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    merged: dict[str, dict[str, Any]] = {}
    for table in soup.select("table.sticky-header-table"):
        body = table.find("tbody")
        if not body:
            continue
        for tr in body.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 4:
                # Some mobile-only rows may differ; skip
                continue

            td_time = tds[0]
            td_code = tds[1]
            td_name = tds[2]
            td_doc = tds[3]

            pdf_a = None
            for a in td_doc.find_all("a", href=True):
                if ".pdf" in a["href"].lower():
                    pdf_a = a
                    break
            if not pdf_a or not pdf_a.get("href"):
                continue
            pdf_href = pdf_a["href"].strip()
            pdf_url = urljoin(HKEX_ORIGIN, pdf_href)
            unique_id = _basename_no_pdf(pdf_url)
            if not unique_id:
                continue

            raw_time = td_time.get_text(" ", strip=True)
            stock_code = td_code.get_text(" ", strip=True)
            company_short_name = td_name.get_text(" ", strip=True)

            # Document column: headline + title — keep full text for Category parsing in Notion.
            category_text = td_doc.get_text(" ", strip=True)
            document_title = pdf_a.get_text(" ", strip=True) or category_text

            merged[unique_id] = {
                "release_time": _parse_release_time(raw_time),
                "stock_code": stock_code,
                "company_short_name": company_short_name,
                "document_title": document_title,
                "category_text": category_text,
                "pdf_url": pdf_url,
                "unique_id": unique_id,
            }
    return list(merged.values())


def _run_single_search(
    page,
    category_spec: tuple[str, str | None],
    watchlist: list[str],
    days_back: int,
) -> list[dict[str, Any]]:
    primary, sub = category_spec
    date_from, date_to = _hk_today_range(days_back)
    _clear_filters(page)
    _wait_panel_ready(page)
    _set_date_range(page, date_from, date_to)
    _choose_headline_category_mode(page)
    _choose_tier2_primary_and_sub(page, primary, sub)
    if sub and sub.lower() == "all":
        _hkex_sync_headline_tier_hidden(page)
    _set_stock_codes(page, watchlist)
    _click_search(page)
    try:
        _wait_for_search_outcome(page)
    except PlaywrightTimeoutError:
        _maybe_save_debug_html(page, "timeout waiting for results")
        raise
    time.sleep(0.5)
    if _norecords_message_shown(page):
        return []
    return _parse_rows(page.content())


def fetch_announcements(
    watchlist: list[str] | None = None,
    days_back: int | None = None,
    target_categories: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Return announcement dicts for all configured categories, merged by unique_id (last wins).
    """
    wl = watchlist if watchlist is not None else app_config.WATCHLIST
    db = days_back if days_back is not None else app_config.DAYS_BACK
    cats = target_categories if target_categories is not None else app_config.TARGET_CATEGORIES
    runs = _expand_category_runs(cats)
    codes = [c.strip() for c in wl if c.strip()]
    if not codes:
        return []

    merged: dict[str, dict[str, Any]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1400, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(app_config.TITLE_SEARCH_URL, wait_until="domcontentloaded", timeout=120_000)
            _wait_panel_ready(page)
            _dismiss_onetrust_if_present(page)

            # One stock per HKEX run — comma-separated autocomplete breaks after the first pick.
            for stock_code in codes:
                for primary, sub in runs:
                    desc = f"{primary!r}" if not sub else f"{primary!r} → {sub!r}"
                    logger.info("HKEX search: stock=%r %s", stock_code, desc)
                    try:
                        rows = _run_single_search(page, (primary, sub), [stock_code], db)
                    except Exception as e:
                        _maybe_save_debug_html(page, f"error {stock_code} {desc}: {e}")
                        raise
                    for r in rows:
                        merged[r["unique_id"]] = r
                    time.sleep(0.75)
        finally:
            browser.close()

    return list(merged.values())
