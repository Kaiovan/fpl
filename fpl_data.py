"""FPL data layer: fetches from the public Fantasy Premier League API, caches it,
and exposes plain-Python functions the chatbot uses as tools.

All prices are returned in £m (FPL stores them in tenths, e.g. 105 -> 10.5).
"""

import time
import unicodedata
from typing import Optional

import requests

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (FPL-Chatbot)"}
POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
POS_LOOKUP = {"GKP": 1, "GK": 1, "DEF": 2, "MID": 3, "FWD": 4, "FW": 4}
STATUS = {"a": "available", "d": "doubtful", "i": "injured", "s": "suspended",
          "u": "unavailable", "n": "not in squad"}

_cache: dict = {}
CACHE_SECONDS = 3 * 60 * 60  # bootstrap/fixtures change slowly; refresh every 3h


def _get(path: str, ttl: int = CACHE_SECONDS):
    now = time.time()
    if path in _cache and now - _cache[path][0] < ttl:
        return _cache[path][1]
    r = requests.get(f"{BASE}/{path}", headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    _cache[path] = (now, data)
    return data


# ---------- helpers ----------

def _bootstrap():
    return _get("bootstrap-static/")


def _fixtures():
    return _get("fixtures/?future=1")


def _teams():
    return {t["id"]: t for t in _bootstrap()["teams"]}


def _players():
    return {p["id"]: p for p in _bootstrap()["elements"]}


def _next_gw() -> int:
    events = _bootstrap()["events"]
    for e in events:
        if e.get("is_next"):
            return e["id"]
    # season over or data odd: fall back to first unfinished
    for e in events:
        if not e.get("finished"):
            return e["id"]
    return events[-1]["id"]


def _current_gw() -> int:
    for e in _bootstrap()["events"]:
        if e.get("is_current"):
            return e["id"]
    return max(_next_gw() - 1, 1)


_SPECIAL = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss",
                          "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i"})


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.translate(_SPECIAL))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def _price(p) -> float:
    return p["now_cost"] / 10


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _team_fixtures(team_id: int, n_gws: int):
    """Fixtures for a team over the next n gameweeks (handles blanks & doubles)."""
    start = _next_gw()
    end = start + n_gws - 1
    teams = _teams()
    out = []
    for fx in _fixtures():
        gw = fx.get("event")
        if gw is None or not (start <= gw <= end):
            continue
        if fx["team_h"] == team_id:
            out.append({"gw": gw, "opponent": teams[fx["team_a"]]["short_name"],
                        "home": True, "fdr": fx["team_h_difficulty"]})
        elif fx["team_a"] == team_id:
            out.append({"gw": gw, "opponent": teams[fx["team_h"]]["short_name"],
                        "home": False, "fdr": fx["team_a_difficulty"]})
    return sorted(out, key=lambda x: x["gw"])


def _fixture_string(fxs):
    return ", ".join(f"GW{f['gw']} {f['opponent']}({'H' if f['home'] else 'A'}) FDR{f['fdr']}"
                     for f in fxs) or "no fixtures (blank)"


def _summary(p, n_gws: int = 5) -> dict:
    teams = _teams()
    fxs = _team_fixtures(p["team"], n_gws)
    return {
        "id": p["id"],
        "name": p["web_name"],
        "full_name": f"{p['first_name']} {p['second_name']}",
        "team": teams[p["team"]]["name"],
        "position": POSITIONS[p["element_type"]],
        "price_m": _price(p),
        "total_points": p["total_points"],
        "form": _f(p["form"]),
        "points_per_game": _f(p["points_per_game"]),
        "selected_by_pct": _f(p["selected_by_percent"]),
        "minutes": p["minutes"],
        "goals": p["goals_scored"],
        "assists": p["assists"],
        "clean_sheets": p["clean_sheets"],
        "xG": _f(p.get("expected_goals")),
        "xA": _f(p.get("expected_assists")),
        "status": STATUS.get(p["status"], p["status"]),
        "chance_of_playing_next_gw": p.get("chance_of_playing_next_round"),
        "news": p.get("news") or "",
        f"next_{n_gws}_fixtures": _fixture_string(fxs),
        "avg_fdr": round(sum(f["fdr"] for f in fxs) / len(fxs), 2) if fxs else None,
        "fixture_count": len(fxs),
    }


def _find(name: str):
    """Return list of matching players, best matches first."""
    q = _norm(name)
    exact, partial = [], []
    for p in _bootstrap()["elements"]:
        names = [_norm(p["web_name"]), _norm(p["second_name"]),
                 _norm(f"{p['first_name']} {p['second_name']}")]
        if q in names:
            exact.append(p)
        elif any(q in n for n in names):
            partial.append(p)
    key = lambda p: -p["total_points"]
    return sorted(exact, key=key) + sorted(partial, key=key)


