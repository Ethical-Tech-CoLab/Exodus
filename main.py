"""
Exodus — unified civilian evacuation risk platform (NYU Ethical CoLab).

Three lenses over one backend:
  • Crisis Map        (EII / INFORM Severity Index)
  • Scenario Model    (ERCF — 7-dimension risk + cost model)
  • Risk Assessment   (CERAI — endangerment + feasibility)

All three share the same FastAPI live-data layer (ACAPS, Tavily, ACLED, GDELT,
Open-Meteo, Anthropic).
"""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import os
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from acaps_data import get_country_data
from calculators import (
    calculate_cerai_scores,
    calculate_remaining_costs,
    calculate_resources,
    calculate_risk,
    calculate_staying_costs,
    default_dims_for_level,
)
from context_ai import analyze_country
from database import (
    create_scenario,
    delete_scenario,
    get_scenario,
    init_db,
    list_scenarios,
    update_scenario,
)
from historical_data import HISTORICAL_CASES
from live_data import (
    SESSION_USAGE,
    crisis_detail,
    earth_image,
    gdelt_articles,
    open_meteo_current,
    usage_read,
)
from world_risk import NUM_TO_ISO3, get_all_risk_levels, get_risk_by_iso3

app = FastAPI(
    title="Exodus — NYU Ethical CoLab",
    description="Unified civilian evacuation risk platform (EII + ERCF + CERAI).",
    version="1.0.0",
)


# ─── Pydantic schemas (ERCF tab) ─────────────────────────────────────────────

class Dims(BaseModel):
    d1_kinetic:       float = Field(..., ge=1, le=5)
    d2_vulnerability: float = Field(..., ge=1, le=5)
    d3_political:     float = Field(..., ge=1, le=5)
    d4_logistics:     float = Field(..., ge=1, le=5)
    d5_destination:   float = Field(..., ge=1, le=5)
    d6_urgency:       float = Field(..., ge=1, le=5)
    d7_information:   float = Field(..., ge=1, le=5)


class ScenarioIn(Dims):
    name:                str
    description:         Optional[str]   = ""
    population:          int             = Field(..., gt=0)
    vulnerable_pct:      float           = Field(20.0, ge=0, le=100)
    distance_km:         float           = Field(50.0, gt=0)
    terrain:             int             = Field(3, ge=1, le=5)
    conflict_pattern:    int             = Field(5, ge=1, le=5)
    conflict_lat:        Optional[float] = None
    conflict_lng:        Optional[float] = None
    safe_zone_lat:       Optional[float] = None
    safe_zone_lng:       Optional[float] = None
    safe_zone_name:      Optional[str]   = None
    distance_source:     str             = "manual"
    road_factor_applied: bool            = False
    haversine_km:        Optional[float] = None


class ResourceReq(BaseModel):
    population:     int   = Field(..., gt=0)
    vulnerable_pct: float = Field(20.0, ge=0, le=100)
    distance_km:    float = Field(50.0, gt=0)
    risk_level:     int   = Field(..., ge=0, le=4)
    d2_mobility:    float = Field(3.0, ge=1, le=5)
    terrain:        int   = Field(3, ge=1, le=5)


class StayReq(BaseModel):
    population:    int   = Field(..., gt=0)
    risk_level:    int   = Field(..., ge=0, le=4)
    days:          int   = Field(..., ge=1, le=365)
    d1:            float = Field(3.0, ge=1, le=5)
    d2:            float = Field(3.0, ge=1, le=5)
    d3:            float = Field(3.0, ge=1, le=5)
    d4:            float = Field(3.0, ge=1, le=5)
    remaining_pct: float = Field(1.0, ge=0, le=1)
    conflict_type: str   = Field("auto")


class RemainingReq(BaseModel):
    population:     int   = Field(..., gt=0)
    vulnerable_pct: float = Field(20.0, ge=0, le=100)
    risk_level:     int   = Field(..., ge=0, le=4)
    days:           int   = Field(..., ge=1, le=365)
    distance_km:    float = Field(50.0, gt=0)
    terrain:        int   = Field(3, ge=1, le=5)
    d1:             float = Field(3.0, ge=1, le=5)
    d2:             float = Field(3.0, ge=1, le=5)
    d3:             float = Field(3.0, ge=1, le=5)
    d4:             float = Field(3.0, ge=1, le=5)
    d5:             float = Field(3.0, ge=1, le=5)
    d6:             float = Field(3.0, ge=1, le=5)
    d7:             float = Field(3.0, ge=1, le=5)


class CountryContextReq(BaseModel):
    iso3:         str
    country_name: Optional[str] = ""


