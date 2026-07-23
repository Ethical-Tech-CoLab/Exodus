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

---

## Peer Review

The full independent academic peer review of this platform is in [PEER-REVIEW.md](PEER-REVIEW.md) (also available as [Word](peer-review/Exodus-Peer-Review.docx) under [`peer-review/`](peer-review/)).

**Recommendation:** Major revisions — the integration is real work and the newest layer (the synthesis endpoint and the Brief tab) is where the problems are concentrated.

**What the review found:**

- **The casualty model returns figures the platform's own flagship case refutes.** Run `historical_data.py`'s Mariupol record (430,000 at risk, 86 days, ERCF level 4, siege) through `calculate_staying_costs` and it projects 61,714 deaths in 30 days (14.4% of the population) and 176,912 over 86 days — 41% of the city. OHCHR verified 1,348 civilian deaths; the contested high-end estimate is ~21,000. The base rate `0.0008` (calculators.py:164) carries no citation, and the figure is printed on the Brief tab as "casualties prevented."
- **The Brief's verdict is decided on dollars; casualties are passed in but never used.** `_recommend` (main.py:375–408) uses `casualties` only to format a sentence — the branch conditions are ERCF level, cost delta, and feasibility. A level-4 "Critical" scenario returns HOLD whenever evacuating costs more money than staying. This inverts the CERAI tab's own argument ("Financial feasibility is a constraint on the actionability of evacuation, not a measure of its necessity. IHL obligations exist independently of cost").
- **The cost comparison is not like-for-like.** Staying cost uses `remaining_pct` of the population (default 0.4); evacuating cost uses the full population — so the default run compares 172,000 people staying against 430,000 leaving, biasing every verdict toward HOLD.
- **Two different models are both called CERAI and they disagree** — the Assess tab's client-side v3 (three dimensions, geometric mean, vulnerability multiplier) and the server's `calculate_cerai_scores` (a linear blend of the seven ERCF dimensions), reported under the same name and gauge labels on the Brief.
- **The server-side CERAI cannot reach its own extremes** — endangerment is bounded to [20, 100] and feasibility to [0, 80], making the "Minimal" label unreachable dead code and steering severe scenarios toward HOLD.
- **The NATO cross-reference is misattributed.** "PHASE 0 — Routine / Standfast / Restricted / Reduce / Evacuate" is labelled a NATO STANAG equivalent; AJP-3.4.2 states a NEO is conducted in four phases — preparatory, preliminary, evacuation, and withdrawal and recovery — and contains none of those terms.
- **`conflict_type: "auto"` promises inference the code does not perform** — it resolves to `regional`, the mildest archetype, and is the default path through `/api/synthesis`.
- **Documentation describes a different build:** the README documents three tabs and three tools where the shell ships six; `ARCHITECTURE.md` is the pre-merge ERCF document, stamped June 2026, and never mentions Exodus, CERAI, EII, or `/api/synthesis`; the README advertises 47 historical cases where the API serves 15; and the credits point at personal forks rather than the canonical org repos, omitting the routing and behavioural simulators entirely.
