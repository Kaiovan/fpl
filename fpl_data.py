"""FPL data layer: fetches from the public Fantasy Premier League API, caches it,
and exposes plain-Python functions the chatbot uses as tools.

All prices are returned in £m (FPL stores them in tenths, e.g. 105 -> 10.5).
"""

import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
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


# ---------- mini-league rivals ----------

CHIP_NAMES = {"wildcard": "Wildcard", "freehit": "Free Hit", "bboost": "Bench Boost",
              "3xc": "Triple Captain", "manager": "Assistant Manager"}
LEAGUE_MANAGERS = 50  # one page of standings; each manager costs two FPL API calls


def _chips_left(used: list) -> Optional[list]:
    """Chips still available for the next deadline. FPL splits the season into windows
    (e.g. GW1-19 and GW20-38) and lists them in bootstrap-static["chips"]."""
    windows = _bootstrap().get("chips")
    if not windows:
        return None
    nxt = _next_gw()
    left = []
    for c in windows:
        if not c["start_event"] <= nxt <= c["stop_event"]:
            continue
        n_used = sum(1 for u in used if u["name"] == c["name"]
                     and c["start_event"] <= u["event"] <= c["stop_event"])
        if n_used < c.get("number", 1):
            left.append(c["name"])
    return left


def _manager_gw(team_id: int, gw: int) -> dict:
    """One manager's picks for a gameweek, plus the chips they have left."""
    picks = _get(f"entry/{team_id}/event/{gw}/picks/", ttl=300)
    hist = _get(f"entry/{team_id}/history/", ttl=600)
    eh = picks.get("entry_history", {})
    return {
        "picks": [{"id": pk["element"], "mult": pk["multiplier"], "c": pk["is_captain"],
                   "vc": pk["is_vice_captain"], "xi": pk["position"] <= 11}
                  for pk in picks["picks"]],
        "active_chip": picks.get("active_chip"),
        "chips_left": _chips_left(hist.get("chips", [])),
        "gw_points": eh.get("points"),
        "total": eh.get("total_points"),
        "bench_points": eh.get("points_on_bench"),
        "hits": eh.get("event_transfers_cost"),
    }


def _fetch_managers(team_ids: list, gw: int) -> dict:
    """{team_id: _manager_gw(...) or None}, fetched in parallel. A manager whose data
    can't be read (for example they joined after the deadline) maps to None."""
    _bootstrap()  # warm the shared cache before the threads start

    def one(tid):
        try:
            return tid, _manager_gw(tid, gw)
        except (requests.RequestException, KeyError, ValueError):
            return tid, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, team_ids))


def _role(pk: Optional[dict]) -> str:
    if not pk:
        return "not owned"
    if pk["c"]:
        return "triple captain" if pk["mult"] == 3 else "captain"
    if not pk["xi"]:
        return "bench (boosted)" if pk["mult"] else "bench"
    return "starting"


def _entry_name(team_id: int) -> dict:
    e = _get(f"entry/{team_id}/", ttl=300)
    return {"team": e.get("name"),
            "manager": f"{e.get('player_first_name', '')} {e.get('player_last_name', '')}".strip(),
            "total": e.get("summary_overall_points")}


def league_radar(league_id: int, team_id: Optional[int] = None) -> dict:
    """Everything the rival radar needs: every manager's picks and chips, plus how the
    league owns each player compared with you. Used by the web page."""
    gw = _current_gw()
    data = _get(f"leagues-classic/{league_id}/standings/", ttl=300)
    rows = data["standings"]["results"][:LEAGUE_MANAGERS]
    ids = [r["entry"] for r in rows]
    outside = bool(team_id) and team_id not in ids  # you, but not in the rows we have
    fetched = _fetch_managers(ids + ([team_id] if outside else []), gw)

    managers = []
    for r in rows:
        m = fetched.get(r["entry"])
        if m:
            managers.append({**m, "team_id": r["entry"], "team": r["entry_name"],
                             "manager": r["player_name"], "rank": r["rank"],
                             "total": r["total"], "gw_points": r["event_total"]})
    me = next((m for m in managers if m["team_id"] == team_id), None)
    if outside and fetched.get(team_id):
        me = {**fetched[team_id], **_entry_name(team_id), "team_id": team_id, "rank": None}
    rivals = [m for m in managers if m["team_id"] != team_id]
    if not rivals:
        return {"error": f"No squads to compare in this league for GW{gw} yet."}

    n = len(rivals)
    stats: dict = {}
    for m in rivals:
        for pk in m["picks"]:
            s = stats.setdefault(pk["id"], [0, 0, 0, 0])  # owned, started, captained, multipliers
            s[0] += 1
            s[1] += pk["mult"] > 0
            s[2] += pk["c"]
            s[3] += pk["mult"]
    mine = {pk["id"]: pk for pk in me["picks"]} if me else {}

    ownership = []
    for pid in set(stats) | set(mine):
        owned, started, captained, mults = stats.get(pid, [0, 0, 0, 0])
        eo = 100 * mults / n
        my_mult = mine[pid]["mult"] if pid in mine else 0
        ownership.append({
            "id": pid,
            "owned_pct": round(100 * owned / n, 1),
            "started_pct": round(100 * started / n, 1),
            "captained_pct": round(100 * captained / n, 1),
            "rival_eo": round(eo, 1),          # effective ownership: captains count double
            "my_role": _role(mine.get(pid)) if me else None,
            "swing": round(100 * my_mult - eo, 1) if me else None,  # + your edge, - their edge
        })
    ownership.sort(key=lambda o: -o["rival_eo"])

    players = _players()
    return {
        "league": data["league"]["name"],
        "league_id": league_id,
        "gameweek": gw,
        "rivals_compared": n,
        "more_managers": data["standings"]["has_next"] or len(data["standings"]["results"]) > len(rows),
        "skipped": sum(1 for r in rows if not fetched.get(r["entry"])),
        "me": me,
        "me_in_top": bool(me) and not outside,
        "managers": managers,
        "ownership": ownership,
        "players": {pid: _summary(players[pid], 3) for pid in {o["id"] for o in ownership}},
        "chip_names": CHIP_NAMES,
    }


