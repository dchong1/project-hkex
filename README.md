# HKEX announcements → Notion (Playwright + optional LLM)

Small personal tool: scrape **HKEX Listed Company Information Title Search** for a watchlist, skip items already stored (by PDF **Unique ID**), add new rows to a **Notion** database with an optional **summary** (at most **70** words). The **Document URL** property links to the full PDF on HKEX.

## Prerequisites

- Python **3.11+**
- [Notion integration](https://developers.notion.com/docs/create-a-notion-integration) with access to your database
- **Optional:** an LLM provider (Grok/xAI, or any OpenAI-compatible endpoint such as Ollama). Default is **no LLM** (`SUMMARIZER=none`) so clones and CI need no paid API key.

## Notion database schema

Create a database with these properties (names and types must match unless you change the constants in `notion_client.py`):

| Property name   | Type        | Notes |
|----------------|-------------|--------|
| Title          | Title       | Announcement title |
| Release Date   | Date        | Include time if available |
| Stock Code     | **Text**    | Plain text; leading zeros preserved. If HKEX returns several codes in one cell, **only the first** is stored. |
| Company Name   | Text        | |
| Document URL   | URL         | **Open this for the full PDF** |
| Unique ID      | Text        | Dedupe key (PDF filename stem) |
| Category       | Multi-select| Tags from HKEX document column |
| Summary        | Text        | LLM summary (≤70 words; key facts where available) |
| Status         | Select      | Add option `New` |

Link the integration to the database. The database ID is the 32-character hex in the database URL (with hyphens removed).

## Configuration

1. Copy `.env.example` to `.env` and set `NOTION_TOKEN` and `NOTION_DATABASE_ID`.
2. Choose a **summarizer** (see [Summarizer modes](#summarizer-modes)); omit LLM vars for zero-cost runs.
3. Edit **`watchlist.yaml`** for stock codes (commit to change what is tracked). Optional `name` fields are for humans only; the fetcher runs **one HKEX search per `code`** (autocomplete cannot reliably bind multiple tickers in one field).
4. Edit **`config.py`** for other defaults:
   - `DAYS_BACK`: how far back the HKEX **from** date is set, and the rolling window for **keeping** rows by parsed **release time** (Asia/Hong_Kong)
   - `TARGET_CATEGORIES`: HKEX headline tier2 labels, e.g. `["Announcements and Notices", "All"]` or `["Circulars"]`  
     A pair `["Some headline group", "All"]` is merged into **one** search (second step + hidden `tierTwoId`, matching the site). Otherwise each entry is its own run; results merge by PDF id.

## Local setup

Use a **virtual environment** in the project folder so dependencies (including Playwright) install reliably. macOS/Homebrew Python often blocks `pip install` globally (PEP 668); the venv avoids that.

**macOS / Linux (bash or zsh):**

```bash
cd /path/to/project-hkex
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python main.py
```

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
python main.py
```

After setup, either **activate** the venv as above or run `\.venv/bin/python main.py` (macOS/Linux) so you do not use a system/conda Python missing this project’s packages.

### Interactive run (local terminal)

When **stdin is a TTY** (normal terminal), `main.py` **prompts** for:

1. **Stock codes** — comma-separated; **Enter** keeps `WATCHLIST` from `watchlist.yaml` (via `config.py`).
2. **Days back (N)** — **Enter** keeps `DAYS_BACK`. Sets the HKEX “from” date **and** drops any row whose parsed release time is **older than N days** in Asia/Hong_Kong (so old rows on a capped results page are excluded).
3. **Target categories** — **`1`** or **Enter** for `TARGET_CATEGORIES` from `config.py`; **`2`** Circulars; **`3`** Listing Documents; **`4`** Custom (tier-2 labels one per line). Edit `_CATEGORY_MENU` in `main.py` to change presets.

Skip prompts and use **`config.py` only**:

```powershell
python main.py --use-config
```

Or set **`HKEX_USE_CONFIG=1`** in the environment.

**CI / automation:** non-interactive runs (e.g. GitHub Actions) have **no TTY**, so `main.py` always uses `config.py` and does not block on input.

Dates use the **Asia/Hong_Kong** timezone when computing the “from / to” range.

## Summarizer modes

Set **`SUMMARIZER`** or **`LLM_PROVIDER`** (alias). Resolution order:

1. If `SUMMARIZER` / `LLM_PROVIDER` is set → use that mode.
2. Else if `GROK_API_KEY` is set → **`grok`** (keeps existing deployments working).
3. Else → **`none`** (default; no LLM HTTP calls).

| Mode | Env | Notes |
|------|-----|--------|
| **`none`** | (default) or `SUMMARIZER=none` | Deterministic blurb from title + category; no PDF download for summarization. **No Grok key required.** |
| **`grok`** | `GROK_API_KEY`, optional `GROK_MODEL` (default `grok-3-mini`) | Downloads PDF excerpt, calls [xAI Chat Completions](https://docs.x.ai/docs). |
| **`openai`** | `LLM_BASE_URL`, `LLM_MODEL`, optional `LLM_API_KEY` | OpenAI-compatible Chat Completions, e.g. Ollama `http://127.0.0.1:11434/v1`. Also accepts `SUMMARIZER=openai-compatible`. |

**Zero-cost local run** (Notion only):

```bash
SUMMARIZER=none python main.py --use-config
```

**Grok:**

```bash
SUMMARIZER=grok GROK_API_KEY=xai-... python main.py --use-config
```

**Ollama (example):**

```bash
SUMMARIZER=openai LLM_BASE_URL=http://127.0.0.1:11434/v1 LLM_MODEL=llama3.2 python main.py --use-config
```

If a paid/remote mode is selected but required credentials or URL are missing, the run fails at startup with a clear error. `none` never requires `GROK_API_KEY`.

## GitHub Actions

Workflow: [`.github/workflows/hkex-to-notion.yml`](.github/workflows/hkex-to-notion.yml)

- **Default schedule**: once daily at `02:00 UTC` (adjust for HKT in the cron expression).
- **Change frequency**: edit the `cron:` line in the workflow file. GitHub Actions **requires a literal cron string** in YAML (not a secret). The file includes commented examples (e.g. twice daily, hourly).
- **Manual run**: Actions → workflow → “Run workflow”.

### Repository secrets and variables

| Name | Kind | Description |
|------|------|-------------|
| `NOTION_TOKEN` | secret | Notion internal integration token |
| `NOTION_DATABASE_ID` | secret | Target database ID |
| `GROK_API_KEY` | secret | xAI API key (only if using Grok) |
| `LLM_API_KEY` | secret | Optional API key for OpenAI-compatible backends |
| `SUMMARIZER` | variable | `none` (default if unset and no Grok key), `grok`, or `openai` |
| `GROK_MODEL` | variable | Grok model (default `grok-3-mini`) |
| `LLM_BASE_URL` | variable | e.g. `http://host:11434/v1` for Ollama |
| `LLM_MODEL` | variable | Model name for OpenAI-compatible mode |

**Migration:** Existing workflows that only set `GROK_API_KEY` continue to use Grok automatically. To run scheduled sync at **zero LLM cost**, remove `GROK_API_KEY` or set repository variable **`SUMMARIZER=none`**.

## Behaviour notes

- **HKEX** may change its HTML/JS; if the fetcher breaks, use `DEBUG_SAVE_HTML=1` locally to dump HTML on failure (see `hkex_fetcher.py`).
- **Rate limits**: small delays between Notion writes and LLM calls.
- **PDF text**: In LLM modes, if extraction fails, the summarizer falls back to title + category only. In `none` mode, PDFs are not downloaded for summarization.

## Responsible use

Use reasonable intervals, respect HKEX terms and capacity. This is intended for **personal** monitoring, not high-volume scraping.

## Improving summaries later

- Lower **temperature** and tune **max_tokens** for more consistent blurbs.
- Refine the prompt (e.g. “lead with the corporate action; include amounts/dates if in excerpt”).
- Optionally parse more PDF pages or skip TOC-only pages.
- Support bilingual output if filings are mostly Chinese.
