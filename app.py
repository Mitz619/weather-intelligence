"""app.py — Flask entrypoint for the Weather Intelligence app.

If you're merging into the databricks-lakebase-app-day-2 reference app,
you only need the two blueprint lines below in your existing app.py.
This standalone version exists so the repo runs on its own.
"""

from flask import Flask, jsonify

from weather_routes import weather_bp

app = Flask(__name__)
app.register_blueprint(weather_bp)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Databricks Apps injects PORT; default 8000 for local runs.
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
