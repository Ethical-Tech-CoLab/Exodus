# Exodus

**Civilian Evacuation Risk Platform · NYU Ethical CoLab**

A single FastAPI application unifying three civilian-evacuation risk tools:

| Tab | Origin | What it does |
|---|---|---|
| **Crisis Map** | EII (Evacuation Inform Index) | Interactive world map of INFORM Severity Index crises with live Tavily news + ACLED conflict timeline per country |
| **Scenario Model** | ERCF (Evacuation Risk Classification Framework) | 7-dimension risk scoring, resource requirements, cost-of-staying vs cost-of-evacuating, scenario CRUD |
| **Risk Assessment** | CERAI (Civilian Evacuation Risk Anticipation Index) | Endangerment + Feasibility assessment with gauges, GDELT news feed, 47 historical cases |

All three share one backend, one design language, one set of API keys.

## Quick start

```bash
cp .env.example .env       # fill in keys (optional — fallbacks work)
pip install -r requirements.txt
python main.py             # http://localhost:8000
```

## API surface

- `GET  /`                          UI (3-tab shell)
- `GET  /health`                    Service status + key availability
- `GET  /docs`                      Auto-generated OpenAPI docs
- `GET  /api/world-risk[/{iso3}]`   INFORM Severity by country
- `GET  /api/iso-lookup`            TopoJSON numeric → ISO3
- `POST /api/country-context`       AI-powered (Claude) country analysis
- `GET  /api/acaps/{iso3}`          ACAPS INFORM + access + risk-radar + crises
- `GET  /api/detail`                EII drawer: Tavily news + ACLED timeline
- `GET  /api/news?q=...`            GDELT keyless news search
- `GET  /api/weather?lat=&lon=`     Open-Meteo current weather
- `POST /api/calculate/risk`        ERCF risk score + level + NATO equivalent
- `POST /api/calculate/resources`   Vehicles, personnel, supplies
- `POST /api/calculate/staying-cost`    USD cost + expected casualties
- `POST /api/calculate/remaining-cost`  USD cost to evacuate + shelter
- `*    /api/scenarios[/{id}]`      Scenario CRUD (SQLite)
- `GET  /api/historical-cases[/{id}]`   Historical analogues

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  static/index.html  (3-tab shell)                    │
│  ├── crisis_map.html        (Leaflet + data.js)      │
│  ├── scenario_model.html    (ERCF form)              │
│  └── risk_assessment.html   (CERAI single-page app)  │
└──────────────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│  FastAPI (main.py)                                   │
│  ├── calculators.py     ERCF math                    │
│  ├── database.py        SQLite scenarios             │
│  ├── world_risk.py      INFORM-derived country index │
│  ├── historical_data.py 15 reference cases           │
│  ├── context_ai.py      Anthropic (with fallback)    │
│  ├── acaps_data.py      ACAPS REST client            │
│  └── live_data.py       Tavily + ACLED + GDELT       │
└──────────────────────────────────────────────────────┘
```

## Credits

- **EII** — Ethical Tech CoLab · github.com/Ethical-Tech-CoLab/evacmodel
- **ERCF** — yagorocha-web/ercf
- **CERAI** — alanarobertson/evac-ai-project-2-
- Data: ACAPS INFORM Severity Index (April 2026), ACLED, GDELT 2.0, Open-Meteo

Unified by NYU Ethical CoLab.
