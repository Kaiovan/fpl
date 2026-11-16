# FPL Chatbot (Claude + Fantasy Premier League API)

Ask questions about your FPL team in plain English. Claude decides which tools to call,
pulls live data from the FPL API, and answers.

## Setup

```bash
cd fpl-bot
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

Create a file called `.env` in the same folder:

```
ANTHROPIC_API_KEY=sk-ant-...
# optional — default is claude-sonnet-5-5
# FPL_BOT_MODEL=claude-fable-5-2
```

Get an API key at https://console.anthropic.com.

## Run

```bash
python bot.py
```

Try:
- `My team ID is 1234567. Who should I captain?`
- `Compare Salah, Saka and Palmer over the next 4 gameweeks`
- `Best midfielders under £7.5m with good fixtures?`
- `Which teams have the easiest run for the next 6 GWs?`
- `Any Arsenal injuries?`
- `Differentials under 3% owned?`
- `When's the deadline? Any double gameweeks?`

Your team ID is the number in the URL on your FPL **Points** page
(`fantasy.premierleague.com/entry/1234567/event/12`).

## Files

| File | What it does |
|---|---|
| `fpl_data.py` | Fetches and caches FPL API data; one function per tool |
| `tools.py` | Tool schemas Claude sees + `run_tool()` dispatcher |
| `bot.py` | `FPLBot` class (tool-use loop) + terminal chat |

## Tools

`get_player`, `compare_players`, `fixture_difficulty`, `get_my_team`, `suggest_captain`,
`suggest_transfers`, `injury_news`, `find_differentials`, `gameweek_info`, `mini_league`

## Adding a tool

1. Write a function in `fpl_data.py` that returns a dict.
2. Add its schema to `TOOLS` in `tools.py` with the **same name** as the function.

## Notes

- Captain/transfer scores are simple heuristics (form, points-per-game, fixture difficulty).
  Tweak `_captain_score` and `score` in `fpl_data.py` to taste.
- The FPL API is public but unofficial; it can be slow or down around deadlines.
- To put it on a web page or chat app, import `FPLBot` and call `bot.chat(text)`.
