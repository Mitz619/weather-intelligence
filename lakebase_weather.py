"""lakebase_weather.py — Lakebase (Postgres + pgvector) schema and writes.

Mirrors the lakebase.py pattern from the day-2 reference app:
get_connection() context manager, psycopg2 + RealDictCursor.

If your existing lakebase.py already exposes get_connection(), delete the
version below and `from lakebase import get_connection` instead.

DATABASE_URL comes from a Databricks secret scope, injected via `valueFrom`
in app.yaml (same setup as the Day 1 support app):

  env:
    - name: "DATABASE_URL"
      valueFrom: "database-url"        # secret key in your scope

Free Edition reminder: this Lakebase instance uses a static Postgres role
(host/user/password in the URL) — SDK credential generation is not supported.
"""

import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

EMBEDDING_DIM = 384  # sentence-transformers/all-MiniLM-L6-v2


def _connect_kwargs():
    """Prefer individual PG* env vars (injected when a Lakebase database is
    attached as an app resource — no secret needed). Fall back to a single
    DATABASE_URL string (secret scope via valueFrom, or local/CI export).
    """
    if os.environ.get("PGHOST"):
        return {
            "host": os.environ["PGHOST"],
            "port": os.environ.get("PGPORT", "5432"),
            "dbname": os.environ.get("PGDATABASE"),
            "user": os.environ.get("PGUSER"),
            "password": os.environ.get("PGPASSWORD"),
            "sslmode": os.environ.get("PGSSLMODE", "require"),
        }
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "No database config found. Attach the Lakebase database as an app "
            "resource (injects PGHOST/PGUSER/...), or set DATABASE_URL."
        )
    # Env vars can carry a trailing newline/space that psycopg2 forwards
    # verbatim (e.g. sslmode="require ") and rejects.
    return {"dsn": url.strip()}


@contextmanager
def get_connection():
    conn = psycopg2.connect(cursor_factory=RealDictCursor, **_connect_kwargs())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS weather_documents (
    id             TEXT PRIMARY KEY,
    location       TEXT NOT NULL,
    source_type    TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast')),
    headline       TEXT,
    narrative_text TEXT NOT NULL,
    issued_at      TIMESTAMPTZ,
    payload        JSONB,
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS weather_embeddings (
    id          BIGSERIAL PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    chunk_text  TEXT NOT NULL,
    embedding   vector({EMBEDDING_DIM}) NOT NULL,
    model_name  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_weather_embeddings_hnsw
    ON weather_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_weather_documents_source_type
    ON weather_documents (source_type);
"""


def ensure_weather_schema():
    """Idempotent — safe to call on every sync."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)


UPSERT_SQL = """
INSERT INTO weather_documents
    (id, location, source_type, headline, narrative_text, issued_at, payload, synced_at)
VALUES %s
ON CONFLICT (id) DO UPDATE SET
    headline       = EXCLUDED.headline,
    narrative_text = EXCLUDED.narrative_text,
    payload        = EXCLUDED.payload,
    synced_at      = EXCLUDED.synced_at;
"""


def upsert_weather_documents(docs):
    """Upsert normalized document dicts. Returns number of rows written.

    Re-running /weather/sync never creates duplicates (stretch goal:
    dedupe/upsert on id).
    """
    if not docs:
        return 0
    rows = [
        (
            d["id"], d["location"], d["source_type"], d.get("headline"),
            d["narrative_text"], d.get("issued_at"), d.get("payload"),
            d.get("synced_at"),
        )
        for d in docs
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, UPSERT_SQL, rows, page_size=200)
    return len(rows)