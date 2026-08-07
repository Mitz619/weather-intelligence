"""weather_client.py — National Weather Service (api.weather.gov) client.

Harvests unstructured weather text (active alerts + narrative forecasts) and
normalizes each item into a document record ready for Lakebase.

Two ways to run:
  1. Imported by the Flask app (POST /weather/sync calls harvest_documents()).
  2. Standalone / GitHub Actions fallback:
       python weather_client.py "Chicago, IL" "Austin, TX" --out weather_docs.jsonl
     (Use this if outbound calls are ever blocked from your compute — same
      pattern as the OpenAlex → GitHub Actions pipeline.)

NWS API notes:
  - Free, no API key. A descriptive User-Agent header is REQUIRED.
  - No geocoder: friendly city names are resolved via KNOWN_LOCATIONS below,
    or pass raw "lat,lon" strings.
"""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.weather.gov"
# NWS asks for a contact in the User-Agent. Put your email/repo here.
USER_AGENT = "weather-intelligence-homework (student project; github.com/<your-repo>)"

# NWS has no geocoding endpoint, so map friendly names to (lat, lon).
# Add more cities as needed, or pass "lat,lon" directly.
KNOWN_LOCATIONS = {
    "chicago, il": (41.8781, -87.6298),
    "austin, tx": (30.2672, -97.7431),
    "new york, ny": (40.7128, -74.0060),
    "seattle, wa": (47.6062, -122.3321),
    "miami, fl": (25.7617, -80.1918),
    "denver, co": (39.7392, -104.9903),
    "new orleans, la": (29.9511, -90.0715),
    "kansas city, mo": (39.0997, -94.5786),
}


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/geo+json"})
    return s


def _get(session, url, params=None):
    """GET with simple retry/backoff on transient errors."""
    last = None
    for attempt in range(3):
        resp = session.get(url, params=params, timeout=30)
        last = resp
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503):
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
    last.raise_for_status()


def resolve_location(location):
    """Accepts 'Chicago, IL' or '41.88,-87.63'. Returns (label, lat, lon)."""
    loc = location.strip()
    parts = [p.strip() for p in loc.split(",")]
    if len(parts) == 2:
        try:
            return loc, float(parts[0]), float(parts[1])
        except ValueError:
            pass
    key = loc.lower()
    if key in KNOWN_LOCATIONS:
        lat, lon = KNOWN_LOCATIONS[key]
        return loc, lat, lon
    raise ValueError(
        f"Unknown location '{location}'. Use 'lat,lon' or add it to KNOWN_LOCATIONS."
    )


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _stable_id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _normalize_alert(feature, location_label):
    props = feature.get("properties", {})
    description = (props.get("description") or "").strip()
    instruction = (props.get("instruction") or "").strip()
    narrative = description
    if instruction:
        narrative = f"{description}\n\nINSTRUCTIONS: {instruction}"
    if not narrative:
        return None
    return {
        "id": props.get("id") or _stable_id("alert", location_label, props.get("sent")),
        "location": location_label,
        "source_type": "alert",
        "headline": props.get("event") or props.get("headline") or "Weather Alert",
        "narrative_text": narrative,
        "issued_at": props.get("sent") or props.get("effective") or _now_iso(),
        "payload": json.dumps(props),
        "synced_at": _now_iso(),
    }


def _normalize_forecast_period(period, location_label):
    narrative = (period.get("detailedForecast") or "").strip()
    if not narrative:
        return None
    return {
        "id": _stable_id("forecast", location_label, period.get("startTime"), period.get("name")),
        "location": location_label,
        "source_type": "forecast",
        "headline": f"{period.get('name', 'Forecast')} — {period.get('shortForecast', '')}".strip(" —"),
        "narrative_text": narrative,
        "issued_at": period.get("startTime") or _now_iso(),
        "payload": json.dumps(period),
        "synced_at": _now_iso(),
    }


def harvest_documents(locations, limit=50):
    """Fetch alerts + forecasts for each location. Returns a list of document dicts.

    limit applies per location across (alerts + forecast periods).
    Documents are de-duplicated by id (the same statewide alert can appear
    for two cities in the same state).
    """
    session = _session()
    docs = {}

    for location in locations:
        label, lat, lon = resolve_location(location)
        count = 0

        # 1. Resolve to an NWS grid point. Response includes forecast URL + state.
        point = _get(session, f"{BASE_URL}/points/{lat},{lon}")
        pprops = point.get("properties", {})
        forecast_url = pprops.get("forecast")
        state = (
            pprops.get("relativeLocation", {})
            .get("properties", {})
            .get("state")
        )

        # 2. Active alerts for the state.
        if state:
            alerts = _get(session, f"{BASE_URL}/alerts/active", params={"area": state})
            for feature in alerts.get("features", []):
                doc = _normalize_alert(feature, label)
                if doc and count < limit:
                    docs[doc["id"]] = doc
                    count += 1

        # 3. Multi-day narrative forecast.
        if forecast_url:
            forecast = _get(session, forecast_url)
            for period in forecast.get("properties", {}).get("periods", []):
                doc = _normalize_forecast_period(period, label)
                if doc and count < limit:
                    docs[doc["id"]] = doc
                    count += 1

    return list(docs.values())


def main():
    parser = argparse.ArgumentParser(description="Harvest NWS weather documents to JSONL.")
    parser.add_argument("locations", nargs="+", help='e.g. "Chicago, IL" or "41.88,-87.63"')
    parser.add_argument("--limit", type=int, default=50, help="Max docs per location")
    parser.add_argument("--out", default="weather_docs.jsonl", help="Output JSONL path")
    args = parser.parse_args()

    docs = harvest_documents(args.locations, limit=args.limit)
    with open(args.out, "w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc) + "\n")
    print(f"Wrote {len(docs)} documents to {args.out}")


if __name__ == "__main__":
    sys.exit(main())
