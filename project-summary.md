# Google Sheets → Neon (Postgres) sync for fast AI report generation

## Context
Source data is a Google Sheet with two tabs:
- **Daily Reports** (active, ~7,974 rows): Timestamp, Name, Phone, State,
  Location, Project, Area of Intervention, Description (free text,
  English/Hindi mixed), Beneficiaries, Project Beneficiaries, Training
  Participants, Community Outreach, Attachment (Drive URL). Rows are
  written by a bot/API integration (WhatsApp/Gupshup-style), not typed
  manually and not via a Google Form.
- **Daily Reports_old** (~659 rows): older, inconsistent schema (some
  columns shifted/misused). Historical only — not synced, not merged into
  the new schema.

Reports are field-worker activity logs used to generate AI-powered
summaries/reports. The old pipeline called the Sheets API live at
report-generation time — slow (rate limits, no indexing, full re-fetch).

## Architecture decisions
- **Neon (Postgres) + pgvector**, one table for both structured filters
  (state/project/date) and semantic search (embeddings on `description`).
  Avoids running two databases that need to stay in sync.
- **Not BigQuery**: a warehouse (1-3s+/query, batch-oriented) is wrong for
  low-latency point lookups feeding a live AI response. Worth adding later
  *alongside* Postgres for large-scale historical analytics only — never as
  a replacement for the live-serving layer.
- **Not a graph DB (Neo4j)**: no current need for relationship-traversal
  queries (worker↔project↔location networks). Aggregation + semantic
  search don't require it.
- **Images**: not embedded directly (no CLIP-style image embedding).
  Recommended future step is to caption images via a vision model once and
  embed the caption as text alongside `description`, since the use case is
  "find reports that mention/show X," not visual similarity search. Not
  implemented yet.

## Repo layout / CI-CD
Two fully independent top-level folders — each deploys on its own, and each
has a GitHub Actions workflow that only fires on changes to its own folder
(`paths:` filter), so a backend change never triggers an Apps Script push
and vice versa:

```
apps-script/            deploys via clasp — Google Apps Script project
backend/                deploys via Vercel — Python serverless functions + DB schema
.github/workflows/
  apps-script-deploy.yml   triggers on apps-script/**
  backend-deploy.yml       triggers on backend/**
```

- **`apps-script/`** — `clasp push`-able on its own.
  - `apps_script_sync.gs` — the sync logic (below).
  - `appsscript.json` — the manifest clasp needs to push.
  - `.clasp.json.example` — copy to `.clasp.json` and fill in your real
    script ID for local dev (`clasp clone <scriptId>` also creates one).
    The real `.clasp.json` is gitignored — it's tied to one script/account.
  - CI (`apps-script-deploy.yml`) needs two repo secrets: `CLASPRC_JSON`
    (contents of `~/.clasprc.json` after a local `clasp login`) and
    `CLASP_JSON` (contents of your real `.clasp.json`).