# ─── Startup ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup():
    # Don't let a read-only / locked filesystem (e.g. Vercel's lambda runtime
    # outside /tmp) take the whole app down. Scenario endpoints will surface
    # their own errors when called.
    try:
        init_db()
    except Exception as e:
        print(f"[exodus] init_db skipped at startup: {e}")


# ─── Static UI ────────────────────────────────────────────────────────────────

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# Force fresh fetches of HTML/JS/CSS so the iframed tabs always pick up the
# latest edits. Without this, browsers happily serve stale tab HTML out of
# disk cache even when the user hard-refreshes the parent shell.
@app.middleware("http")
async def _no_cache_static(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/")
async def root():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/health")
async def health():
    return {
        "app":     "Exodus",
        "version": app.version,
        "keys": {
            "acaps":     bool(os.environ.get("ACAPS_API_KEY")),
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "tavily":    bool(os.environ.get("TAVILY_API_KEY")),
            "acled":     bool(os.environ.get("ACLED_EMAIL") and os.environ.get("ACLED_PASSWORD")),
            "nasa":      bool(os.environ.get("NASA_API_KEY")),
        },
        "countries_tracked": len(get_all_risk_levels()),
    }


# ─── Scenario CRUD (ERCF tab) ────────────────────────────────────────────────

@app.get("/api/scenarios")
async def list_all():
    return list_scenarios()


@app.post("/api/scenarios", status_code=201)
async def create(body: ScenarioIn):
    risk = calculate_risk(body.model_dump())
    data = {**body.model_dump(), **risk}
    return create_scenario(data)


@app.get("/api/scenarios/{sid}")
async def get(sid: int):
    s = get_scenario(sid)
    if not s:
        raise HTTPException(404, "Not found")
    return s


@app.put("/api/scenarios/{sid}")
async def update(sid: int, body: ScenarioIn):
    risk = calculate_risk(body.model_dump())
    data = {**body.model_dump(), **risk}
    updated = update_scenario(sid, data)
    if not updated:
        raise HTTPException(404, "Not found")
    return updated


@app.delete("/api/scenarios/{sid}")
async def delete(sid: int):
    if not delete_scenario(sid):
        raise HTTPException(404, "Not found")
    return {"ok": True}


# ─── Calculation endpoints ───────────────────────────────────────────────────

@app.post("/api/calculate/risk")
async def calc_risk(body: Dims):
    return calculate_risk(body.model_dump())


@app.post("/api/calculate/resources")
async def calc_resources(body: ResourceReq):
    return calculate_resources(
        body.population, body.vulnerable_pct, body.risk_level,
        body.distance_km, body.d2_mobility, body.terrain,
    )


@app.post("/api/calculate/staying-cost")
async def calc_staying(body: StayReq):
    dims = {"d1": body.d1, "d2": body.d2, "d3": body.d3, "d4": body.d4}
    return calculate_staying_costs(
        body.population, body.risk_level, body.days, dims,
        body.remaining_pct, body.conflict_type,
    )


@app.post("/api/calculate/remaining-cost")
async def calc_remaining(body: RemainingReq):
    dims = {k: getattr(body, k) for k in ("d1", "d2", "d3", "d4", "d5", "d6", "d7")}
    return calculate_remaining_costs(
        body.population, body.vulnerable_pct, body.risk_level,
        body.days, body.distance_km, dims, body.terrain,
    )


# ─── Historical case dataset ─────────────────────────────────────────────────

@app.get("/api/historical-cases")
async def historical():
    return HISTORICAL_CASES


@app.get("/api/historical-cases/{cid}")
async def historical_one(cid: int):
    for c in HISTORICAL_CASES:
        if c["id"] == cid:
            return c
    raise HTTPException(404, "Not found")


# ─── World risk map (Crisis Map tab) ─────────────────────────────────────────

@app.get("/api/world-risk")
async def world_risk():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_all_risk_levels)


@app.get("/api/world-risk/{iso3}")
async def country_risk(iso3: str):
    iso3u = iso3.upper()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_risk_by_iso3, iso3u)


@app.get("/api/iso-lookup")
async def iso_lookup():
    return NUM_TO_ISO3


@app.post("/api/country-context")
async def country_context(body: CountryContextReq):
    return analyze_country(body.iso3.upper(), body.country_name or "")


# ─── ACAPS live data ─────────────────────────────────────────────────────────

@app.get("/api/acaps/{iso3}")
async def acaps_country(iso3: str):
    iso3u = iso3.upper()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_country_data, iso3u)


