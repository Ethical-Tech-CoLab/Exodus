"""
ERCF calculation logic.

Implements the four core functions main.py imports:
  - calculate_risk(dims)           -> {risk_score, risk_level, risk_label, nato_equivalent, weights}
  - calculate_resources(...)       -> vehicles, personnel, water, food, fuel
  - calculate_staying_costs(...)   -> per-day + cumulative cost of NOT evacuating
  - calculate_remaining_costs(...) -> cost of evacuating the remaining population

All money values are USD, indicative — for planning, not procurement.
"""
from math import ceil
from typing import Dict


# ─── Risk model ──────────────────────────────────────────────────────────────

# Weights per ERCF dimension. Kinetic + urgency drive the score hardest; the
# information dimension is a tiebreaker.
_WEIGHTS = {
    "d1_kinetic":       0.22,
    "d2_vulnerability": 0.16,
    "d3_political":     0.12,
    "d4_logistics":     0.14,
    "d5_destination":   0.12,
    "d6_urgency":       0.18,
    "d7_information":   0.06,
}

_LEVEL_LABELS = ["Baseline", "Low", "Moderate", "High", "Critical"]

# ERCF level -> NATO STANAG equivalent (used in evac doctrine cross-references)
_NATO = ["PHASE 0 — Routine", "PHASE 1 — Standfast", "PHASE 2 — Restricted",
         "PHASE 3 — Reduce", "PHASE 4 — Evacuate"]


def _level_from_score(score: float) -> int:
    if score >= 4.25:
        return 4
    if score >= 3.5:
        return 3
    if score >= 2.5:
        return 2
    if score >= 1.75:
        return 1
    return 0


def calculate_risk(dims: Dict) -> Dict:
    """Weighted-average risk over D1..D7, then bin into ERCF levels 0..4."""
    score = sum(_WEIGHTS[k] * float(dims[k]) for k in _WEIGHTS)
    level = _level_from_score(score)
    return {
        "risk_score":      round(score, 3),
        "risk_level":      level,
        "risk_label":      _LEVEL_LABELS[level],
        "nato_equivalent": _NATO[level],
        "weights":         _WEIGHTS,
    }


# ─── Resource planning ───────────────────────────────────────────────────────

# Bus / coach capacity per vehicle, with mobility (d2) reducing throughput
_BUS_CAPACITY = 45
# Per-person daily SPHERE-ish minima for staging area
_WATER_L_PER_DAY    = 15.0   # litres / person / day
_FOOD_KCAL_PER_DAY  = 2100   # kcal / person / day  (~0.6 kg dry ration)
_FUEL_L_PER_KM_BUS  = 0.35   # diesel litres / km

# Terrain multiplier for vehicle count (1=urban, 5=alpine/desert)
_TERRAIN_MULT = {1: 1.0, 2: 1.05, 3: 1.15, 4: 1.30, 5: 1.55}


def calculate_resources(population: int, vulnerable_pct: float, risk_level: int,
                        distance_km: float, d2_mobility: float, terrain: int) -> Dict:
    """Estimate vehicles, personnel, and supplies for a single-wave evacuation."""
    vulnerable = int(round(population * vulnerable_pct / 100.0))
    # Reduce effective bus capacity as mobility worsens (d2 = 1..5)
    cap = _BUS_CAPACITY * (1.0 - 0.08 * (d2_mobility - 1))
    cap = max(cap, 22.0)
    terrain_m = _TERRAIN_MULT.get(int(terrain), 1.15)

    buses     = ceil(population / cap * terrain_m)
    ambulances = ceil(vulnerable / 6.0 * (1 + 0.15 * max(d2_mobility - 3, 0)))
    drivers   = buses + ambulances + ceil(buses * 0.25)        # +relief drivers
    medics    = ceil(vulnerable / 25.0) + max(risk_level, 1)
    security  = ceil(population / 250.0) * (1 + 0.5 * (risk_level >= 3))
    coordinators = max(2, ceil(buses / 12.0))

    one_way_fuel_l = buses * distance_km * _FUEL_L_PER_KM_BUS
    # Round trip + 20% reserve
    fuel_l = ceil(one_way_fuel_l * 2.4)

    # 1 day of supplies pre-staged at receiving site
    water_l = ceil(population * _WATER_L_PER_DAY)
    food_kcal = population * _FOOD_KCAL_PER_DAY
    food_kg = ceil(population * 0.6)

    return {
        "vehicles": {
            "buses":      buses,
            "ambulances": ambulances,
        },
        "personnel": {
            "drivers":      drivers,
            "medics":       medics,
            "security":     security,
            "coordinators": coordinators,
        },
        "supplies": {
            "water_l":   water_l,
            "food_kg":   food_kg,
            "food_kcal": food_kcal,
            "fuel_l":    fuel_l,
        },
        "assumptions": {
            "bus_capacity":         round(cap, 1),
            "terrain_multiplier":   terrain_m,
            "vulnerable_count":     vulnerable,
            "distance_km":          distance_km,
        },
    }


