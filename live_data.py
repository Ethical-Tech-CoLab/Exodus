"""
Live external-data layer for the Crisis Map drawer + Risk Assessment news feed.

Wraps the keyed providers:
  - Tavily    : real-time news per crisis + road-access reports (EII original)
  - ACLED     : structured conflict events for the timeline (OAuth2 — the
                legacy api.acleddata.com key+email endpoint is deprecated)
  - NASA      : Earth Imagery snapshot proxy (key stays server-side)
  - GDELT     : 2.0 doc API — keyless, for CERAI's news feed
  - Open-Meteo: keyless current weather, for CERAI quick environmental context

Disk-caches successful responses for 6 hours to avoid hammering the upstreams,
and counts real outbound Tavily/ACLED/NASA calls so the UI can show API spend.
"""
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE_DIR = os.path.join(_HERE, ".cache")
_TTL = 6 * 3600  # 6 hours

# ─── API-usage tracking (so you don't blow through Tavily credits) ───────────
# Tavily bills per *search* (basic = 1 credit), not per token. Every real
# outbound call is counted here; cache hits cost nothing and are not counted.
_USAGE_PATH = os.path.join(_CACHE_DIR, "usage.json")
SESSION_USAGE = {"tavily": 0, "acled": 0, "earth_image": 0}  # this process only


def usage_bump(kind: str):
    SESSION_USAGE[kind] = SESSION_USAGE.get(kind, 0) + 1
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        data = json.load(open(_USAGE_PATH)) if os.path.exists(_USAGE_PATH) else {}
        data[kind] = data.get(kind, 0) + 1
        json.dump(data, open(_USAGE_PATH, "w"))
    except Exception:
        pass


def usage_read() -> dict:
    try:
        return json.load(open(_USAGE_PATH)) if os.path.exists(_USAGE_PATH) else {}
    except Exception:
        return {}


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
    "Bolivia": "Bolivia",
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


# ─── Road access / blockages (EII) ───────────────────────────────────────────
# Whether the roads out are usable is the single most decisive input to
# evacuation feasibility, and no open, global, structured road-closure feed
# covers conflict zones — so this reads the news for it via Tavily.
#
# Two honest limits, surfaced in the UI rather than hidden:
#   1. News prose carries NO geometry. There are no coordinates for "the
#      Salah al-Din road is cut", so blockages are reported as a list and a
#      feasibility signal, never drawn as lines on the map.
#   2. The status below is keyword-derived from the headline and snippet, not
#      verified. It is a triage hint that points at a source to read.
ROAD_QUERY = ("road closures, blocked highways, destroyed bridges, checkpoints, "
              "border crossing closures and impassable evacuation routes")
# Word boundaries are load-bearing here: without them "closure" fires on
# "disclosure", "mined" on "examined", and "passable" on "impassable" — which
# would flip a blocked road to reopened.
ROAD_PATTERNS = [
    ("blocked",    r"\bblock(ed|ade|ades|ing|s)?\b|\bclos(e|es|ed|ure|ures|ing)\b|"
                   r"\bshut(s|ting)?\b|\bhalt(s|ed|ing)?\b|\bsuspend(s|ed|ing)?\b|"
                   r"\bseal(ed|s|ing)?\b|cut off|cut-off|impassab|inaccessib|besieg|siege|"
                   r"encircl|\btrapped\b|\bstranded\b|no way out"),
    ("damaged",    r"destroy|damag|collaps|\bbomb(ed|ing|s)?\b|\bshell(ed|ing)\b|"
                   r"\bstruck\b|washed away|landslide|\bflood(ed|ing|s)?\b|"
                   r"landmine|land mine|\bmined\b|\bcrater(s|ed)?\b"),
    ("checkpoint", r"checkpoint|road ?block|barricade|\bpermit(s)?\b|screening|"
                   r"turned back|denied passage"),
    ("reopened",   r"reopen|re-open|restored|\bclear(ed|ing)\b|repair|resumed|"
                   r"\bpassable\b|corridor open|humanitarian corridor"),
]
# Weight per status when turning counts into a 0–1 obstruction signal. Reopenings
# genuinely offset blockages, so they subtract.
ROAD_WEIGHTS = {"blocked": 1.0, "damaged": 0.8, "checkpoint": 0.5, "reopened": -0.5}
ROAD_SATURATE = 5.0   # weighted score at which the signal reaches 1.0