@app.get("/api/country-context-acaps/{iso3}")
async def country_context_acaps(iso3: str):
    if not os.environ.get("ACAPS_API_KEY", "").strip():
        return {
            "error":   "ACAPS_API_KEY not configured",
            "results": [],
            "_note":   "Add ACAPS_API_KEY=<token> to your .env. Register at https://api.acaps.org/register/",
        }
    iso3u = iso3.upper()
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, get_country_data, iso3u)
    return {"results": data.get("inform", []), "count": len(data.get("inform", []))}


# ─── EII drawer endpoint (Tavily + ACLED) ────────────────────────────────────

@app.get("/api/detail")
async def api_detail(
    crisis:  str = Query(""),
    country: str = Query(""),
    nocache: bool = Query(False),
    days:    int = Query(60, ge=1, le=365),
    place:   str = Query(""),
    roads:   str = Query("1"),
):
    want_roads = roads not in ("0", "false", "no")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: crisis_detail(crisis, country, nocache, days, place, want_roads))


@app.get("/api/usage")
async def api_usage():
    """Report API spend so the user can watch their Tavily credits."""
    return {
        "total":   usage_read(),      # cumulative across all runs (persisted)
        "session": SESSION_USAGE,     # since this server process started
        "note": "Tavily basic search = 1 API credit. A detail call spends 2 "
                "(news + road access); add ?roads=0 to spend 1. Cached results "
                "(≤6h) cost 0. Check your monthly limit at app.tavily.com.",
    }


@app.get("/api/earth-image")
async def api_earth_image(lat: float, lon: float, dim: str = Query("0.5")):
    """Proxy NASA's keyed Earth Imagery API — key stays server-side."""
    loop = asyncio.get_event_loop()
    data, err = await loop.run_in_executor(None, earth_image, lat, lon, dim)
    if err:
        payload, status = err
        return JSONResponse(payload, status_code=status)
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})


# ─── CERAI live feeds (GDELT keyless + Open-Meteo) ───────────────────────────

@app.get("/api/news")
async def api_news(q: str = Query(..., min_length=2), n: int = Query(10, ge=1, le=50)):
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, gdelt_articles, q, n)
    return res or {"items": []}


@app.get("/api/weather")
async def api_weather(lat: float, lon: float):
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, open_meteo_current, lat, lon)
    return res or {"error": "Open-Meteo unavailable"}


# ─── Unified synthesis (Brief tab) ───────────────────────────────────────────

class SynthesisReq(BaseModel):
    """Single input bundle that drives all three indices.

    iso3 pulls INFORM context. If a dimension is omitted, it is derived from
    the country's INFORM severity (or moderate defaults if none).
    """
    iso3:              Optional[str]   = None
    population:        int             = Field(..., gt=0)
    vulnerable_pct:    float           = Field(20.0, ge=0, le=100)
    distance_km:       float           = Field(50.0, gt=0)
    terrain:           int             = Field(3, ge=1, le=5)
    conflict_pattern:  int             = Field(5, ge=1, le=5)
    days:              int             = Field(30, ge=1, le=365)
    remaining_pct:     float           = Field(0.4, ge=0, le=1)
    d1_kinetic:        Optional[float] = Field(None, ge=1, le=5)
    d2_vulnerability:  Optional[float] = Field(None, ge=1, le=5)
    d3_political:      Optional[float] = Field(None, ge=1, le=5)
    d4_logistics:      Optional[float] = Field(None, ge=1, le=5)
    d5_destination:    Optional[float] = Field(None, ge=1, le=5)
    d6_urgency:        Optional[float] = Field(None, ge=1, le=5)
    d7_information:    Optional[float] = Field(None, ge=1, le=5)


def _fmt_usd(n: float) -> str:
    return "${:,}".format(int(round(n)))


def _recommend(ercf_level: int, stay_total: float, evac_total: float,
               casualties: float, feasibility: float) -> dict:
    delta = stay_total - evac_total
    if ercf_level <= 1:
        return {
            "verdict":   "MONITOR",
            "rationale": (f"ERCF level {ercf_level} — no operational evacuation indicated. "
                          "Maintain situational awareness and pre-position registration data."),
        }
    if ercf_level >= 3 and delta > 0 and feasibility >= 25:
        return {
            "verdict":   "EVACUATE",
            "rationale": (f"Cost of staying ({_fmt_usd(stay_total)}) exceeds evacuation "
                          f"({_fmt_usd(evac_total)}) by {_fmt_usd(delta)}, "
                          f"with ~{casualties:.0f} casualties prevented."),
        }
    if ercf_level >= 3 and feasibility < 25:
        return {
            "verdict":   "HOLD",
            "rationale": (f"High risk but feasibility is {feasibility:.0f}/100 — corridor security "
                          "or destination capacity blocks execution. Negotiate access before launch."),
        }
    if ercf_level >= 3 and delta <= 0:
        return {
            "verdict":   "HOLD",
            "rationale": (f"High risk, but evacuation cost ({_fmt_usd(evac_total)}) currently "
                          f"exceeds projected cost of staying ({_fmt_usd(stay_total)}). "
                          "Reassess at next decision point or revise destination."),
        }
    return {
        "verdict":   "PREPARE",
        "rationale": ("Moderate composite risk. Pre-position transport and supplies; "
                      "define explicit triggers (ERCF dimension thresholds) for execution."),
    }