def mini_league_rivals(league_id: int, team_id: Optional[int] = None) -> dict:
    """Compact rival radar for the chat model: threats, your edges, captain split, chips."""
    d = league_radar(league_id, team_id)
    if "error" in d:
        return d
    P = d["players"]

    def brief(o):
        p = P[o["id"]]
        out = {"name": p["full_name"], "team": p["team"], "position": p["position"],
               "rival_effective_ownership_pct": o["rival_eo"], "rivals_captaining_pct": o["captained_pct"],
               "form": p["form"], "next_3_fixtures": p["next_3_fixtures"]}
        if o["my_role"] is not None:
            out["your_role"] = o["my_role"]
        return out

    own = d["ownership"]
    chip_counts = {}
    for m in d["managers"]:
        for c in m["chips_left"] or []:
            chip_counts[CHIP_NAMES.get(c, c)] = chip_counts.get(CHIP_NAMES.get(c, c), 0) + 1
    out = {
        "league": d["league"], "gameweek_of_squads": d["gameweek"], "rivals_compared": d["rivals_compared"],
        "note": "Effective ownership counts a captain twice (three times for a triple captain) and "
                "a benched player as zero. Over 100% means rivals captain him heavily.",
        "captain_split": [{"name": P[o["id"]]["name"], "pct_of_rivals": o["captained_pct"]}
                          for o in sorted(own, key=lambda o: -o["captained_pct"]) if o["captained_pct"]][:6],
        "managers_with_chip_left": chip_counts,
        "standings_top": [{"rank": m["rank"], "team": m["team"], "team_id": m["team_id"],
                           "total": m["total"], "chips_left": [CHIP_NAMES.get(c, c) for c in m["chips_left"] or []]}
                          for m in d["managers"][:10]],
    }
    me = d["me"]
    if me:
        above = [m for m in d["managers"] if m["rank"] and (me["rank"] is None or m["rank"] < me["rank"])]
        out["you"] = {"rank": me["rank"], "total": me["total"],
                      "gap_to_leader": d["managers"][0]["total"] - me["total"],
                      "next_above": ({"team": above[-1]["team"], "team_id": above[-1]["team_id"],
                                      "points_ahead": above[-1]["total"] - me["total"]} if above else None)}
        out["threats_you_dont_cover"] = [brief(o) for o in sorted(own, key=lambda o: o["swing"]) if o["swing"] < -5][:8]
        out["your_edges"] = [brief(o) for o in sorted(own, key=lambda o: -o["swing"]) if o["swing"] > 5][:8]
    else:
        out["most_owned"] = [brief(o) for o in own[:10]]
    return out


def rival_compare(team_id: int, rival_team_id: int) -> dict:
    """Head to head: where two squads differ. Shared players score the same for both,
    so only these differences can change the points gap."""
    gw = _current_gw()
    got = _fetch_managers([team_id, rival_team_id], gw)
    for tid in (team_id, rival_team_id):
        if not got.get(tid):
            return {"error": f"Couldn't load team {tid} for GW{gw}. Check the team ID."}
    you, them = got[team_id], got[rival_team_id]
    yn, tn = _entry_name(team_id), _entry_name(rival_team_id)
    a = {pk["id"]: pk for pk in you["picks"]}
    b = {pk["id"]: pk for pk in them["picks"]}
    players = _players()

    def item(pid):
        p = _summary(players[pid], 3)
        return {"name": p["full_name"], "team": p["team"], "position": p["position"], "form": p["form"],
                "status": p["status"], "next_3_fixtures": p["next_3_fixtures"],
                "your_role": _role(a.get(pid)), "their_role": _role(b.get(pid))}

    def summary(m, names):
        cap = next((pk["id"] for pk in m["picks"] if pk["c"]), None)
        return {**names, "captain": players[cap]["web_name"] if cap else None,
                "active_chip": m["active_chip"],
                "chips_left": [CHIP_NAMES.get(c, c) for c in m["chips_left"] or []]}

    return {
        "gameweek_of_squads": gw,
        "you": summary(you, yn), "rival": summary(them, tn),
        "rival_points_ahead": (tn["total"] or 0) - (yn["total"] or 0),
        "only_rival_has": [item(p) for p in b if p not in a],
        "only_you_have": [item(p) for p in a if p not in b],
        "shared_different_role": [item(p) for p in a if p in b and a[p]["mult"] != b[p]["mult"]],
        "shared_same_role_count": sum(1 for p in a if p in b and a[p]["mult"] == b[p]["mult"]),
    }