# ─── Relevance gates ─────────────────────────────────────────────────────────
# Tavily ranks by relevance but still returns loosely-related news, and the
# status patterns above are generic enough to fire on any of it. Without these
# gates every crisis saturated at signal 1.0, which would have applied the
# maximum feasibility penalty to all crises on evidence about other continents.

ROAD_SUBJECT = (r"\broad(s|way|ways|block|blocks)?\b|\bhighway(s)?\b|\bbridge(s)?\b|"
                r"\broute(s)?\b|\bcorridor(s)?\b|\bcheckpoint(s)?\b|\bcrossing(s)?\b|"
                r"\bstreet(s)?\b|\bmotorway(s)?\b|\bconvoy(s)?\b|\boverland\b|"
                r"\bland route(s)?\b|\bsupply line(s)?\b|\bevacuation route(s)?\b|"
                r"\btravel\b|\btraffic\b|\bdrive\b|\bdriving\b|\bvehicle(s)?\b")
# Encirclement is the most absolute road blockage there is, and it is reported
# without ever naming a road, so this language satisfies the subject gate alone.
SIEGE_SUBJECT = (r"\bbesieg(e|ed|ing)?\b|\bsiege\b|\bencircl(e|ed|ing|ement)\b|"
                 r"\bsurrounded\b|\bcut off\b|\bcut-off\b|\bsealed off\b|"
                 r"\btrapped\b|no way out|\bescape\b|\bfleeing\b|\bflee\b|"
                 r"\bstranded\b|\bblockade(d)?\b")
# Maritime and air disruption uses the same verbs ("blockade", "closed") but
# says nothing about whether people can drive out.
NON_ROAD_SUBJECT = (r"\bnaval\b|\bmaritime\b|\bshipping\b|\bvessel(s)?\b|\btanker(s)?\b|"
                    r"\bport(s)?\b|\bharbou?r(s)?\b|\bsea ?lane(s)?\b|\bairspace\b|"
                    r"\bflight(s)?\b|\bairport(s)?\b|\bairline(s)?\b|\bstrait(s)?\b|"
                    r"\bcanal\b|\bwaterway(s)?\b|\bgulf\b|\bred sea\b|\bhormuz\b|"
                    r"\brunway(s)?\b|\bferry\b|\bferries\b|"
                    r"\bchoke ?point(s)?\b|\btrade route(s)?\b|\btrade\b|"
                    r"\bexport(s|ed|ing)?\b|\bimport(s|ed|ing)?\b|\bcargo\b|"
                    r"\bfreight\b|\bcommercial traffic\b")
# Words that carry no geographic signal, so they must not satisfy the place gate.
_PLACE_STOPWORDS = {
    "republic", "democratic", "state", "states", "islamic", "federal", "united",
    "people", "peoples", "kingdom", "territory", "occupied", "province",
    "region", "north", "south", "east", "west", "northern", "southern",
    "eastern", "western", "central", "greater", "new", "city", "district",
}

# A word that is some *other* country's entire name cannot identify this one.
_OTHER_COUNTRY_WORDS = {"sudan", "congo", "guinea", "korea", "niger", "china"}

# Demonyms and plurals a country name takes in prose — anchored at the front of
# the token so "Yemen" reaches "Yemeni" without "Niger" reaching "Nigeria".
_DEMONYM_SUFFIX = r"(i|is|s|n|ns|an|ans|ese|na|ien|iens)?"

# Country names in the INFORM data are not the names reporters use.
PLACE_ALIASES = {
    "CAR": ("central african", "bangui"),
    "DRC": ("congo", "kinshasa", "goma", "kivu", "ituri"),
    "DR Congo": ("congo", "kinshasa", "goma", "kivu", "ituri"),
    "Democratic Republic of Congo": ("congo", "kinshasa", "goma", "kivu", "ituri"),
    "Palestine": ("gaza", "west bank", "rafah", "khan younis", "jerusalem"),
    "occupied Palestinian territory": ("palestin", "gaza", "west bank", "rafah"),
    "State of Palestine": ("palestin", "gaza", "west bank", "rafah"),
    "Sudan": ("sudan", "khartoum", "darfur", "el fasher", "el-fasher",
              "obeid", "omdurman"),
    "South Sudan": ("south sudan", "juba", "upper nile", "unity state"),
    "Syria": ("syria", "aleppo", "damascus", "idlib", "homs"),
    "Yemen": ("yemen", "sanaa", "sana'a", "aden", "hodeidah", "taiz", "marib"),
    "Myanmar": ("myanmar", "burma", "rakhine", "kachin", "shan", "sagaing"),
    "Ethiopia": ("ethiopia", "tigray", "amhara", "oromia", "afar"),
    "Nigeria": ("nigeria", "borno", "maiduguri", "yobe", "adamawa"),
    "Somalia": ("somalia", "mogadishu", "puntland", "jubaland"),
    "Mali": ("mali", "bamako", "mopti", "gao", "timbuktu"),
    "Burkina Faso": ("burkina", "ouagadougou", "sahel region"),
    "Niger": ("niger", "niamey", "diffa", "tillaberi"),
    "Afghanistan": ("afghan", "kabul", "kandahar", "herat"),
    "Ukraine": ("ukrain", "kyiv", "kharkiv", "donetsk", "kherson", "zaporizhzhia"),
    "Lebanon": ("lebanon", "beirut", "bekaa", "lebanese"),
    "Haiti": ("haiti", "port-au-prince", "haitian"),
    "Mozambique": ("mozambiqu", "cabo delgado", "beira", "pemba"),
    "Venezuela": ("venezuela", "caracas"),
    "Chad": ("chad", "n'djamena", "ndjamena"),
    "Cameroon": ("cameroon", "yaounde", "douala", "far north"),
}