@app.post("/api/synthesis")
async def synthesis(body: SynthesisReq):
    """Run all three indices on one input bundle and return a unified verdict."""
    # 1. INFORM context (or empty placeholder)
    inform = get_risk_by_iso3(body.iso3) if body.iso3 else {
        "iso3": "", "name": "", "crisis": "", "level": 2, "sev": 0,
        "inform_score": 0, "drivers": "", "trend": "", "source": "no pin",
    }
    base_level = inform.get("level", 2)

    # 2. Resolve dimensions — fill missing from severity defaults
    defaults = default_dims_for_level(base_level)
    dims = {
        "d1_kinetic":       body.d1_kinetic       if body.d1_kinetic       is not None else defaults["d1_kinetic"],
        "d2_vulnerability": body.d2_vulnerability if body.d2_vulnerability is not None else defaults["d2_vulnerability"],
        "d3_political":     body.d3_political     if body.d3_political     is not None else defaults["d3_political"],
        "d4_logistics":     body.d4_logistics     if body.d4_logistics     is not None else defaults["d4_logistics"],
        "d5_destination":   body.d5_destination   if body.d5_destination   is not None else defaults["d5_destination"],
        "d6_urgency":       body.d6_urgency       if body.d6_urgency       is not None else defaults["d6_urgency"],
        "d7_information":   body.d7_information   if body.d7_information   is not None else defaults["d7_information"],
    }

    # 3. Run all three lenses
    ercf  = calculate_risk(dims)
    cerai = calculate_cerai_scores(dims)
    res   = calculate_resources(body.population, body.vulnerable_pct,
                                ercf["risk_level"], body.distance_km,
                                dims["d2_vulnerability"], body.terrain)
    pattern_map = {1: "urban_siege", 2: "enclave", 3: "city_conflict",
                   4: "regional",    5: "auto"}
    stay = calculate_staying_costs(
        body.population, ercf["risk_level"], body.days,
        {"d1": dims["d1_kinetic"], "d2": dims["d2_vulnerability"],
         "d3": dims["d3_political"], "d4": dims["d4_logistics"]},
        body.remaining_pct, pattern_map.get(body.conflict_pattern, "auto"),
    )
    evac = calculate_remaining_costs(
        body.population, body.vulnerable_pct, ercf["risk_level"], body.days,
        body.distance_km,
        {"d1": dims["d1_kinetic"], "d2": dims["d2_vulnerability"],
         "d3": dims["d3_political"], "d4": dims["d4_logistics"],
         "d5": dims["d5_destination"], "d6": dims["d6_urgency"],
         "d7": dims["d7_information"]},
        body.terrain,
    )

    # 4. Recommendation — v6 model shapes: staying cost/toll from stay["totals"],
    # evacuation operation cost from the resources summary (remaining-cost now
    # prices the in-situ assistance stream, not the evacuation itself).
    stay_total = stay["totals"]["financial_usd"]
    casualties = stay["totals"]["deaths"] + stay["totals"]["injuries"]
    evac_total = res["summary"]["total_cost_usd"]
    rec = _recommend(ercf["risk_level"], stay_total, evac_total,
                     casualties, cerai["feasibility"])
    rec["cost_delta_usd"]       = round(stay_total - evac_total, 2)
    rec["casualties_prevented"] = round(casualties)

    # 5. Top historical analogues at this risk level (or one below)
    target_lvl = ercf["risk_level"]
    matched = [c for c in HISTORICAL_CASES if c["risk_level"] in (target_lvl, max(0, target_lvl - 1))]
    matched.sort(key=lambda c: abs(c["population_at_risk"] - body.population))
    analogues = matched[:4]

    return {
        "inform":         inform,
        "cerai":          cerai,
        "ercf":           ercf,
        "dims_used":      dims,
        "resources":      res,
        "staying_cost":   stay,
        "remaining_cost": evac,
        "recommendation": rec,
        "analogues":      analogues,
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
