"""
Country risk lookup, derived from the INFORM Severity Index dataset shipped
with the Crisis Map tab (static/data.js).

Re-parses data.js at module load (cheap — runs once at import) so a single
source of truth backs both the map UI and the API.

Public API:
  - WORLD_RISK            : dict[iso3, dict]   raw lookup used by context_ai
  - get_all_risk_levels() : dict[iso3, dict]   API-friendly view
  - get_risk_by_iso3(iso3): dict               single country (or empty)
  - NUM_TO_ISO3           : dict[str, str]     TopoJSON numeric -> ISO3
"""
import json
import os
import re
from typing import Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA_JS = os.path.join(_HERE, "static", "data.js")


# ─── Parse data.js once at import ────────────────────────────────────────────

def _load_inform_data() -> List[dict]:
    """Extract the EDI_DATA array from static/data.js."""
    if not os.path.exists(_DATA_JS):
        return []
    with open(_DATA_JS, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"window\.EDI_DATA\s*=\s*(\[.*?\]);", text, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(1))
    except Exception:
        return []


def _sev_to_ercf_level(sev: int) -> int:
    """INFORM severity (1..5) -> ERCF level (0..4)."""
    return max(0, min(4, int(sev) - 1))


def _build_world_risk() -> Dict[str, dict]:
    """Aggregate per-ISO3, taking the most severe crisis per country."""
    out: Dict[str, dict] = {}
    for c in _load_inform_data():
        iso3 = (c.get("iso3") or "").upper()
        if not iso3:
            continue
        existing = out.get(iso3)
        # Keep the highest-severity entry per country
        if existing and existing.get("sev", 0) >= c.get("sev", 0):
            continue
        out[iso3] = {
            "iso3":          iso3,
            "name":          c.get("name", ""),
            "crisis":        c.get("crisis", ""),
            "level":         _sev_to_ercf_level(c.get("sev", 1)),
            "sev":           c.get("sev"),
            "inform_score":  c.get("informIdx"),
            "drivers":       c.get("drivers", ""),
            "trend":         c.get("trend", ""),
            "lat":           c.get("lat"),
            "lng":           c.get("lng"),
            "edi":           c.get("edi"),
            # Fields used by context_ai fallback (best-effort defaults)
            "pop_at_risk":   None,
            "conflict_type": _infer_conflict_type(c.get("drivers", "")),
            "access_label":  _infer_access_label(c.get("sev", 1)),
            "access":        max(0, min(5, int(c.get("sev", 1)) - 1)),
            "exit_routes":   [],
            "actors":        [],
            "source":        "INFORM Severity Index — April 2026 (ACAPS / EU JRC)",
        }
    return out


def _infer_conflict_type(drivers: str) -> str:
    d = (drivers or "").lower()
    if "conflict" in d and "displacement" in d:
        return "Complex Emergency (NIAC + displacement)"
    if "conflict" in d:
        return "NIAC (non-international armed conflict)"
    if "displacement" in d:
        return "Refugee / IDP situation"
    if "flood" in d or "cyclone" in d or "drought" in d:
        return "Climatic / natural disaster"
    return "Unclassified"


def _infer_access_label(sev) -> str:
    return {
        5: "Extreme constraints — multi-actor obstruction",
        4: "High constraints — frequent denial / interference",
        3: "Moderate constraints — episodic interference",
        2: "Low constraints — generally permissive",
        1: "Open access",
    }.get(int(sev or 1), "Unknown")


WORLD_RISK: Dict[str, dict] = _build_world_risk()


# ─── Public API ──────────────────────────────────────────────────────────────

def get_all_risk_levels() -> Dict[str, dict]:
    """All countries keyed by ISO3."""
    return WORLD_RISK


def get_risk_by_iso3(iso3: str) -> dict:
    """Country lookup. Returns a level-0 placeholder for unknown ISO3."""
    iso3u = (iso3 or "").upper()
    if iso3u in WORLD_RISK:
        return WORLD_RISK[iso3u]
    return {
        "iso3":          iso3u,
        "name":          iso3u,
        "crisis":        "No active ACAPS-tracked crisis",
        "level":         0,
        "sev":           0,
        "inform_score":  0,
        "drivers":       "",
        "trend":         "",
        "lat":           None,
        "lng":           None,
        "edi":           None,
        "pop_at_risk":   0,
        "conflict_type": "Baseline",
        "access_label":  "Open access",
        "access":        0,
        "exit_routes":   [],
        "actors":        [],
        "source":        "Default — not in INFORM Severity Index",
    }