def _captain_score(p) -> float:
    """Simple heuristic: recent form blended with season PPG, scaled by
    next-GW fixture ease and number of fixtures (doubles count twice)."""
    if p["status"] in ("i", "s", "u", "n"):
        return 0.0
    chance = p.get("chance_of_playing_next_round")
    availability = 1.0 if chance is None else chance / 100
    fxs = _team_fixtures(p["team"], 1)
    ease = sum((6 - f["fdr"]) / 3 for f in fxs)  # FDR2 -> 1.33, FDR5 -> 0.33
    base = 0.6 * _f(p["form"]) + 0.4 * _f(p["points_per_game"])
    return round(base * ease * availability, 2)


# ---------- tool functions ----------

def get_player(name: str, n_gws: int = 5) -> dict:
    matches = _find(name)
    if not matches:
        return {"error": f"No player found matching '{name}'."}
    result = {"player": _summary(matches[0], n_gws)}
    if len(matches) > 1:
        result["other_matches"] = [f"{p['web_name']} ({_teams()[p['team']]['short_name']})"
                                   for p in matches[1:6]]
    return result


def compare_players(names: list, n_gws: int = 5) -> dict:
    out, missing = [], []
    for n in names:
        m = _find(n)
        (out.append(_summary(m[0], n_gws)) if m else missing.append(n))
    return {"players": out, "not_found": missing}


def fixture_difficulty(n_gws: int = 5) -> dict:
    rows = []
    for tid, t in _teams().items():
        fxs = _team_fixtures(tid, n_gws)
        avg = round(sum(f["fdr"] for f in fxs) / len(fxs), 2) if fxs else None
        rows.append({"team": t["name"], "fixtures": len(fxs), "avg_fdr": avg,
                     "run": _fixture_string(fxs)})
    rows.sort(key=lambda r: (r["avg_fdr"] is None, r["avg_fdr"] or 0, -r["fixtures"]))
    return {"from_gw": _next_gw(), "n_gws": n_gws,
            "teams_easiest_first": rows}


def get_my_team(team_id: int) -> dict:
    entry = _get(f"entry/{team_id}/", ttl=300)
    gw = entry.get("current_event") or _current_gw()
    picks = _get(f"entry/{team_id}/event/{gw}/picks/", ttl=300)
    players = _players()
    squad = []
    for pk in picks["picks"]:
        p = players[pk["element"]]
        s = _summary(p, 3)
        s.update({"slot": pk["position"], "starting": pk["position"] <= 11,
                  "captain": pk["is_captain"], "vice_captain": pk["is_vice_captain"]})
        squad.append(s)
    hist = picks.get("entry_history", {})
    return {
        "team_name": entry.get("name"),
        "manager": f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip(),
        "overall_points": entry.get("summary_overall_points"),
        "overall_rank": entry.get("summary_overall_rank"),
        "picks_from_gw": gw,
        "bank_m": hist.get("bank", 0) / 10,
        "squad_value_m": hist.get("value", 0) / 10,
        "active_chip": picks.get("active_chip"),
        "squad": squad,
    }


def suggest_captain(team_id: Optional[int] = None, top_n: int = 5) -> dict:
    players = _players()
    if team_id:
        gw = _get(f"entry/{team_id}/", ttl=300).get("current_event") or _current_gw()
        picks = _get(f"entry/{team_id}/event/{gw}/picks/", ttl=300)["picks"]
        pool = [players[pk["element"]] for pk in picks if pk["position"] <= 11]
    else:
        pool = [p for p in players.values() if p["minutes"] > 0]
    ranked = sorted(pool, key=_captain_score, reverse=True)[:top_n]
    return {
        "gameweek": _next_gw(),
        "method": "0.6*form + 0.4*points_per_game, scaled by next-GW fixture ease, "
                  "fixture count and chance of playing. A heuristic, not a prediction.",
        "ranking": [{**_summary(p, 1), "captain_score": _captain_score(p)} for p in ranked],
    }