- **`backend/`** — a self-contained Vercel project root.
  - `api/sync.py`, `api/_shared/` — the sync endpoint (below).
  - `scripts/backfill_embeddings.py` — local one-off job (below).
  - `db/schema.sql` — run once in Neon's SQL editor.
  - `requirements.txt`, `vercel.json`, `.env.example`.
  - Deploy either by setting this Vercel project's **Root Directory** to
    `backend` in the Vercel dashboard (Vercel's Git integration then
    handles CI/CD itself, ignoring pushes that don't touch this folder), or
    via `backend-deploy.yml`, which needs `VERCEL_TOKEN`, `VERCEL_ORG_ID`,
    `VERCEL_PROJECT_ID` repo secrets (from `vercel link --cwd backend`
    locally once). Pick one — running both will double-deploy.

## Sync pipeline

- **Apps Script** (`apps-script/apps_script_sync.gs`), lives in the Sheet.
  - Installable **onChange** trigger (not `onEdit`, which doesn't fire for
    API-based writes; not `onFormSubmit`, since there's no Form).
  - `LockService.waitLock()` (not `tryLock`) around each run, so a burst of
    rapid bot writes queues up instead of being dropped — each queued run
    re-reads the sheet's current last row once it gets the lock, so nothing
    appended while it waited gets missed.
  - Tracks a `lastSyncedRow` watermark in Script Properties; only advances
    it after a batch is sent successfully, so a failed HTTP call retries
    that chunk next run instead of skipping it.
  - Sends new rows in batches of 200 to the Vercel endpoint, authenticated
    with a `SYNC_SECRET` bearer token (Script Property, not hard-coded).
  - `backfillAll()` resets the watermark to the header row and reuses the
    same incremental path — used once for the initial ~7,974-row load.

- **Backend** (`backend/api/sync.py`) — Vercel Python serverless function.
  - Verifies `SYNC_SECRET`, upserts rows into Neon via
    `psycopg2.extras.execute_values` with
    `ON CONFLICT (sheet_row_id) DO UPDATE`, then batch-embeds (single
    OpenAI call per request) any row that came back with a NULL embedding.
  - `api/_shared/embeddings.py`, `api/_shared/reports.py`: helper modules.
    Live under a leading-underscore directory so Vercel's Python builder
    doesn't treat them as their own routes.
  - Env vars: `DATABASE_URL`, `SYNC_SECRET`, `OPENAI_API_KEY`.

- **`backend/db/schema.sql`** — run once in Neon's SQL editor. `reports`
  table with `phone` as `TEXT` (not numeric, to keep leading digits/
  precision), a nullable `description_embedding vector(1536)`, btree
  indexes on `state`, `project`, `report_timestamp`. The HNSW index on the
  embedding column is commented out — create it only once embeddings are
  actually populated for most rows, otherwise it just gets rebuilt from
  mostly-NULL data.

## `sheet_row_id` — the upsert key
Originally the sheet row number — breaks if a row is deleted or the sheet
gets reordered, since everything after it shifts. Fixed: `sheet_row_id` is
now `sha256("<report_timestamp ISO>|<phone, digits only>")`, computed in
Apps Script and sent as part of the payload. Stable regardless of the row's
position in the sheet. Postgres trusts the value as sent — the sync
endpoint is authenticated via `SYNC_SECRET`, not a public surface, so
there's no need to recompute/verify the hash server-side.

## Embeddings
- Generated once per row, only when `description_embedding IS NULL` — never
  re-embeds existing rows.
- **OpenAI** (`text-embedding-3-small`, 1536-dim, matches the schema
  column). Tried Voyage AI first (Anthropic's recommended pairing provider,
  since Anthropic has no embeddings API of its own) but its free tier caps
  requests at 3/minute with no payment method — too restrictive for the
  initial ~7,974-row backfill — so switched to OpenAI, whose default rate
  limits are much higher. `embed_texts()` still accepts an `input_type`
  argument for interface compatibility (ignored — OpenAI has no query vs.
  document distinction), so `semantic_search_reports` didn't need to change
  if the provider changes again later.
- Batched: one OpenAI call per sync request (or per chunk in a backfill),
  not one call per row — keeps well inside Vercel's function time limit
  even for a full 200-row `backfillAll()` chunk.
- `backend/scripts/backfill_embeddings.py` is a separate, local-only
  follow-up job for anything that ends up NULL after the fact (e.g.
  `OPENAI_API_KEY` wasn't set yet during the initial load). Loops in chunks
  of 100 until nothing is left to embed. Run with `backend/` as the working
  directory: `python scripts/backfill_embeddings.py`.

## Report generation queries (`backend/api/_shared/reports.py`)
- `get_aggregate_report(...)`: plain SQL, counts/sums by state, project,
  month, with optional filters — no vector involved.
- `semantic_search_reports(...)`: embeds the query text, then does a
  pgvector cosine-distance search (`<=>` operator) combined with the same
  structured filters (state/project/date range) in one query.
- Report-generation code (wherever it currently calls the Sheets API) still
  needs to be pointed at these functions instead — not done as part of this
  change, since the report generator itself lives outside this repo.

## Open items / not done here
- Report-generation code itself hasn't been repointed at Neon yet — these
  query functions are ready for it to call.
- No image captioning step yet (see Architecture decisions above).
- `backend/vercel.json` sets `maxDuration: 60` for `api/sync.py`; confirm
  this is within your Vercel plan's limit (Hobby defaults to 10s unless
  changed).
- LLM inference (report generation + embeddings) is the dominant cost
  driver at scale, not database storage — Neon cost scales gently even at
  large volumes.
