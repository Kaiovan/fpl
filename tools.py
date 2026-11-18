"""Tool schemas for Claude + a dispatcher that runs the matching fpl_data function."""

import json
import fpl_data as fpl

POS_ENUM = {"type": "string", "enum": ["GKP", "DEF", "MID", "FWD"]}

TOOLS = [
    {
        "name": "get_player",
        "description": "Look up one Premier League player's FPL stats: price, points, form, "
                       "xG/xA, ownership, injury status/news, and upcoming fixtures with FDR. "
                       "Use for any question about a single player.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Player name, e.g. 'Salah' or 'Bukayo Saka'"},
            "n_gws": {"type": "integer", "description": "How many upcoming gameweeks of fixtures", "default": 5},
        }, "required": ["name"]},
    },
    {
        "name": "compare_players",
        "description": "Compare 2-5 players side by side (stats + upcoming fixtures).",
        "input_schema": {"type": "object", "properties": {
            "names": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5},
            "n_gws": {"type": "integer", "default": 5},
        }, "required": ["names"]},
    },
    {
        "name": "fixture_difficulty",
        "description": "Rank all 20 teams by average fixture difficulty (FDR) over the next N "
                       "gameweeks, easiest first. Shows blanks/doubles via fixture counts.",
        "input_schema": {"type": "object", "properties": {
            "n_gws": {"type": "integer", "default": 5},
        }},
    },
    {
        "name": "get_my_team",
        "description": "Fetch a manager's FPL squad by team ID: players, captain, bank, squad value, "
                       "rank. The team ID is the number in the URL on the FPL 'Points' page.",
        "input_schema": {"type": "object", "properties": {
            "team_id": {"type": "integer"},
        }, "required": ["team_id"]},
    },
    {
        "name": "suggest_captain",
        "description": "Rank captain options for the next gameweek. If team_id is given, ranks "
                       "that manager's starting XI; otherwise ranks all players.",
        "input_schema": {"type": "object", "properties": {
            "team_id": {"type": "integer"},
            "top_n": {"type": "integer", "default": 5},
        }},
    },
    {
        "name": "suggest_transfers",
        "description": "Find the best available players for a position under a price cap, "
                       "ranked by form and fixtures. Pass exclude_team_id to skip players the "
                       "manager already owns.",
        "input_schema": {"type": "object", "properties": {
            "position": POS_ENUM,
            "max_price": {"type": "number", "description": "Max price in £m, e.g. 7.5"},
            "n_gws": {"type": "integer", "default": 5},
            "exclude_team_id": {"type": "integer"},
            "top_n": {"type": "integer", "default": 8},
        }, "required": ["position", "max_price"]},
    },
    {
        "name": "injury_news",
        "description": "List injured, doubtful or suspended players with their news, most-owned "
                       "first. Optionally filter by team name.",
        "input_schema": {"type": "object", "properties": {
            "team": {"type": "string", "description": "Team name or short name, e.g. 'Arsenal' or 'ARS'"},
        }},
    },
    {
        "name": "find_differentials",
        "description": "Find in-form players with low ownership (differentials).",
        "input_schema": {"type": "object", "properties": {
            "max_ownership": {"type": "number", "default": 5.0, "description": "Max % selected by"},
            "position": POS_ENUM,
            "max_price": {"type": "number"},
            "top_n": {"type": "integer", "default": 10},
        }},
    },
    {
        "name": "gameweek_info",
        "description": "Next gameweek number, deadline (UTC), and which teams have a double or "
                       "blank gameweek. Use for deadline and chip-timing questions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "mini_league",
        "description": "Get standings for a classic mini-league by league ID (top 50).",
        "input_schema": {"type": "object", "properties": {
            "league_id": {"type": "integer"},
        }, "required": ["league_id"]},
    },
    {
        "name": "mini_league_rivals",
        "description": "Rival radar for a classic mini-league (top 50 managers): how rivals own and "
                       "captain each player (effective ownership), the captain split, which chips "
                       "rivals have left and, with team_id, the players that threaten that manager "
                       "and the ones that give them an edge. Use for 'how do I climb my league', "
                       "'who should I cover', 'what are my rivals doing'.",
        "input_schema": {"type": "object", "properties": {
            "league_id": {"type": "integer"},
            "team_id": {"type": "integer", "description": "The manager's own team ID, if known"},
        }, "required": ["league_id"]},
    },
    {
        "name": "rival_compare",
        "description": "Head to head between two FPL teams: points gap, captains, chips left, and "
                       "the players only one of them owns or uses differently. Shared players score "
                       "the same for both, so only these differences change the gap. Use for "
                       "'how do I catch X' or 'compare me with my rival'.",
        "input_schema": {"type": "object", "properties": {
            "team_id": {"type": "integer", "description": "The manager's own team ID"},
            "rival_team_id": {"type": "integer"},
        }, "required": ["team_id", "rival_team_id"]},
    },
]

_FUNCS = {t["name"]: getattr(fpl, t["name"]) for t in TOOLS}


def _without_defaults(schema):
    """Gemini accepts a subset of JSON Schema that has no "default" keyword."""
    if isinstance(schema, dict):
        return {k: _without_defaults(v) for k, v in schema.items() if k != "default"}
    if isinstance(schema, list):
        return [_without_defaults(v) for v in schema]
    return schema


def gemini_tools() -> list:
    """The same tools in Gemini's functionDeclarations format."""
    out = []
    for t in TOOLS:
        decl = {"name": t["name"], "description": t["description"]}
        if t["input_schema"].get("properties"):  # a tool with no inputs declares no parameters
            decl["parameters"] = _without_defaults(t["input_schema"])
        out.append(decl)
    return out


def run_tool(name: str, args: dict) -> str:
    """Run a tool and return a JSON string. Errors are returned, not raised,
    so Claude can tell the user what went wrong."""
    try:
        result = _FUNCS[name](**args)
    except KeyError as e:
        result = {"error": f"Not found: {e}. Check the ID or name."}
    except Exception as e:  # network errors, bad IDs, etc.
        result = {"error": f"{type(e).__name__}: {e}"}
    return json.dumps(result, ensure_ascii=False)
