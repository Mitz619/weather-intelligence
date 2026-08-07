# Weather Intelligence

**Unstructured Data → Lakebase Vector Search → REST API**

Semantic search over live National Weather Service text. The pipeline harvests free-text weather alerts and narrative forecasts from api.weather.gov, embeds them with sentence-transformers, stores the vectors in Lakebase (Postgres + pgvector), and serves cosine-similarity retrieval from a Flask REST API running as a Databricks App.

```
POST /weather/search {"query": "flash flood risk this weekend"}
        → top-k weather documents ranked by vector similarity
```

## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │              api.weather.gov                │
                    │   /points  /alerts/active  /forecast        │
                    └──────────────┬──────────────────────────────┘
                                   │ harvest + normalize
              ┌────────────────────┴───────────────────┐
              │                                        │
   POST /weather/sync                     GitHub Actions (fallback +
   (Databricks App)                       scheduled cron, every 6h)
              │                                        │
              ▼                                        ▼
        ┌─────────────────────────────────────────────────────┐
        │           Lakebase: weather_documents               │
        │   id | location | source_type | headline |          │
        │   narrative_text | issued_at | payload | synced_at  │
        └──────────────────────┬──────────────────────────────┘
                               │ scripts/ingest_weather_embeddings.py
                               │ chunk 800/100 → all-MiniLM-L6-v2 (384-dim)
                               ▼
        ┌─────────────────────────────────────────────────────┐
        │           Lakebase: weather_embeddings              │
        │   document_id | chunk_index | chunk_text |          │
        │   embedding vector(384) | model_name                │
        │   HNSW index (vector_cosine_ops)                    │
        └──────────────────────┬──────────────────────────────┘
                               │ pgvector <=> cosine distance
                               ▼
                     POST /weather/search
