# HKEX announcements → Notion (Playwright + Grok)

Small personal tool: scrape **HKEX Listed Company Information Title Search** for a watchlist, skip items already stored (by PDF **Unique ID**), add new rows to a **Notion** database with a short **Grok** summary. The **Document URL** property links to the full PDF on HKEX; the **Summary** is a teaser only (≤40 words).

## Prerequisites

- Python **3.11+**
- [Notion integration](https://developers.notion.com/docs/create-a-notion-integration) with access to your database
- [xAI API key](https://docs.x.ai/docs) for Grok

## Notion database schema

Create a database with these properties (names and types must match unless you change the constants in `notion_client.py`):

| Property name   | Type        | Notes |
|----------------|-------------|--------|
| Title          | Title       | Announcement title |
| Release Date   | Date        | Include time if available |
| Stock Code     | **Text**    | Plain text preserves leading zeros (e.g. `00700`) |
| Company Name   | Text        | |
| Document URL   | URL         | **Open this for the full PDF** |
| Unique ID      | Text        | Dedupe key (PDF filename stem) |
| Category       | Multi-select| Tags from HKEX document column |
| Summary        | Text        | Short LLM summary |
| Status         | Select      | Add option `New` |

Link the integration to the database. The database ID is the 32-character hex in the database URL (with hyphens removed).

## Configuration

1. Copy `.env.example` to `.env` and set `NOTION_TOKEN`, `NOTION_DATABASE_ID`, and `GROK_API_KEY`.
2. Edit **`config.py`**:
   - `WATCHLIST`: stock codes as strings, e.g. `["09888", "00700", "09988"]` — the fetcher runs **one HKEX search per code** (autocomplete cannot reliably bind multiple tickers in one field).
   - `DAYS_BACK`: how far back to search (default `10`)
   - `TARGET_CATEGORIES`: HKEX headline tier2 labels, e.g. `["Announcements and Notices", "All"]` or `["Circulars"]`  
     A pair `["Some headline group", "All"]` is merged into **one** search (second step + hidden `tierTwoId`, matching the site). Otherwise each entry is its own run; results merge by PDF id.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
python main.py
```

Dates use the **Asia/Hong_Kong** timezone when computing the “from / to” range.

## GitHub Actions

Workflow: [`.github/workflows/hkex-to-notion.yml`](.github/workflows/hkex-to-notion.yml)

- **Default schedule**: once daily at `02:00 UTC` (adjust for HKT in the cron expression).
- **Change frequency**: edit the `cron:` line in the workflow file. GitHub Actions **requires a literal cron string** in YAML (not a secret). The file includes commented examples (e.g. twice daily, hourly).
- **Manual run**: Actions → workflow → “Run workflow”.

### Repository secrets

| Secret | Description |
|--------|-------------|
| `NOTION_TOKEN` | Notion internal integration token |
| `NOTION_DATABASE_ID` | Target database ID |
| `GROK_API_KEY` | xAI API key |

Optional: set repository variable **`GROK_MODEL`** (the workflow passes it through) or define it in your shell / `.env` locally. If unset, the default in `llm_summarizer.py` is used.

## Behaviour notes

- **HKEX** may change its HTML/JS; if the fetcher breaks, use `DEBUG_SAVE_HTML=1` locally to dump HTML on failure (see `hkex_fetcher.py`).
- **Rate limits**: small delays between Notion writes and LLM calls.
- **PDF text**: If extraction fails, the summarizer falls back to title + category only.

## Responsible use

Use reasonable intervals, respect HKEX terms and capacity. This is intended for **personal** monitoring, not high-volume scraping.

## Improving summaries later

- Lower **temperature** and tune **max_tokens** for more consistent blurbs.
- Refine the prompt (e.g. “lead with the corporate action; include amounts/dates if in excerpt”).
- Optionally parse more PDF pages or skip TOC-only pages.
- Support bilingual output if filings are mostly Chinese.
