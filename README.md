# FPL Assistant (Fantasy Premier League API + an AI chat)

Explore live FPL data and ask questions about your team in plain English. The AI model
decides which tools to call, pulls live data from the FPL API, and answers.

It can run completely free: the data and the Explore pages cost nothing, and the chat can
use Google Gemini's free tier. Claude is supported too, as a paid option.

## Setup

```bash
cd fpl
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

Create a file called `.env` in the same folder with **one** of these keys. The chat uses
whichever key is present (Claude if both are; set `LLM_PROVIDER=gemini` or `claude` to choose).

```
# Free: Google Gemini. Get a key at https://aistudio.google.com/apikey
GEMINI_API_KEY=...
# optional - default is gemini-3.5-flash-lite
# GEMINI_MODEL=gemini-3.8-flash

# Paid: Claude. Get a key at https://console.anthropic.com
# ANTHROPIC_API_KEY=sk-ant-...
# optional - default is claude-sonnet-5-5
# FPL_BOT_MODEL=claude-haiku-5-5
```

About Gemini's free tier: it has per-minute and per-day limits (the chat says so when they
are hit, and Explore keeps working), and Google may use free-tier questions and answers to
improve its products. The chat panel tells visitors this.

## Run

### Web app

```bash
python server.py
```

Opens http://127.0.0.1:8000 with two sides on one page:

- **Explore**: captain picks, transfer targets, fixture difficulty, differentials, injuries,
  your squad and your mini-league. These read the FPL API directly, so they are free
  and work without an API key.
- **Planner**: your squad on a pitch for the next gameweek. Drag players between the XI and
  the bench (or tap a player, then Substitute), set captain and vice, sell players and pick
  replacements, and try a chip. It tracks the bank, transfers, points hits, the 3-per-club
  limit and formation rules. Selling prices follow FPL's rule (you keep half of any rise)
  and come from the manager's transfer history; free transfers are estimated and can be
  changed. Without a team ID it is a £100m squad builder. Plans are saved in the browser
  until the deadline and never touch the real FPL team.
- **Mini-league** has three views:
  - *Rival radar*: which players your rivals own and captain that you don't (threats),
    which ones only you lean on (your edge), the league's captain split, and the chips
    every manager has left.
  - *Head to head*: you against one rival, listing only the players you don't share,
    since those are the only ones that can change the gap.
  - *Standings*: the table, with a Compare button on each row.

  The radar reads the top 50 managers of the league (about 100 FPL API calls on a cold
  server, then cached for 5 minutes).
- **Ask**: chat with the bot. Needs one of the keys above: free on Gemini's free tier, or
  a small cost per question on Claude.

Enter your team ID in the top bar to rank your own squad. The "Ask" buttons in Explore
send a ready-made question about what you're looking at to the chat.

### Terminal chat

```bash
python bot.py
```

Try:
- `My team ID is 1234567. Who should I captain?`
- `Compare Saka, Palmer and Haaland over the next 4 gameweeks`
- `Best midfielders under £7.5m with good fixtures?`
- `Which teams have the easiest run for the next 6 GWs?`
- `Any Arsenal injuries?`
- `Differentials under 3% owned?`
- `When's the deadline? Any double gameweeks?`
- `My team ID is 1234567 and my league is 314. How do I catch the leader?`

Your team ID is the number in the URL on your FPL **Points** page
(`fantasy.premierleague.com/entry/1234567/event/12`).

## Files

| File | What it does |
|---|---|
| `fpl_data.py` | Fetches and caches FPL API data; one function per tool |
| `tools.py` | Tool schemas the model sees + `run_tool()` dispatcher |
| `bot.py` | `GeminiBot` and `FPLBot` (Claude) chat loops, `make_bot()`, terminal chat |
| `server.py` | Web server: JSON endpoints for Explore, plus `/api/chat` for Ask |
| `public/index.html` | The whole web page (HTML, CSS and JavaScript in one file, no build step) |

## Tools

`get_player`, `compare_players`, `fixture_difficulty`, `get_my_team`, `suggest_captain`,
`suggest_transfers`, `injury_news`, `find_differentials`, `gameweek_info`, `mini_league`,
`mini_league_rivals`, `rival_compare`

## Adding a tool

1. Write a function in `fpl_data.py` that returns a dict.
2. Add its schema to `TOOLS` in `tools.py` with the **same name** as the function.

## Notes

- Captain/transfer scores are simple heuristics (form, points-per-game, fixture difficulty).
  Tweak `_captain_score` and `score` in `fpl_data.py` to taste.
- The FPL API is public but unofficial; it can be slow or down around deadlines.
- Run locally, the web app listens on `127.0.0.1` only, so it is private to your computer.
- To use the bot from another app: `from bot import make_bot`, then `make_bot().chat(text)`.

## Hosting on Vercel

1. Push this repo to GitHub.
2. On vercel.com choose **Add New > Project** and import the repo. Vercel finds the FastAPI
   app in `server.py` and serves `public/` itself; leave the build settings as they are.
3. Add environment variables (in the import screen, or later under
   **Settings > Environment Variables**, then redeploy):

   | Name | Value |
   |---|---|
   | `GEMINI_API_KEY` | Free option. Your Gemini key; Ask then costs nothing. |
   | `ANTHROPIC_API_KEY` | Paid option, instead of the Gemini key. Your Claude key. |
   | `APP_PASSWORD` | Optional. If set, Ask asks visitors for this password. |

4. Deploy. Every later push to `main` redeploys.

Anyone with the URL can use the site, and Explore is always free.

- With the Gemini key, Ask is free too, but all visitors share your key's free daily limit.
  Set `APP_PASSWORD` if you want to keep that limit for yourself and friends.
- With the Claude key, every Ask question is billed to you. Set `APP_PASSWORD` unless you
  mean the chat to be public, and set a spend limit on the key in the Claude Console.

The server keeps no chat history: the page sends the last few turns with each question,
which is what lets it run on serverless hosts.
