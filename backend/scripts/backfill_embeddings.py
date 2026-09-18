"""One-off batch job: embed existing rows where description_embedding is
still NULL (e.g. rows loaded before OPENAI_API_KEY was set, or a sync call
that failed partway through). Never re-embeds rows that already have one.

Run locally:
    DATABASE_URL=... OPENAI_API_KEY=... python scripts/backfill_embeddings.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
from pgvector.psycopg2 import register_vector

from api._shared.embeddings import embed_texts

DATABASE_URL = os.environ["DATABASE_URL"]
CHUNK_SIZE = 100

SELECT_SQL = """
SELECT sheet_row_id, description FROM reports
WHERE description_embedding IS NULL
  AND description IS NOT NULL AND description <> ''
LIMIT %s;
"""

UPDATE_SQL = "UPDATE reports SET description_embedding = %s WHERE sheet_row_id = %s;"


def run():
    conn = psycopg2.connect(DATABASE_URL)
    register_vector(conn)
    total = 0
    try:
        while True:
            with conn.cursor() as cur:
                cur.execute(SELECT_SQL, (CHUNK_SIZE,))
                batch = cur.fetchall()

            if not batch:
                break

            vectors = embed_texts([description for _, description in batch])
            with conn:
                with conn.cursor() as cur:
                    for (sheet_row_id, _), vector in zip(batch, vectors):
                        cur.execute(UPDATE_SQL, (vector, sheet_row_id))

            total += len(batch)
            print(f"Embedded {total} rows so far...")
    finally:
        conn.close()
    print(f"Done. Embedded {total} rows total.")


if __name__ == "__main__":
    run()