# Sub-national crises need the same treatment one level down. Keyed by a
# lowercase substring of the curated place label; an absent entry falls back
# to the words in the place label itself.
SUBPLACE_ALIASES = {
    "mindanao": ("mindanao", "barmm", "cotabato", "maguindanao", "marawi",
                 "lanao", "zamboanga", "davao", "general santos", "sultan kudarat",
                 "sulu", "basilan", "tawi-tawi", "surigao"),
    "gaza": ("gaza", "rafah", "khan younis", "deir al-balah", "jabalia"),
    "west bank": ("west bank", "jenin", "nablus", "hebron", "ramallah", "tulkarem"),
    "cabo delgado": ("cabo delgado", "pemba", "mocimboa", "palma", "macomia"),
    "cox's bazar": ("cox's bazar", "coxs bazar", "kutupalong", "balukhali", "teknaf"),
    "darién": ("darien", "darién", "bajo chiquito", "canaan membrillo"),
}


def _tok_pattern(tok: str) -> str:
    """A place token as a regex: anchored at a word start, demonyms allowed."""
    if " " in tok or "-" in tok or "'" in tok:
        return r"\b" + re.escape(tok)
    return r"\b" + re.escape(tok) + _DEMONYM_SUFFIX + r"\b"


def _match_tokens(text: str, toks) -> bool:
    return any(re.search(_tok_pattern(tok), text) for tok in toks)


def _words_of(src):
    """Significant lowercase words in a place or country name."""
    return [t for t in re.split(r"[^a-z']+", (src or "").lower())
            if len(t) >= 4 and t not in _PLACE_STOPWORDS]


def _all_words_of(src):
    """Every word of a name, stopwords kept, so multi-word names stay whole."""
    return [t for t in re.split(r"[^a-z']+", (src or "").lower()) if len(t) >= 3]


def _place_tokens(country: str, place: Optional[str] = None) -> set:
    """Distinctive lowercase terms that mark an item as being about this crisis."""
    toks = set()
    full = _all_words_of(country)
    if len(full) > 1:
        toks.add(" ".join(full))
        toks.update(w for w in _words_of(country) if w not in _OTHER_COUNTRY_WORDS)
    else:
        toks.update(_words_of(country))
    toks.update(_place_only_tokens(country, place))
    for alias in PLACE_ALIASES.get(country, ()):
        toks.add(alias.lower())
    return toks


def _place_only_tokens(country: str, place: Optional[str] = None) -> set:
    """Tokens that identify the sub-national area *and not merely the country*."""
    if not place:
        return set()
    toks = {w for w in _words_of(place)}
    low = place.lower()
    for key, aliases in SUBPLACE_ALIASES.items():
        if key in low:
            toks.update(a.lower() for a in aliases)
    country_words = set(_words_of(country)) | {
        a.lower() for a in PLACE_ALIASES.get(country, ())}
    return {t for t in toks if t not in country_words}


