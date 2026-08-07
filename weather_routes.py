"""weather_routes.py — Flask blueprint for the weather pipeline.

Register in your existing app.py with two lines:

    from weather_routes import weather_bp
    app.register_blueprint(weather_bp)

Endpoints:
  POST /weather/sync    {"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}
  POST /weather/search  {"query": "flash flood risk this weekend", "top_k": 5}
"""

import threading

import psycopg2.errors
from flask import Blueprint, jsonify, request

from lakebase_weather import (
    get_connection,
    ensure_weather_schema,
    upsert_weather_documents,
)
from weather_client import harvest_documents

weather_bp = Blueprint("weather", __name__)

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# Load the embedding model once at module/app level, not per-request.
# Lazy + lock so app startup stays fast and /weather/sync works even
# before the model has ever been needed.
_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(MODEL_NAME)
    return _model


def _to_pgvector(vec):
    """Python list -> pgvector text literal, cast with ::vector in SQL."""
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


@weather_bp.route("/weather/sync", methods=["POST"])
def weather_sync():
    body = request.get_json(silent=True) or {}
    locations = body.get("locations")
    limit = body.get("limit", 50)

    if not isinstance(locations, list) or not locations:
        return jsonify({"error": "Body must include a non-empty 'locations' list."}), 400
    if not isinstance(limit, int) or limit < 1:
        limit = 50
    limit = min(limit, 200)

    try:
        ensure_weather_schema()
        docs = harvest_documents(locations, limit=limit)
        written = upsert_weather_documents(docs)
    except ValueError as e:                      # unknown location
        return jsonify({"error": str(e)}), 400
    except Exception as e:                       # NWS/network/DB failure
        return jsonify({"error": f"Sync failed: {e}"}), 502

    by_type = {}
    for d in docs:
        by_type[d["source_type"]] = by_type.get(d["source_type"], 0) + 1

    return jsonify({
        "synced": written,
        "by_source_type": by_type,
        "locations": locations,
        "note": "Run the embedding ingestion script to make these searchable.",
    })


SEARCH_SQL = """
SELECT d.id, d.location, d.source_type, d.headline, d.issued_at,
       e.chunk_text,
       1 - (e.embedding <=> %s::vector) AS similarity
FROM weather_embeddings e
JOIN weather_documents d ON d.id = e.document_id
ORDER BY e.embedding <=> %s::vector
LIMIT %s;
"""


@weather_bp.route("/weather/search", methods=["POST"])
def weather_search():
    body = request.get_json(silent=True) or {}
    query = body.get("query")
    top_k = body.get("top_k", 5)

    if not isinstance(query, str) or not query.strip():
        return jsonify({"error": "Body must include a non-empty 'query' string."}), 400
    if not isinstance(top_k, int):
        top_k = 5
    top_k = max(1, min(top_k, 20))  # clamp 1–20

    no_data_response = jsonify({
        "query": query,
        "results": [],
        "message": "No embeddings yet. POST /weather/sync, then run the ingestion script.",
    })

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Empty-table edge case: report clearly instead of erroring.
                cur.execute("SELECT EXISTS (SELECT 1 FROM weather_embeddings) AS has_rows;")
                if not cur.fetchone()["has_rows"]:
                    return no_data_response

                vec = _to_pgvector(get_model().encode(query.strip(), normalize_embeddings=True))
                cur.execute(SEARCH_SQL, (vec, vec, top_k))
                rows = cur.fetchall()
    except psycopg2.errors.UndefinedTable:
        # Tables not created yet (sync never run) — same as "no data".
        return no_data_response
    except Exception as e:
        return jsonify({"error": f"Search failed: {e}"}), 500

    results = [
        {
            "document_id": r["id"],
            "location": r["location"],
            "source_type": r["source_type"],
            "headline": r["headline"],
            "issued_at": r["issued_at"].isoformat() if r["issued_at"] else None,
            "chunk_text": r["chunk_text"],
            "similarity": round(float(r["similarity"]), 4),
        }
        for r in rows
    ]
    return jsonify({"query": query, "top_k": top_k, "results": results})

