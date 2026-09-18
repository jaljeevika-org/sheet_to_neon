-- Neon (Postgres) schema for the "Daily Reports" sync.
-- Run once in Neon's SQL editor.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS reports (
    id                      BIGSERIAL PRIMARY KEY,

    -- Stable identity for upserts: sha256(hex) of
    -- "<report_timestamp ISO>|<phone, digits only>", computed in Apps Script.
    -- NOT the sheet row number, which shifts if rows are deleted/reordered.
    sheet_row_id            TEXT NOT NULL UNIQUE,

    report_timestamp        TIMESTAMPTZ NOT NULL,
    name                    TEXT,
    phone                   TEXT,
    state                   TEXT,
    location                TEXT,
    project                 TEXT,
    area_of_intervention    TEXT,
    description             TEXT,
    -- BIGINT, not INTEGER: real field data has produced out-of-range values
    -- in these columns (e.g. a misplaced phone number), which crashed
    -- inserts under INTEGER's ~2.1 billion cap. Apps Script also rejects
    -- obviously-bogus values before sending, but the wider type is a
    -- backstop against whatever else messy sheet data throws at it.
    beneficiaries           BIGINT,
    project_beneficiaries   BIGINT,
    training_participants   BIGINT,
    community_outreach      BIGINT,
    attachment_url          TEXT,

    -- Populated lazily (NULL until embedded); gemini-embedding-001 truncated to 1536-dim.
    description_embedding   vector(1536),

    synced_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reports_state ON reports (state);
CREATE INDEX IF NOT EXISTS idx_reports_project ON reports (project);
CREATE INDEX IF NOT EXISTS idx_reports_timestamp ON reports (report_timestamp);

-- Create only after a meaningful fraction of rows have embeddings —
-- an HNSW index built over mostly-NULL vectors wastes time rebuilding later.
-- CREATE INDEX idx_reports_embedding_hnsw ON reports
--   USING hnsw (description_embedding vector_cosine_ops);