def road_item_is_relevant(text: str, country: str, place: Optional[str] = None) -> bool:
    """True if this item is plausibly about land access *in this crisis's area*.

    Both gates are required: right-topic-wrong-country and
    right-country-wrong-topic are the two observed failure modes.
    """
    t = (text or "").lower()
    if not (re.search(ROAD_SUBJECT, t) or re.search(SIEGE_SUBJECT, t)):
        return False
    # Maritime/air-only items mention no land subject beyond the generic verbs.
    if re.search(NON_ROAD_SUBJECT, t) and not re.search(
            r"\broad(s|way|ways)?\b|\bhighway(s)?\b|\bbridge(s)?\b|\bcheckpoint(s)?\b|"
            r"\bland route(s)?\b|\boverland\b|\bconvoy(s)?\b|\bbesieg|\bsiege\b|"
            r"\bencircl", t):
        return False
    # A crisis with a curated sub-national area has to be matched at that level;
    # under-counting is the safer error and the UI labels absent reports.
    if place:
        sub = _place_only_tokens(country, place)
        return bool(sub) and _match_tokens(t, sub)
    toks = _place_tokens(country, place)
    if not toks:
        # No way to confirm the item is about this crisis — treat as unverified.
        return False
    return _match_tokens(t, toks)


# The negation has to be read, not just the keyword: "no damage reported" is
# not road damage.
NEGATED_DAMAGE = (r"\bno (immediate )?(reports? of )?(major |serious |significant )?"
                  r"(damage|damages|casualties)\b|"
                  r"\bwithout (major |serious )?damage\b|"
                  r"\bno damage (was |were )?(reported|recorded)\b|"
                  r"\bdamage (was |were )?not reported\b")
# `reopened` subtracts from the obstruction signal, so require the reopening to
# be predicated of something people travel on.
REOPEN_SUBJECT = (r"\broad(s|way|ways)?\b|\bhighway(s)?\b|\bbridge(s)?\b|"
                  r"\broute(s)?\b|\bcorridor(s)?\b|\bcrossing(s)?\b|\bborder(s)?\b|"
                  r"\bport(s)?\b|\bpass(es)?\b|\baccess\b|\btraffic\b|\bconvoy(s)?\b|"
                  r"\bsupply line(s)?\b|\bcheckpoint(s)?\b")


def classify_road(text: str):
    """Return (primary_status, all_matched_tags) for one news item.

    `status` prefers the obstruction reading, because under-calling a blocked
    route is the more dangerous error for an evacuation tool to make.
    """
    t = (text or "").lower()
    tags = [name for name, pat in ROAD_PATTERNS if re.search(pat, t)]
    if "damaged" in tags and re.search(NEGATED_DAMAGE, t):
        tags.remove("damaged")
    if "reopened" in tags and not re.search(REOPEN_SUBJECT, t):
        tags.remove("reopened")
    for name in ("blocked", "damaged", "checkpoint", "reopened"):
        if name in tags:
            return name, tags
    return "unclear", tags


def crisis_query(country: str, crisis: str, place: Optional[str] = None) -> str:
    """The search subject for one crisis; `place` anchors sub-national reporting."""
    return f"{place}, {country} {crisis}" if place else f"{country} {crisis}"


_HEADLINE_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "from",
    "as", "by", "with", "into", "after", "amid", "over", "still", "says", "say",
    "new", "more", "than", "that", "this", "it", "its", "is", "are", "was",
    "were", "be", "been", "has", "have", "had", "will", "could", "would",
}


def _headline_key(title: Optional[str]) -> set:
    """Content words of a headline, with the syndicating outlet's suffix removed."""
    t = (title or "").lower()
    t = re.sub(r"\s+[-–|]\s+[^-–|]{1,40}$", "", t)      # trailing " - Outlet"
    words = [w for w in re.split(r"[^a-z0-9']+", t)
             if w and w not in _HEADLINE_STOPWORDS]
    return set(words)


def _is_duplicate(item: dict, kept: list) -> bool:
    """True if `item` reports the same story as something already kept."""
    url = (item.get("url") or "").split("?")[0].rstrip("/")
    key = _headline_key(item.get("title"))
    for k in kept:
        if url and url == (k.get("url") or "").split("?")[0].rstrip("/"):
            return True
        other = _headline_key(k.get("title"))
        if not key or not other:
            continue
        overlap = len(key & other) / len(key | other)
        if overlap >= 0.75:
            return True
    return False


