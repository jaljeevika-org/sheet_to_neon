"""Vercel Python function: receives new/changed rows from the Apps Script
sync, upserts them into Neon, and embeds descriptions for any row that
doesn't have one yet (never re-embeds existing rows).
"""
import json
import os
from http.server import BaseHTTPRequestHandler

import psycopg2
from pgvector.psycopg2 import register_vector
from psycopg2.extras import execute_values

from api._shared.embeddings import embed_texts

DATABASE_URL = os.environ["DATABASE_URL"]
SYNC_SECRET = os.environ["SYNC_SECRET"]

UPSERT_SQL = """
INSERT INTO reports (
    sheet_row_id, report_timestamp, name, phone, state, location, project,
    area_of_intervention, description, beneficiaries, project_beneficiaries,
    training_participants, community_outreach, attachment_url, updated_at
) VALUES %s
ON CONFLICT (sheet_row_id) DO UPDATE SET
    report_timestamp = EXCLUDED.report_timestamp,
    name = EXCLUDED.name,
    phone = EXCLUDED.phone,
    state = EXCLUDED.state,
    location = EXCLUDED.location,
    project = EXCLUDED.project,
    area_of_intervention = EXCLUDED.area_of_intervention,
    description = EXCLUDED.description,
    beneficiaries = EXCLUDED.beneficiaries,
    project_beneficiaries = EXCLUDED.project_beneficiaries,
    training_participants = EXCLUDED.training_participants,
    community_outreach = EXCLUDED.community_outreach,
    attachment_url = EXCLUDED.attachment_url,
    updated_at = now()
RETURNING sheet_row_id, description, description_embedding IS NULL AS needs_embedding;
"""

EMBED_UPDATE_SQL = "UPDATE reports SET description_embedding = %s WHERE sheet_row_id = %s;"


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.headers.get("Authorization") != f"Bearer {SYNC_SECRET}":
            self._respond(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            rows = body["rows"]
        except (json.JSONDecodeError, KeyError):
            self._respond(400, {"error": 'expected {"rows": [...]}'})
            return

        if not rows:
            self._respond(200, {"upserted": 0, "embedded": 0})
            return

        try:
            upserted, embedded = sync_rows(rows)
        except Exception as exc:
            self._respond(500, {"error": str(exc)})
            return

        self._respond(200, {"upserted": upserted, "embedded": embedded})

    def _respond(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


def sync_rows(rows):
    values = [
        (
            r["sheet_row_id"], r["report_timestamp"], r.get("name"), r.get("phone"),
            r.get("state"), r.get("location"), r.get("project"),
            r.get("area_of_intervention"), r.get("description"),
            r.get("beneficiaries"), r.get("project_beneficiaries"),
            r.get("training_participants"), r.get("community_outreach"),
            r.get("attachment_url"),
        )
        for r in rows
    ]

    conn = psycopg2.connect(DATABASE_URL)
    try:
        register_vector(conn)

        with conn:
            with conn.cursor() as cur:
                results = execute_values(cur, UPSERT_SQL, values, fetch=True)

        to_embed = [
            (sheet_row_id, description)
            for sheet_row_id, description, needs_embedding in results
            if needs_embedding and description and description.strip()
        ]

        if to_embed:
            # One batched embedding call per sync request, not one per row —
            # keeps this well inside Vercel's function time limit even when
            # backfillAll() sends a full BATCH_SIZE chunk at once.
            vectors = embed_texts([description for _, description in to_embed])
            with conn:
                with conn.cursor() as cur:
                    for (sheet_row_id, _), vector in zip(to_embed, vectors):
                        cur.execute(EMBED_UPDATE_SQL, (vector, sheet_row_id))

        return len(values), len(to_embed)
    finally:
        conn.close()
