# Setup runbook — getting the Sheet → Neon flow live

Follow in order. Each step says exactly what's needed and from where.
Nothing here should be typed into a Claude Code chat — set values directly
in the dashboards named below.

## 1. Neon (Postgres)
- Open your Neon project → SQL Editor.
- Paste and run [backend/db/schema.sql](backend/db/schema.sql). Creates the
  `reports` table, indexes, and enables the `vector` extension.
- Copy the connection string from Neon's "Connection Details" panel — this
  is `DATABASE_URL`, needed in step 3.

## 2. Voyage AI (embeddings)
- Get an API key from dash.voyageai.com → `VOYAGE_API_KEY`, needed in
  step 3. (Not OpenAI — Anthropic/Claude has no embeddings API of its own
  and recommends Voyage as the pairing provider. First 200M tokens/month
  are free, which comfortably covers this project's data volume.)
- Optional: if you'd rather skip semantic search for now, this step can be
  skipped entirely — aggregation reports need no embeddings provider at all.

## 3. Deploy the backend to Vercel
- New Project → import the GitHub repo (`jaljeevika-org/sheet_to_neon`).
- **Root Directory: `backend`** (this is what makes the monorepo split work).
- Environment variables (Vercel dashboard → Settings → Environment Variables):
  - `DATABASE_URL` — from step 1
  - `VOYAGE_API_KEY` — from step 2
  - `SYNC_SECRET` — any long random string you generate now (e.g.
    `openssl rand -hex 32`). Reuse the exact same value in step 4.
- Deploy. Copy the resulting URL, e.g. `https://sheet-to-neon.vercel.app`.

## 4. Apps Script (in the script editor — Extensions → Apps Script from the Sheet)
- Project Settings → Script Properties → add `SYNC_SECRET` = the same
  value you used in step 3.
- In `apps_script_sync.gs`, set:
  ```
  var SYNC_ENDPOINT = 'https://<your-vercel-url>/api/sync';
  ```
  (paste your real URL from step 3, then `clasp push --force`, or hand the
  URL to Claude to do this + push)
- Run `createOnChangeTrigger()` once from the editor (authorizes + installs
  the trigger).
- Run `backfillAll()` once — loads the existing ~7,974 rows. Do this
  *after* steps 1–4 are all live, not before.

## 5. Verify it's actually working
- In Neon's SQL Editor: `SELECT count(*) FROM reports;` — should match (or
  be climbing toward) the Sheet's row count.
- Spot-check one row: `SELECT * FROM reports ORDER BY synced_at DESC LIMIT 1;`
  and compare it to the corresponding sheet row.
- Add one new row to the Sheet (or wait for the bot to write one) and
  confirm it shows up in Neon within a few seconds.

## Before step 4: confirm the sheet's real header row
The code maps columns by header text, not position:
`Timestamp, Name, Phone, State, Location, Project, Area of Intervention,
Description, Beneficiaries, Project Beneficiaries, Training Participants,
Community Outreach, Attachment`
If your actual sheet's headers differ, tell Claude before running
`backfillAll()` — a mismatch means silently empty/wrong columns in Neon,
not an error.