# ─── Staying cost (people who DON'T evacuate) ─────────────────────────────────

_CONFLICT_TYPES = {
    "urban_siege":   {"label": "Urban siege",   "confinement": 1.40, "morbidity": 1.30},
    "enclave":       {"label": "Encircled enclave", "confinement": 1.55, "morbidity": 1.45},
    "city_conflict": {"label": "Active city conflict", "confinement": 1.25, "morbidity": 1.20},
    "regional":      {"label": "Regional conflict",    "confinement": 1.10, "morbidity": 1.05},
}

# Daily per-capita "cost of staying" — combination of life-support, medical and protection
# Indicative USD/person/day at risk level 0; multiplied by (1 + risk_level * 0.6)
_STAYING_BASE_USD = 12.0


def _normalize_conflict_type(label: str) -> str:
    if label and label.lower() in _CONFLICT_TYPES:
        return label.lower()
    return "regional"


def calculate_staying_costs(population: int, risk_level: int, days: int,
                            dims: Dict, remaining_pct: float,
                            conflict_type: str = "auto") -> Dict:
    """Cost (USD) for people who remain inside the affected zone."""
    ct_key = _normalize_conflict_type(conflict_type if conflict_type != "auto" else "regional")
    ct = _CONFLICT_TYPES[ct_key]

    remaining = int(round(population * max(min(remaining_pct, 1.0), 0.0)))
    # Confinement modifier driven by authorisation (d3) + logistics collapse (d4)
    auth = float(dims.get("d3", 3.0))
    log_ = float(dims.get("d4", 3.0))
    confinement = ct["confinement"] * (1 + 0.06 * ((auth - 3) + (log_ - 3)))
    confinement = max(confinement, 1.0)

    daily = _STAYING_BASE_USD * (1 + risk_level * 0.6) * confinement
    per_day_cost = remaining * daily
    total = per_day_cost * days

    casualty_rate_per_day = 0.0008 * (1 + risk_level * 0.9) * ct["morbidity"]
    expected_casualties = round(remaining * casualty_rate_per_day * days, 1)

    return {
        "conflict_type":         ct["label"],
        "remaining_population":  remaining,
        "per_capita_per_day":    round(daily, 2),
        "cost_per_day":          round(per_day_cost, 2),
        "total_cost":            round(total, 2),
        "days":                  days,
        "expected_casualties":   expected_casualties,
        "confinement_modifier":  round(confinement, 3),
    }


# ─── Remaining cost (people who DO evacuate) ─────────────────────────────────

# Distance band cost (USD/person) — buses, escort, intake, registration
_DISTANCE_BAND_USD = [
    (50,   28),
    (150,  62),
    (350,  140),
    (800,  240),
    (2000, 410),
]


def _distance_cost_per_person(distance_km: float) -> float:
    for upper, cost in _DISTANCE_BAND_USD:
        if distance_km <= upper:
            return cost
    return _DISTANCE_BAND_USD[-1][1] * 1.4