def tavily_roads(country: str, crisis: str, days: int = 60,
                 max_results: int = 10, place: Optional[str] = None) -> Optional[dict]:
    """Road-access items for one crisis, classified and scored."""
    news = tavily_news(f"{crisis_query(country, crisis, place)} {ROAD_QUERY}",
                       days=days, max_results=max_results)
    if news is None:
        return None
    items, counts = [], {"blocked": 0, "damaged": 0, "checkpoint": 0, "reopened": 0}
    dropped = duplicates = 0
    for it in news["items"]:
        blob = f"{it.get('title','')} {it.get('snippet','')}"
        # Relevance first: an item about another country, or about shipping
        # rather than roads, must not reach the classifier at all.
        if not road_item_is_relevant(blob, country, place):
            dropped += 1
            continue
        status, tags = classify_road(blob)
        if status == "unclear":
            continue                       # no road language at all — drop the noise
        # Deduplicate after classification so the count reflects distinct
        # reports. One wire story on four outlets is one road blockage.
        if _is_duplicate(it, items):
            duplicates += 1
            continue
        counts[status] += 1
        items.append(dict(it, status=status, tags=tags))
    score = sum(ROAD_WEIGHTS[s] * n for s, n in counts.items())
    signal = max(0.0, min(1.0, score / ROAD_SATURATE))
    return {"answer": news.get("answer"), "items": items, "counts": counts,
            "signal": round(signal, 3), "considered": len(news["items"]),
            "off_topic": dropped, "duplicates": duplicates, "query_days": days}


# ─── ACLED (OAuth2) ──────────────────────────────────────────────────────────
# ACLED migrated to OAuth2 in 2025: log in with email+password -> Bearer token
# (valid 24h) -> query https://acleddata.com/api/acled/read. The legacy
# api.acleddata.com key+email endpoint is deprecated.
ACLED_OAUTH_URL = "https://acleddata.com/oauth/token"
ACLED_READ_URL = "https://acleddata.com/api/acled/read"
_ACLED_TOKEN = {"token": None, "exp": 0}