def suggest_transfers(position: str, max_price: float, n_gws: int = 5,
                      exclude_team_id: Optional[int] = None, top_n: int = 8) -> dict:
    pos = POS_LOOKUP.get(position.upper())
    if not pos:
        return {"error": "position must be GKP, DEF, MID or FWD"}
    owned = set()
    if exclude_team_id:
        gw = _get(f"entry/{exclude_team_id}/", ttl=300).get("current_event") or _current_gw()
        owned = {pk["element"] for pk in
                 _get(f"entry/{exclude_team_id}/event/{gw}/picks/", ttl=300)["picks"]}

    def score(p):
        fxs = _team_fixtures(p["team"], n_gws)
        ease = sum((6 - f["fdr"]) for f in fxs) / max(n_gws, 1)
        return (0.6 * _f(p["form"]) + 0.4 * _f(p["points_per_game"])) * ease

    pool = [p for p in _bootstrap()["elements"]
            if p["element_type"] == pos and _price(p) <= max_price
            and p["status"] == "a" and p["minutes"] > 0 and p["id"] not in owned]
    ranked = sorted(pool, key=score, reverse=True)[:top_n]
    return {"position": POSITIONS[pos], "max_price_m": max_price, "n_gws": n_gws,
            "note": "Ranked by form/PPG blended with fixture ease over the window. "
                    "Selling price in the real game may differ from current price.",
            "candidates": [{**_summary(p, n_gws), "score": round(score(p), 2)} for p in ranked]}


def injury_news(team: Optional[str] = None) -> dict:
    teams = _teams()
    out = []
    for p in _bootstrap()["elements"]:
        if p["status"] == "a" or not p.get("news"):
            continue
        tname = teams[p["team"]]["name"]
        if team and _norm(team) not in (_norm(tname), _norm(teams[p["team"]]["short_name"])):
            continue
        out.append({"name": p["web_name"], "team": tname, "status": STATUS.get(p["status"]),
                    "chance_of_playing_next_gw": p.get("chance_of_playing_next_round"),
                    "news": p["news"], "selected_by_pct": _f(p["selected_by_percent"])})
    out.sort(key=lambda x: -x["selected_by_pct"])
    return {"count": len(out), "players": out[:40]}


def find_differentials(max_ownership: float = 5.0, position: Optional[str] = None,
                       max_price: Optional[float] = None, top_n: int = 10) -> dict:
    pos = POS_LOOKUP.get(position.upper()) if position else None
    pool = [p for p in _bootstrap()["elements"]
            if _f(p["selected_by_percent"]) <= max_ownership and p["status"] == "a"
            and p["minutes"] >= 90
            and (pos is None or p["element_type"] == pos)
            and (max_price is None or _price(p) <= max_price)]
    pool.sort(key=lambda p: _f(p["form"]), reverse=True)
    return {"max_ownership_pct": max_ownership,
            "players": [_summary(p, 3) for p in pool[:top_n]]}


def gameweek_info() -> dict:
    events = {e["id"]: e for e in _bootstrap()["events"]}
    nxt = _next_gw()
    e = events[nxt]
    upcoming = [x for x in _fixtures() if x.get("event") == nxt]
    team_counts: dict = {}
    for fx in upcoming:
        for t in (fx["team_h"], fx["team_a"]):
            team_counts[t] = team_counts.get(t, 0) + 1
    teams = _teams()
    return {
        "next_gameweek": nxt,
        "deadline_utc": e["deadline_time"],
        "fixtures": len(upcoming),
        "double_gw_teams": [teams[t]["name"] for t, c in team_counts.items() if c > 1],
        "blank_gw_teams": [t["name"] for tid, t in teams.items() if tid not in team_counts],
    }


def mini_league(league_id: int) -> dict:
    data = _get(f"leagues-classic/{league_id}/standings/", ttl=300)
    rows = data["standings"]["results"][:50]
    return {"league": data["league"]["name"],
            "standings": [{"rank": r["rank"], "team": r["entry_name"],
                           "manager": r["player_name"], "total": r["total"],
                           "gw_points": r["event_total"], "team_id": r["entry"]}
                          for r in rows]}


# ---------- web UI helpers (not exposed to Claude as tools) ----------

def fixture_grid(n_gws: int = 5) -> dict:
    """Per-gameweek fixtures for every team, easiest run first."""
    start = _next_gw()
    last = min(start + n_gws - 1, _bootstrap()["events"][-1]["id"])
    rows = []
    for tid, t in _teams().items():
        fxs = _team_fixtures(tid, n_gws)
        rows.append({"team": t["name"], "short": t["short_name"], "fixtures": fxs,
                     "avg_fdr": round(sum(f["fdr"] for f in fxs) / len(fxs), 2) if fxs else None})
    rows.sort(key=lambda r: (r["avg_fdr"] is None, r["avg_fdr"] or 0, -len(r["fixtures"])))
    return {"gameweeks": list(range(start, last + 1)), "teams": rows}


def team_names() -> list:
    return sorted(t["name"] for t in _teams().values())