# ─── TopoJSON numeric → ISO3 (UN M49 -> ISO 3166-1 alpha-3) ──────────────────
# Subset covering all countries currently surfaced in WORLD_RISK plus major
# neighbouring states. World-Atlas TopoJSON keys countries by UN M49 numeric.

NUM_TO_ISO3: Dict[str, str] = {
    "004": "AFG", "008": "ALB", "012": "DZA", "020": "AND", "024": "AGO", "031": "AZE",
    "032": "ARG", "036": "AUS", "040": "AUT", "044": "BHS", "048": "BHR", "050": "BGD",
    "051": "ARM", "056": "BEL", "064": "BTN", "068": "BOL", "070": "BIH", "072": "BWA",
    "076": "BRA", "084": "BLZ", "090": "SLB", "096": "BRN", "100": "BGR", "104": "MMR",
    "108": "BDI", "112": "BLR", "116": "KHM", "120": "CMR", "124": "CAN", "140": "CAF",
    "144": "LKA", "148": "TCD", "152": "CHL", "156": "CHN", "158": "TWN", "170": "COL",
    "174": "COM", "178": "COG", "180": "COD", "188": "CRI", "191": "HRV", "192": "CUB",
    "196": "CYP", "203": "CZE", "204": "BEN", "208": "DNK", "212": "DMA", "214": "DOM",
    "218": "ECU", "222": "SLV", "226": "GNQ", "231": "ETH", "232": "ERI", "233": "EST",
    "242": "FJI", "246": "FIN", "250": "FRA", "262": "DJI", "266": "GAB", "268": "GEO",
    "270": "GMB", "275": "PSE", "276": "DEU", "288": "GHA", "300": "GRC", "320": "GTM",
    "324": "GIN", "328": "GUY", "332": "HTI", "340": "HND", "344": "HKG", "348": "HUN",
    "352": "ISL", "356": "IND", "360": "IDN", "364": "IRN", "368": "IRQ", "372": "IRL",
    "376": "ISR", "380": "ITA", "384": "CIV", "388": "JAM", "392": "JPN", "398": "KAZ",
    "400": "JOR", "404": "KEN", "408": "PRK", "410": "KOR", "414": "KWT", "417": "KGZ",
    "418": "LAO", "422": "LBN", "426": "LSO", "428": "LVA", "430": "LBR", "434": "LBY",
    "440": "LTU", "442": "LUX", "450": "MDG", "454": "MWI", "458": "MYS", "466": "MLI",
    "478": "MRT", "480": "MUS", "484": "MEX", "496": "MNG", "498": "MDA", "499": "MNE",
    "504": "MAR", "508": "MOZ", "512": "OMN", "516": "NAM", "520": "NRU", "524": "NPL",
    "528": "NLD", "548": "VUT", "554": "NZL", "558": "NIC", "562": "NER", "566": "NGA",
    "578": "NOR", "586": "PAK", "591": "PAN", "598": "PNG", "600": "PRY", "604": "PER",
    "608": "PHL", "616": "POL", "620": "PRT", "624": "GNB", "626": "TLS", "630": "PRI",
    "634": "QAT", "642": "ROU", "643": "RUS", "646": "RWA", "682": "SAU", "686": "SEN",
    "688": "SRB", "690": "SYC", "694": "SLE", "702": "SGP", "703": "SVK", "704": "VNM",
    "705": "SVN", "706": "SOM", "710": "ZAF", "716": "ZWE", "724": "ESP", "728": "SSD",
    "729": "SDN", "748": "SWZ", "752": "SWE", "756": "CHE", "760": "SYR", "762": "TJK",
    "764": "THA", "768": "TGO", "780": "TTO", "784": "ARE", "788": "TUN", "792": "TUR",
    "795": "TKM", "800": "UGA", "804": "UKR", "807": "MKD", "818": "EGY", "826": "GBR",
    "834": "TZA", "840": "USA", "854": "BFA", "858": "URY", "860": "UZB", "862": "VEN",
    "882": "WSM", "887": "YEM", "894": "ZMB",
}