def acled_token(email: str, password: str) -> str:
    if _ACLED_TOKEN["token"] and _ACLED_TOKEN["exp"] - 120 > time.time():
        return _ACLED_TOKEN["token"]
    tp = os.path.join(_CACHE_DIR, "acled_token.json")
    if os.path.exists(tp):
        try:
            c = json.load(open(tp))
            if c.get("exp", 0) - 120 > time.time():
                _ACLED_TOKEN.update(c)
                return c["token"]
        except Exception:
            pass
    body = urllib.parse.urlencode({
        "username": email, "password": password, "grant_type": "password",
        "client_id": "acled", "scope": "authenticated",
    }).encode()
    req = urllib.request.Request(
        ACLED_OAUTH_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "Exodus/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    tok = out["access_token"]
    exp = time.time() + int(out.get("expires_in", 86400))
    _ACLED_TOKEN.update({"token": tok, "exp": exp})
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        json.dump({"token": tok, "exp": exp}, open(tp, "w"))
    except Exception:
        pass
    return tok


def acled_timeline(country: str, months: int = 18) -> Optional[dict]:
    email = os.environ.get("ACLED_EMAIL", "").strip()
    password = os.environ.get("ACLED_PASSWORD", "").strip()
    if not (email and password):
        return None
    name = _ACLED_ALIAS.get(country, country)
    token = acled_token(email, password)
    # Some access tiers serve only data >=12 months old; request a wide lower
    # bound and let the API enforce its own recency cutoff. Page ascending.
    since = (datetime.utcnow() - timedelta(days=(months + 14) * 31)).strftime("%Y-%m-%d")
    buckets, types = {}, {}
    newest, cutoff, total_rows, truncated = None, None, 0, False
    MAX_PAGES = 30
    page = 1
    while page <= MAX_PAGES:
        url = ACLED_READ_URL + "?" + urllib.parse.urlencode({
            "country": name, "event_date": since, "event_date_where": ">=",
            "fields": "event_date|event_type|fatalities", "limit": 5000, "page": page,
        })
        out = _http_json(url, headers={"Authorization": "Bearer " + token})
        if cutoff is None:
            try:
                cutoff = out["data_query_restrictions"]["date_recency"].get("date")
            except Exception:
                cutoff = None
        rows = out.get("data") or []
        if not rows:
            break
        total_rows += len(rows)
        for e in rows:
            d = e.get("event_date") or ""
            m = d[:7]
            if not m:
                continue
            if newest is None or d > newest:
                newest = d
            b = buckets.setdefault(m, {"month": m, "events": 0, "fatalities": 0})
            b["events"] += 1
            try:
                b["fatalities"] += int(e.get("fatalities") or 0)
            except (TypeError, ValueError):
                pass
            t = e.get("event_type") or "Other"
            types[t] = types.get(t, 0) + 1
        if len(rows) < 5000:
            break
        page += 1
    else:
        truncated = True
    kept = sorted(buckets.values(), key=lambda x: x["month"])[-months:]
    note = ""
    if cutoff:
        note = f"Your ACLED access tier serves data up to {cutoff} (~12-month embargo)."
    if truncated:
        note += " High event volume — earliest portion shown; recent months may be undercounted."
    return {
        "country_used": name,
        "months": kept,
        "by_type": sorted(types.items(), key=lambda x: -x[1]),
        "total_events": sum(b["events"] for b in kept),
        "total_fatalities": sum(b["fatalities"] for b in kept),
        "newest": newest,
        "cutoff": cutoff,
        "truncated": truncated,
        "note": note,
    }


# ─── NASA Earth Imagery proxy ────────────────────────────────────────────────

def earth_image(lat: float, lon: float, dim: str = "0.5"):
    """Landsat snapshot for one location, key server-side, bytes disk-cached.

    Returns (png_bytes, None) on success or (None, (error_dict, http_status)).
    """
    key = os.environ.get("NASA_API_KEY", "").strip()
    if not key:
        return None, ({"error": "no_nasa_key"}, 404)
    cpath = os.path.join(_CACHE_DIR, "img_" + hashlib.sha1(
        f"earth|{lat}|{lon}|{dim}".encode()).hexdigest()[:16] + ".png")
    if os.path.exists(cpath) and time.time() - os.path.getmtime(cpath) < 7 * 86400:
        try:
            return open(cpath, "rb").read(), None
        except Exception:
            pass
    url = "https://api.nasa.gov/planetary/earth/imagery?" + urllib.parse.urlencode(
        {"lon": lon, "lat": lat, "dim": dim, "api_key": key})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Exodus/1.0"})
        with urllib.request.urlopen(req, timeout=35) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read()
        if not ctype.startswith("image"):
            return None, ({"error": "nasa_no_image",
                           "detail": data[:200].decode("utf-8", "ignore")}, 502)
        os.makedirs(_CACHE_DIR, exist_ok=True)
        open(cpath, "wb").write(data)
        usage_bump("earth_image")
        return data, None
    except Exception as e:
        return None, ({"error": "nasa_unreachable", "detail": str(e)}, 502)


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

def crisis_detail(crisis: str, country: str, nocache: bool = False,
                  days: int = 60, place: Optional[str] = None,
                  want_roads: bool = True) -> dict:
    """Combined Tavily + roads + ACLED payload, matching EII's /api/detail shape.

    `place` and `days` are part of the cache key: changing the search location
    or window changes the results, so a stale cached response must not serve.
    Road access is a second Tavily search (doubles the credit cost of a call);
    pass want_roads=False to skip it when conserving credits.
    """
    days = max(1, min(365, int(days or 60)))
    place = (place or "").strip() or None
    tk = bool(os.environ.get("TAVILY_API_KEY", "").strip())
    ak = bool(os.environ.get("ACLED_EMAIL", "").strip()
              and os.environ.get("ACLED_PASSWORD", "").strip())
    ckey = (f"detail|{crisis}|{country}|{place or ''}|d{days}"
            f"|t{tk}|a{ak}|r{int(bool(want_roads))}")
    if not nocache:
        hit = _cache_get(ckey)
        if hit:
            hit["cached"] = True
            return hit

    resp = {"crisis": crisis, "country": country, "place": place,
            "cached": False, "days": days,
            "tavily": None, "acled": None, "roads": None, "errors": [], "keys": {}}
    resp["keys"]["tavily"] = tk
    if tk:
        try:
            resp["tavily"] = tavily_news(
                f"{crisis_query(country, crisis, place)} "
                "latest conflict, security and humanitarian developments",
                days=days)
            usage_bump("tavily")  # a real search credit was spent
        except Exception as e:
            resp["errors"].append(f"tavily: {e}")
        if want_roads:
            try:
                resp["roads"] = tavily_roads(country, crisis, days=days, place=place)
                usage_bump("tavily")   # second search = second credit
            except Exception as e:
                resp["errors"].append(f"roads: {e}")
    else:
        resp["errors"].append("no_tavily_key")

    resp["keys"]["acled"] = ak
    if ak:
        try:
            resp["acled"] = acled_timeline(country)
            usage_bump("acled")
        except Exception as e:
            resp["errors"].append(f"acled: {e}")
    else:
        resp["errors"].append("no_acled_key")

    if resp.get("tavily") or resp.get("acled") or resp.get("roads"):
        _cache_set(ckey, resp)  # only cache real data
    return resp
