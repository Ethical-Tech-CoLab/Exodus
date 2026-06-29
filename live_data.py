"""
Live external-data layer for the Crisis Map drawer + Risk Assessment news feed.

Wraps three keyed providers:
  - Tavily    : real-time news per crisis (EII original)
  - ACLED     : structured conflict events for the timeline (EII original)
  - GDELT     : 2.0 doc API — keyless, for CERAI's news feed
  - Open-Meteo: keyless current weather, for CERAI quick environmental context

Disk-caches successful responses for 6 hours to avoid hammering the upstreams.
"""
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE_DIR = os.path.join(_HERE, ".cache")
_TTL = 6 * 3600  # 6 hours

_ACLED_ALIAS = {
    "DR Congo": "Democratic Republic of Congo",
    "Congo DRC": "Democratic Republic of Congo",
    "DRC": "Democratic Republic of Congo",
    "CAR": "Central African Republic",
    "Syrian Arab Republic": "Syria",
    "occupied Palestinian territory": "Palestine",
    "State of Palestine": "Palestine",
    "Venezuela (Bolivarian Republic of)": "Venezuela",
    "Iran (Islamic Republic of)": "Iran",
    "Tanzania": "United Republic of Tanzania",
    "Moldova": "Republic of Moldova",
}


def _cache_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")


def _cache_get(key: str):
    p = _cache_path(key)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < _TTL:
        try:
            with open(p, "r") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _cache_set(key: str, obj):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_cache_path(key), "w") as f:
            json.dump(obj, f)
    except Exception:
        pass


def _http_json(url: str, data=None, headers=None, method: str = "GET", timeout: int = 30):
    body = json.dumps(data).encode() if data is not None else None
    h = {"Content-Type": "application/json", "User-Agent": "Exodus/1.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ─── Tavily ──────────────────────────────────────────────────────────────────

def tavily_news(query: str, days: int = 60, max_results: int = 10) -> Optional[dict]:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return None
    out = _http_json("https://api.tavily.com/search", method="POST", data={
        "api_key": key, "query": query, "topic": "news",
        "days": days, "max_results": max_results,
        "include_answer": True, "search_depth": "basic",
    })
    items = []
    for r in out.get("results", []):
        items.append({
            "title":   r.get("title"),
            "url":     r.get("url"),
            "date":    r.get("published_date"),
            "snippet": (r.get("content") or "")[:260],
            "source":  urllib.parse.urlparse(r.get("url", "")).netloc.replace("www.", ""),
        })
    return {"answer": out.get("answer"), "items": items}


# ─── ACLED ───────────────────────────────────────────────────────────────────

def acled_timeline(country: str, months: int = 18) -> Optional[dict]:
    key = os.environ.get("ACLED_API_KEY", "").strip()
    email = os.environ.get("ACLED_EMAIL", "").strip()
    if not (key and email):
        return None
    name = _ACLED_ALIAS.get(country, country)
    since = (datetime.utcnow() - timedelta(days=months * 31)).strftime("%Y-%m-%d")
    url = "https://api.acleddata.com/acled/read?" + urllib.parse.urlencode({
        "key": key, "email": email, "country": name,
        "event_date": since, "event_date_where": ">=",
        "fields": "event_date|event_type|fatalities", "limit": 5000,
    })
    out = _http_json(url)
    buckets, types = {}, {}
    for e in out.get("data", []):
        m = (e.get("event_date") or "")[:7]
        if not m:
            continue
        b = buckets.setdefault(m, {"month": m, "events": 0, "fatalities": 0})
        b["events"] += 1
        try:
            b["fatalities"] += int(e.get("fatalities") or 0)
        except (TypeError, ValueError):
            pass
        t = e.get("event_type") or "Other"
        types[t] = types.get(t, 0) + 1
    return {
        "country_used":     name,
        "months":           sorted(buckets.values(), key=lambda x: x["month"]),
        "by_type":          sorted(types.items(), key=lambda x: -x[1]),
        "total_events":     sum(b["events"] for b in buckets.values()),
        "total_fatalities": sum(b["fatalities"] for b in buckets.values()),
    }


# ─── GDELT (keyless) ─────────────────────────────────────────────────────────

def gdelt_articles(query: str, max_records: int = 10) -> Optional[dict]:
    encoded = urllib.parse.quote(query)
    url = (f"https://api.gdeltproject.org/api/v2/doc/doc?query={encoded}"
           f"&mode=artlist&maxrecords={max_records}&format=json")
    try:
        out = _http_json(url)
    except Exception:
        return None
    items = []
    for a in out.get("articles", []):
        items.append({
            "title":      a.get("title"),
            "url":        a.get("url"),
            "date":       a.get("seendate"),
            "source":     a.get("domain"),
            "language":   a.get("language"),
            "sourcecountry": a.get("sourcecountry"),
        })
    return {"items": items}


# ─── Open-Meteo (keyless current weather) ────────────────────────────────────

def open_meteo_current(lat: float, lon: float) -> Optional[dict]:
    if lat is None or lon is None:
        return None
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,precipitation,wind_speed_10m,weather_code")
    try:
        out = _http_json(url, timeout=12)
    except Exception:
        return None
    return out.get("current") or out


# ─── EII-style detail aggregator ─────────────────────────────────────────────

def crisis_detail(crisis: str, country: str, nocache: bool = False) -> dict:
    """Combined Tavily + ACLED payload, matching EII's /api/detail shape."""
    ckey = f"detail|{crisis}|{country}"
    if not nocache:
        hit = _cache_get(ckey)
        if hit:
            hit["cached"] = True
            return hit

    resp = {
        "crisis": crisis, "country": country, "cached": False,
        "tavily": None, "acled": None, "errors": [], "keys": {},
    }
    resp["keys"]["tavily"] = bool(os.environ.get("TAVILY_API_KEY"))
    if resp["keys"]["tavily"]:
        try:
            resp["tavily"] = tavily_news(
                f"{country} {crisis} latest conflict, security and humanitarian developments")
        except Exception as e:
            resp["errors"].append(f"tavily: {e}")
    else:
        resp["errors"].append("no_tavily_key")

    resp["keys"]["acled"] = bool(os.environ.get("ACLED_API_KEY") and os.environ.get("ACLED_EMAIL"))
    if resp["keys"]["acled"]:
        try:
            resp["acled"] = acled_timeline(country)
        except Exception as e:
            resp["errors"].append(f"acled: {e}")
    else:
        resp["errors"].append("no_acled_key")

    if resp.get("tavily") or resp.get("acled"):
        _cache_set(ckey, resp)
    return resp