# ---------- squad planner ----------

def planner_players(n_gws: int = 3) -> dict:
    """Every player in a compact form, plus each team's next fixtures, for the planner page."""
    teams = _teams()
    return {
        "gameweek": _next_gw(),
        "teams": {tid: {"name": t["name"], "short": t["short_name"],
                        "fixtures": _team_fixtures(tid, n_gws)} for tid, t in teams.items()},
        "players": [{
            "id": p["id"], "name": p["web_name"],
            "full_name": f"{p['first_name']} {p['second_name']}",
            "team": p["team"], "pos": POSITIONS[p["element_type"]], "price": _price(p),
            "form": _f(p["form"]), "ppg": _f(p["points_per_game"]), "total": p["total_points"],
            "owned": _f(p["selected_by_percent"]), "status": STATUS.get(p["status"], p["status"]),
            "chance": p.get("chance_of_playing_next_round"), "news": p.get("news") or "",
        } for p in _bootstrap()["elements"]],
    }


def _sell_price(now: int, bought: int) -> int:
    """FPL selling price in tenths: you keep half of any rise, rounded down to £0.1m."""
    return now if now <= bought else bought + (now - bought) // 2


def _free_transfers(history: dict, started: int, upto: int) -> int:
    """Estimate the free transfers available for the gameweek after `upto`.
    One new free transfer arrives each deadline, up to five. Since 2024/25 a Wildcard or
    Free Hit keeps the saved ones, so those weeks change nothing."""
    chips = {c["event"]: c["name"] for c in history.get("chips", [])}
    ft = 1
    for row in sorted(history.get("current", []), key=lambda r: r["event"]):
        g = row["event"]
        if g <= started or g > upto:
            continue
        if chips.get(g) in ("wildcard", "freehit"):
            continue
        ft = min(5, max(ft - row.get("event_transfers", 0), 0) + 1)
    return ft


def planner_team(team_id: int) -> dict:
    """A manager's squad as the starting point for the planner: picks, bank, selling
    prices, chips left and an estimate of free transfers."""
    entry = _get(f"entry/{team_id}/", ttl=300)
    gw = entry.get("current_event")
    if not gw:
        return {"error": "This team has no squad yet. Its first deadline hasn't passed."}
    picks = _get(f"entry/{team_id}/event/{gw}/picks/", ttl=300)
    squad_gw = gw
    if picks.get("active_chip") == "freehit" and gw > 1:
        # A Free Hit squad lasts one week; the next deadline starts from the week before.
        squad_gw = gw - 1
        picks = _get(f"entry/{team_id}/event/{squad_gw}/picks/", ttl=300)
    history = _get(f"entry/{team_id}/history/", ttl=600)
    transfers = _get(f"entry/{team_id}/transfers/", ttl=300)
    started = entry.get("started_event") or 1
    players = _players()

    bought = {}  # latest price paid for each player
    for t in sorted(transfers, key=lambda t: t.get("time") or ""):
        bought[t["element_in"]] = t["element_in_cost"]

    def start_price(pid):
        """Price when the manager's first squad was picked, for players never transferred in."""
        try:
            for r in _get(f"element-summary/{pid}/")["history"]:
                if r["round"] >= started:
                    return r["value"]
        except (requests.RequestException, KeyError):
            pass
        return players[pid]["now_cost"]

    ids = [pk["element"] for pk in picks["picks"]]
    missing = [pid for pid in ids if pid not in bought]
    with ThreadPoolExecutor(max_workers=8) as pool:
        bought.update(dict(zip(missing, pool.map(start_price, missing))))

    squad = []
    for pk in picks["picks"]:
        pid = pk["element"]
        now = players[pid]["now_cost"]
        squad.append({"id": pid, "slot": pk["position"], "captain": pk["is_captain"],
                      "vice": pk["is_vice_captain"], "purchase": bought[pid] / 10,
                      "sell": _sell_price(now, bought[pid]) / 10})
    bank = picks.get("entry_history", {}).get("bank", 0) / 10
    return {
        "team_name": entry.get("name"),
        "manager": f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip(),
        "gameweek": _next_gw(),
        "squad_from_gw": squad_gw,
        "bank": bank,
        "squad": sorted(squad, key=lambda s: s["slot"]),
        "chips_left": _chips_left(history.get("chips", [])),
        "free_transfers": _free_transfers(history, started, gw),
    }


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
