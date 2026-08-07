"""ingest_weather_embeddings.py — chunk + embed weather documents into pgvector.

Plain Python + psycopg2 (NO Spark, NO spark.write.jdbc — JDBC writes are not
supported against this Lakebase instance). Runs anywhere DATABASE_URL can
reach Lakebase: locally, inside the Databricks App container, or in a
GitHub Actions job. Do NOT run in a serverless notebook kernel — psycopg2
SIGABRTs there and Hugging Face model downloads are blocked by egress rules.

Usage:
    export DATABASE_URL="postgresql://user:pass@host:5432/db?sslmode=require"
    python scripts/ingest_weather_embeddings.py
    python scripts/ingest_weather_embeddings.py --from-jsonl weather_docs.jsonl
"""

import argparse
import json
import sys
from pathlib import Path

from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root
from lakebase_weather import (  # noqa: E402
    get_connection,
    ensure_weather_schema,
    upsert_weather_documents,
)

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"  # 384-dim, matches news pipeline
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
ENCODE_BATCH = 64


def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Sliding-window character chunking (same convention as the news pipeline).

    Most NWS forecast text is < 800 chars, so it stays a single chunk;
    combined alert description + instructions is where chunking kicks in.
    """
    text = text.strip()
    if len(text) <= chunk_size:
        return [text] if text else []
    chunks, start = [], 0
    step = chunk_size - overlap
    while start < len(text):
        chunk = text[start:start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        start += step
    return chunks


def fetch_unembedded(cur):
    cur.execute("""
        SELECT d.id, d.narrative_text
        FROM weather_documents d
        LEFT JOIN weather_embeddings e ON e.document_id = d.id
        WHERE e.id IS NULL
          AND d.narrative_text IS NOT NULL
          AND length(trim(d.narrative_text)) > 0
        ORDER BY d.synced_at;
    """)
    return cur.fetchall()


INSERT_SQL = """
INSERT INTO weather_embeddings
    (document_id, chunk_index, chunk_text, embedding, model_name)
VALUES %s
ON CONFLICT (document_id, chunk_index) DO NOTHING;
"""
INSERT_TEMPLATE = "(%s, %s, %s, %s::vector, %s)"


def to_pgvector(vec):
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def load_jsonl(path):
    docs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                docs.append(json.loads(line))
    return docs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-jsonl", default=None,
                        help="Load harvested documents from a JSONL file first "
                             "(GitHub Actions fallback path).")
    args = parser.parse_args()

    ensure_weather_schema()

    if args.from_jsonl:
        docs = load_jsonl(args.from_jsonl)
        written = upsert_weather_documents(docs)
        print(f"Upserted {written} documents from {args.from_jsonl}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            rows = fetch_unembedded(cur)
    print(f"{len(rows)} documents need embedding")
    if not rows:
        return

    # Build (document_id, chunk_index, chunk_text) triples.
    triples = []
    for row in rows:
        for idx, chunk in enumerate(chunk_text(row["narrative_text"])):
            triples.append((row["id"], idx, chunk))
    print(f"{len(triples)} chunks to embed (chunk_size={CHUNK_SIZE}, overlap={CHUNK_OVERLAP})")

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)

    inserted = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for i in range(0, len(triples), ENCODE_BATCH):
                batch = triples[i:i + ENCODE_BATCH]
                embeddings = model.encode(
                    [t[2] for t in batch],
                    batch_size=ENCODE_BATCH,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
                values = [
                    (doc_id, idx, chunk, to_pgvector(emb), MODEL_NAME)
                    for (doc_id, idx, chunk), emb in zip(batch, embeddings)
                ]
                execute_values(cur, INSERT_SQL, values,
                               template=INSERT_TEMPLATE, page_size=len(values))
                inserted += len(values)
                print(f"  wrote {inserted}/{len(triples)} chunks")

    print("Done.")


if __name__ == "__main__":
    main()
