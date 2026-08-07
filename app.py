"""app.py — Flask entrypoint for the Weather Intelligence app.

If you're merging into the databricks-lakebase-app-day-2 reference app,
you only need the two blueprint lines below in your existing app.py.
This standalone version exists so the repo runs on its own.
"""

import os
import sys
import traceback


def _log(msg):
    """Print immediately (unbuffered) so it reaches /logz before any crash."""
    print(f"[startup] {msg}", flush=True)


# --- Startup diagnostics: surface the real cause in /logz -------------------
# Importing weather_routes pulls in sentence-transformers etc. Wrap the risky
# work so a failure prints a clear message instead of a silent exit.
try:
    _log("Python " + sys.version.split()[0])
    db_url = os.environ.get("DATABASE_URL")
    _log(f"DATABASE_URL present: {bool(db_url)}")
    if db_url:
        # Show only the shape, never the password.
        tail = db_url.strip().split("@")[-1] if "@" in db_url else "(no @ in url)"
        _log(f"DATABASE_URL host/db: ...@{tail}")
    else:
        _log("PGHOST present: " + str(bool(os.environ.get("PGHOST"))))

    from flask import Flask, jsonify, render_template_string
    from weather_routes import weather_bp
    _log("Imports OK")

    # Probe the database connection once at startup (optional but diagnostic).
    try:
        from lakebase_weather import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok;")
                cur.fetchone()
        _log("Database connection OK")
    except Exception as db_err:
        _log(f"Database connection FAILED: {type(db_err).__name__}: {db_err}")
        # Do NOT exit — let the app start so /health and the UI still load and
        # you can read this line. Search will report the same error per-request.
except Exception:
    _log("FATAL during startup:")
    traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()
    raise
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.register_blueprint(weather_bp)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weather Intelligence</title>
<style>
  :root { --fg:#1a1a2e; --muted:#6b7280; --line:#e5e7eb; --accent:#2563eb; --bg:#f7f8fa; }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         margin:0; background:var(--bg); color:var(--fg); }
  .wrap { max-width:760px; margin:0 auto; padding:40px 20px 80px; }
  h1 { font-size:1.6rem; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 28px; font-size:.95rem; }
  .row { display:flex; gap:8px; margin-bottom:12px; }
  input[type=text] { flex:1; padding:12px 14px; border:1px solid var(--line);
                     border-radius:10px; font-size:1rem; background:#fff; }
  input[type=number] { width:80px; padding:12px 10px; border:1px solid var(--line);
                       border-radius:10px; font-size:1rem; background:#fff; }
  button { padding:12px 18px; border:0; border-radius:10px; background:var(--accent);
           color:#fff; font-size:.95rem; font-weight:600; cursor:pointer; }
  button.secondary { background:#fff; color:var(--fg); border:1px solid var(--line); }
  button:disabled { opacity:.55; cursor:default; }
  .tools { display:flex; gap:8px; align-items:center; margin-bottom:28px; }
  .tools input[type=text] { flex:1; }
  .status { color:var(--muted); font-size:.85rem; min-height:1.2em; }
  .card { background:#fff; border:1px solid var(--line); border-radius:12px;
          padding:16px 18px; margin-bottom:12px; }
  .card .top { display:flex; justify-content:space-between; align-items:baseline;
               gap:12px; margin-bottom:6px; }
  .headline { font-weight:600; }
  .meta { color:var(--muted); font-size:.82rem; }
  .badge { display:inline-block; font-size:.72rem; padding:2px 8px; border-radius:999px;
           background:#eef2ff; color:var(--accent); margin-left:8px; vertical-align:middle; }
  .sim { font-variant-numeric:tabular-nums; color:var(--accent); font-weight:600;
         font-size:.85rem; white-space:nowrap; }
  .chunk { color:#374151; font-size:.92rem; line-height:1.5; white-space:pre-wrap; }
  .empty { color:var(--muted); padding:20px 0; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Weather Intelligence</h1>
  <p class="sub">Semantic search over live NWS alerts &amp; forecasts (Lakebase + pgvector).</p>

  <div class="row">
    <input id="q" type="text" placeholder="e.g. flash flood risk this weekend"
           value="flash flood risk this weekend"
           onkeydown="if(event.key==='Enter')search()">
    <input id="k" type="number" min="1" max="20" value="5" title="top_k">
    <button id="searchBtn" onclick="search()">Search</button>
  </div>

  <div class="tools">
    <input id="locs" type="text" placeholder="Sync locations, e.g. Chicago, IL; Austin, TX">
    <button class="secondary" id="syncBtn" onclick="sync()">Sync</button>
  </div>

  <div class="status" id="status"></div>
  <div id="results"></div>
</div>

<script>
const $ = id => document.getElementById(id);

async function search() {
  const query = $("q").value.trim();
  const top_k = Math.max(1, Math.min(20, parseInt($("k").value) || 5));
  if (!query) { $("status").textContent = "Enter a query."; return; }
  $("searchBtn").disabled = true;
  $("status").textContent = "Searching\u2026";
  $("results").innerHTML = "";
  try {
    const r = await fetch("/weather/search", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ query, top_k })
    });
    const data = await r.json();
    if (data.error) { $("status").textContent = "Error: " + data.error; return; }
    if (data.message && (!data.results || !data.results.length)) {
      $("status").textContent = ""; $("results").innerHTML =
        '<div class="empty">' + data.message + "</div>"; return;
    }
    if (!data.results.length) {
      $("status").textContent = ""; $("results").innerHTML =
        '<div class="empty">No matches.</div>'; return;
    }
    $("status").textContent = data.results.length + " result(s) for \u201C" + data.query + "\u201D";
    $("results").innerHTML = data.results.map(render).join("");
  } catch (e) {
    $("status").textContent = "Request failed: " + e.message;
  } finally { $("searchBtn").disabled = false; }
}

function render(x) {
  const sim = (x.similarity != null) ? (x.similarity * 100).toFixed(1) + "%" : "";
  const badge = x.source_type ? '<span class="badge">' + esc(x.source_type) + "</span>" : "";
  const when = x.issued_at ? " \u00B7 " + esc(x.issued_at) : "";
  return '<div class="card">'
    + '<div class="top"><div><span class="headline">' + esc(x.headline || "Weather document")
    + "</span>" + badge + "</div><div class=\\"sim\\">" + sim + "</div></div>"
    + '<div class="meta">' + esc(x.location || "") + when + "</div>"
    + '<div class="chunk">' + esc(x.chunk_text || "") + "</div></div>";
}

async function sync() {
  const raw = $("locs").value.trim();
  if (!raw) { $("status").textContent = "Enter locations to sync (semicolon-separated)."; return; }
  const locations = raw.split(";").map(s => s.trim()).filter(Boolean);
  $("syncBtn").disabled = true;
  $("status").textContent = "Syncing\u2026 (documents only; run the embed job to make them searchable)";
  try {
    const r = await fetch("/weather/sync", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ locations, limit: 50 })
    });
    const data = await r.json();
    $("status").textContent = data.error
      ? "Sync error: " + data.error
      : "Synced " + data.synced + " document(s). Run the embedding job to index them.";
  } catch (e) {
    $("status").textContent = "Sync failed: " + e.message;
  } finally { $("syncBtn").disabled = false; }
}

function esc(s){ return String(s).replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
</script>
</body>
</html>"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Databricks Apps injects PORT; default 8000 for local runs.
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))