def calculate_cerai_scores(dims: Dict) -> Dict:
    """Derive CERAI's two headline scores from the same ERCF dimensions.

    Endangerment (0–100, higher = more endangered) blends the threat-side
    dimensions; Feasibility (0–100, higher = more feasible to evacuate)
    inverts the operational-obstacle dimensions.
    """
    e_raw = (float(dims["d1_kinetic"]) * 0.45 +
             float(dims["d2_vulnerability"]) * 0.20 +
             float(dims["d6_urgency"]) * 0.35)
    endangerment = max(0.0, min(100.0, e_raw * 20.0))

    f_raw = (float(dims["d3_political"]) * 0.20 +
             float(dims["d4_logistics"]) * 0.35 +
             float(dims["d5_destination"]) * 0.20 +
             float(dims["d7_information"]) * 0.25)
    feasibility = max(0.0, min(100.0, 100.0 - (f_raw * 20.0)))

    def e_lbl(v):
        if v >= 80: return "Critical"
        if v >= 60: return "High"
        if v >= 40: return "Moderate"
        if v >= 20: return "Low"
        return "Minimal"

    def f_lbl(v):
        if v >= 60: return "Open"
        if v >= 40: return "Constrained"
        if v >= 20: return "Hard"
        return "Blocked"

    return {
        "endangerment":       round(endangerment, 1),
        "feasibility":        round(feasibility, 1),
        "endangerment_label": e_lbl(endangerment),
        "feasibility_label":  f_lbl(feasibility),
    }


# ─── Dimension defaults derived from INFORM severity ─────────────────────────

# D3 runs the same direction as every other dimension: 1 = full consent from all
# armed parties, 5 = active refusal or no valid authorization. Higher is worse.
# Every consumer below assumes this — the composite weight, the confinement
# modifier, the CERAI feasibility term and the coordination multiplier.
_SEV_TO_DIMS = [
    {"d1_kinetic":1.0,"d2_vulnerability":1.5,"d3_political":3.0,"d4_logistics":2.0,"d5_destination":2.0,"d6_urgency":1.0,"d7_information":1.5},
    {"d1_kinetic":2.0,"d2_vulnerability":2.5,"d3_political":3.5,"d4_logistics":2.5,"d5_destination":2.5,"d6_urgency":2.0,"d7_information":2.0},
    {"d1_kinetic":3.0,"d2_vulnerability":3.0,"d3_political":3.5,"d4_logistics":3.0,"d5_destination":2.5,"d6_urgency":3.0,"d7_information":2.5},
    {"d1_kinetic":4.0,"d2_vulnerability":3.5,"d3_political":3.5,"d4_logistics":3.5,"d5_destination":3.0,"d6_urgency":4.0,"d7_information":3.0},
    {"d1_kinetic":5.0,"d2_vulnerability":4.0,"d3_political":4.5,"d4_logistics":4.0,"d5_destination":3.0,"d6_urgency":4.5,"d7_information":4.0},
]


def default_dims_for_level(level: int) -> Dict:
    """Return plausible ERCF dimension values for an ERCF level 0..4."""
    return dict(_SEV_TO_DIMS[max(0, min(4, int(level)))])


def calculate_remaining_costs(population: int, vulnerable_pct: float,
                              risk_level: int, days: int, distance_km: float,
                              dims: Dict, terrain: int) -> Dict:
    """Cost (USD) to evacuate the remaining population and shelter for `days`."""
    vulnerable = int(round(population * vulnerable_pct / 100.0))
    base = _distance_cost_per_person(distance_km)

    terrain_m = _TERRAIN_MULT.get(int(terrain), 1.15)
    risk_m    = 1 + (risk_level * 0.18)
    # Information opacity (d7) and authorisation (d3) raise coordination cost
    info_m    = 1 + 0.04 * (float(dims.get("d7", 3.0)) - 3)
    auth_m    = 1 + 0.05 * (float(dims.get("d3", 3.0)) - 3)

    move_per_person = base * terrain_m * risk_m * info_m * auth_m
    move_total = move_per_person * population

    # Reception / shelter — SPHERE-aligned daily figure
    shelter_per_day_per_person = 8.5 + (1.6 * (vulnerable_pct / 100.0)) + (0.5 * risk_level)
    shelter_total = shelter_per_day_per_person * population * days

    # Vulnerable-population uplift (medical, escort, child protection)
    vulnerable_uplift = vulnerable * 35 * (1 + 0.2 * risk_level)

    total = move_total + shelter_total + vulnerable_uplift

    return {
        "evacuated_population":       population,
        "vulnerable_count":           vulnerable,
        "transport_per_capita":       round(move_per_person, 2),
        "transport_total":            round(move_total, 2),
        "shelter_per_day_per_capita": round(shelter_per_day_per_person, 2),
        "shelter_total":              round(shelter_total, 2),
        "vulnerable_uplift_total":    round(vulnerable_uplift, 2),
        "total_cost":                 round(total, 2),
        "days":                       days,
    }
