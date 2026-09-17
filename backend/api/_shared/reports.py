"""Report-generation queries against Neon: plain-SQL aggregation, and
pgvector semantic search optionally combined with the same filters.
Callers are expected to open the connection with pgvector.psycopg2's
register_vector() already applied (see api/sync.py for the pattern).
"""
from api._shared.embeddings import embed_texts

AGGREGATE_SQL = """
SELECT
    state,
    project,
    date_trunc('month', report_timestamp) AS month,
    count(*) AS report_count,
    sum(beneficiaries) AS total_beneficiaries,
    sum(project_beneficiaries) AS total_project_beneficiaries,
    sum(training_participants) AS total_training_participants,
    sum(community_outreach) AS total_community_outreach
FROM reports
WHERE (%(start_date)s IS NULL OR report_timestamp >= %(start_date)s)
  AND (%(end_date)s IS NULL OR report_timestamp < %(end_date)s)
  AND (%(state)s IS NULL OR state = %(state)s)
  AND (%(project)s IS NULL OR project = %(project)s)
GROUP BY state, project, month
ORDER BY month, state, project;
"""

SEMANTIC_SEARCH_SQL = """
SELECT
    id, sheet_row_id, report_timestamp, name, state, project,
    location, area_of_intervention, description, attachment_url,
    1 - (description_embedding <=> %(query_embedding)s) AS similarity
FROM reports
WHERE description_embedding IS NOT NULL
  AND (%(state)s IS NULL OR state = %(state)s)
  AND (%(project)s IS NULL OR project = %(project)s)
  AND (%(start_date)s IS NULL OR report_timestamp >= %(start_date)s)
  AND (%(end_date)s IS NULL OR report_timestamp < %(end_date)s)
ORDER BY description_embedding <=> %(query_embedding)s
LIMIT %(limit)s;
"""


def _rows_as_dicts(cur):
    columns = [c.name for c in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_aggregate_report(conn, start_date=None, end_date=None, state=None, project=None):
    with conn.cursor() as cur:
        cur.execute(AGGREGATE_SQL, {
            "start_date": start_date,
            "end_date": end_date,
            "state": state,
            "project": project,
        })
        return _rows_as_dicts(cur)


def semantic_search_reports(conn, query_text, limit=10, state=None, project=None,
                             start_date=None, end_date=None):
    query_embedding = embed_texts([query_text])[0]
    with conn.cursor() as cur:
        cur.execute(SEMANTIC_SEARCH_SQL, {
            "query_embedding": query_embedding,
            "limit": limit,
            "state": state,
            "project": project,
            "start_date": start_date,
            "end_date": end_date,
        })
        return _rows_as_dicts(cur)