```

## Repository layout

| File | Purpose |
|---|---|
| `weather_client.py` | NWS API client: resolves locations to grid points, fetches alerts + forecasts, normalizes into document records. Also runs standalone to dump JSONL. |
| `lakebase_weather.py` | `get_connection()` (psycopg2 + RealDictCursor), DDL for both tables + HNSW index, upsert logic. |
| `weather_routes.py` | Flask blueprint: `POST /weather/sync`, `POST /weather/search`. |
| `app.py` | Flask entrypoint (registers the blueprint; adds `/health`). |
| `scripts/ingest_weather_embeddings.py` | Chunk + embed + batch-write embeddings via `execute_values`. Plain Python — no Spark. |
| `.github/workflows/weather_sync.yml` | Scheduled harvest + embed (stretch goal), and the fallback path if compute egress blocks NWS. |
| `app.yaml` | Databricks Apps config (secret-based `DATABASE_URL`). |

## Why the National Weather Service API

- **Free, keyless, generous rate limits** — effort goes into harvesting/vectorization/retrieval, not auth plumbing. Only requirement is a descriptive `User-Agent` header (set yours at the top of `weather_client.py`).
- **Genuinely unstructured prose** — alert `description` + `instruction` fields and per-period `detailedForecast` narratives are exactly the kind of text where semantic search beats keyword search ("risk of flooding near rivers" matches a Flash Flood Warning that never uses the word "risk").
- **Two text flavors in one API** — alerts and forecasts are both harvested and tagged with `source_type`, covering the multi-flavor stretch goal without mixing providers.

One quirk: NWS has no geocoder. Friendly city names resolve through a `KNOWN_LOCATIONS` map in `weather_client.py`; raw `"lat,lon"` strings also work. **US locations only** — NWS covers US territory.

## Schema decisions

**`weather_documents`** — one row per harvested item.
- `id TEXT PRIMARY KEY` is the natural dedup key: the NWS alert URN for alerts, `sha1(location | startTime | period_name)` for forecast periods. `ON CONFLICT (id) DO UPDATE` makes re-syncing idempotent — no duplicate rows, ever.
- `narrative_text` for alerts concatenates description + `INSTRUCTIONS:` so safety guidance is searchable alongside the event description.
- `payload JSONB` preserves the raw API response for provenance.
- `source_type` is CHECK-constrained to `'alert' | 'forecast'` and indexed for future filtered retrieval.

**`weather_embeddings`** — one row per chunk.
- `document_id` FK with `ON DELETE CASCADE`; `UNIQUE (document_id, chunk_index)` + `ON CONFLICT DO NOTHING` makes the embed job safely re-runnable.
- `embedding vector(384)` with an HNSW index (`vector_cosine_ops`) backing the `<=>` queries.
- `model_name` recorded per row so a future model migration can distinguish generations.

**Chunking:** sliding window, `CHUNK_SIZE=800`, `CHUNK_OVERLAP=100` — the same convention as the ticker-news pipeline. Most NWS forecast text fits a single chunk; chunking mainly matters for long combined alert description + instruction bodies.

**Embedding model:** `sentence-transformers/all-MiniLM-L6-v2` (384-dim), matching the existing news pipeline so both vector stores share dimensionality and distance-operator conventions. Embeddings are L2-normalized at both ingest and query time.

## Setup

### 1. Lakebase credentials

Create a static-password Postgres role in the Lakebase UI (Free Edition does not support SDK credential generation — `generate_database_credential()` returns empty). Build the URL:

```
postgresql://<role>:<password>@<host>:5432/<database>?sslmode=require
```

Store it in a Databricks secret scope and reference the key in `app.yaml` via `valueFrom`. Never commit the URL.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

Uses `psycopg2-binary` — fine here because every DB-facing component runs in the Databricks App container, locally, or in GitHub Actions. **Do not run any of this in a serverless notebook kernel**: psycopg2's C extension aborts (SIGABRT / exit 134) there, and serverless egress blocks both api.weather.gov and the Hugging Face model download.

### 3. Deploy the app

Deploy as a Databricks App with `app.yaml`, or run locally:

```bash
export DATABASE_URL="postgresql://..."
python app.py
```

Register in an existing app instead with two lines:

```python
from weather_routes import weather_bp
app.register_blueprint(weather_bp)
```

### 4. (Optional) GitHub Actions

Add `DATABASE_URL` as a repository secret. The workflow in `.github/workflows/weather_sync.yml` then harvests and embeds every 6 hours (or on manual dispatch). This is also the fallback if outbound calls to api.weather.gov are blocked from your compute tier — GitHub Actions makes the external calls and writes straight to Lakebase over its public endpoint, the same pattern as an external-API bridge.

## Running the pipeline end-to-end

**Sync** — fetch, normalize, upsert (tables are created idempotently on first call):

```bash
curl -X POST $APP_URL/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}'
```

```json
{"synced": 42, "by_source_type": {"alert": 14, "forecast": 28}, "locations": ["Chicago, IL", "Austin, TX"]}
```

**Embed** — chunk and vectorize anything not yet embedded:

```bash
export DATABASE_URL="postgresql://..."
python scripts/ingest_weather_embeddings.py
# or, JSONL fallback path:
python weather_client.py "Chicago, IL" --out weather_docs.jsonl
python scripts/ingest_weather_embeddings.py --from-jsonl weather_docs.jsonl
```

**Search** — cosine similarity via pgvector `<=>`:

```bash
curl -X POST $APP_URL/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query": "risk of flooding near rivers", "top_k": 5}'
```

```json
{
  "query": "risk of flooding near rivers",
  "top_k": 5,
  "results": [
    {
      "document_id": "urn:oid:2.49.0.1.840.0...",
      "location": "Austin, TX",
      "source_type": "alert",
      "headline": "Flash Flood Warning",
      "chunk_text": "...excessive runoff will cause flooding of rivers, creeks...",
      "similarity": 0.7134
    }
  ]
}
```

## Implementation notes

- **Query embedding at request time** — the model is loaded lazily once at module level behind a lock, never per-request. First `/weather/search` after a cold start pays the model-load cost; subsequent requests are fast.
- **Vector literals** — embeddings are passed as `"[0.1,0.2,...]"` strings cast with `%s::vector` in SQL. No Spark JDBC anywhere: `spark.write.jdbc` is not supported against this Lakebase instance, so all writes use psycopg2 `execute_values` with batched `ON CONFLICT` inserts.
- **Retries** — NWS transient errors (429/5xx) get exponential backoff.
- **Edge cases** — empty or missing `weather_embeddings` → empty result set with a helpful message, not a 500. Missing/blank `query` → 400. `top_k` clamped to 1–20. Unknown location → 400 with guidance. Statewide alerts appearing under two cities in the same state deduplicate by id.

## Stretch goals covered

- Dedup/upsert on `id` — re-running `/weather/sync` never duplicates rows.
- Scheduled re-sync — GitHub Actions cron every 6 hours.
- Two source flavors (alerts + forecasts) distinguishable by `source_type`.

## Known limitations / future work

- `KNOWN_LOCATIONS` is a manual map; a real geocoder (e.g., the Census geocoding API) would remove it.
- Expired alerts persist; a cleanup job keyed on the `expires` field in `payload` would keep the index fresh.
- No `source_type` filter parameter on search yet, and no LLM summary (RAG) layer — both natural next steps; the RAG variant could reuse a Databricks AI Gateway chat model over the top-k chunks.
- Character-based chunking is crude; sentence-aware splitting would produce cleaner chunks for long alerts.
- No HNSW vs. no-index latency benchmark yet (listed stretch goal).
