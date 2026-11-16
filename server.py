"""Web front end for the FPL bot: a small FastAPI server plus one static page.

Locally: run `python server.py`, then open http://127.0.0.1:8000
On Vercel: this file is the entrypoint (Vercel looks for `app` in server.py).

- Explore endpoints call fpl_data directly: no AI model, so they cost nothing.
- Ask goes through the bot in bot.py (Gemini's free tier, or Claude, plus the FPL tools).

The server keeps no conversation state. The page sends the earlier turns with each
question, so chat works on serverless hosts where requests can land on different instances.
"""

import hmac
import os
from pathlib import Path
from typing import List, Literal, Optional

import anthropic
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import fpl_data as fpl
# Importing bot also loads .env. make_bot() picks Claude or Gemini from the keys that are set.
from bot import NO_KEY, ChatError, active_model, chat_configured, make_bot, provider

PORT = int(os.getenv("PORT", "8000"))
# Optional: when set, Ask requires this password. Stray spaces from pasting are ignored.
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
MAX_HISTORY_TURNS = 12  # earlier turns replayed to the model with each question
MAX_TURN_CHARS = 8000
INDEX = Path(__file__).parent / "public" / "index.html"  # Vercel also serves public/ from its CDN
POSITION = "^(GKP|DEF|MID|FWD)$"

app = FastAPI(title="FPL Assistant", docs_url=None, redoc_url=None)


# ---------- explore: plain data, no AI model ----------

def _explore(fn, **kwargs):
    """Run a data function, turning failures into clean HTTP errors."""
    try:
        result = fn(**kwargs)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise HTTPException(404, "Not found. Check the ID.")
        raise HTTPException(502, "The FPL API returned an error. Try again shortly.")
    except requests.RequestException:
        raise HTTPException(502, "Couldn't reach the FPL API. Try again shortly.")
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/meta")
def meta():
    configured = chat_configured()
    return {"gameweek": _explore(fpl.gameweek_info), "teams": _explore(fpl.team_names),
            "chat_configured": configured, "chat_locked": bool(APP_PASSWORD),
            "provider": provider() if configured else None,
            "model": active_model() if configured else None}


@app.get("/api/captain")
def captain(team_id: Optional[int] = Query(None, ge=1), top_n: int = Query(10, ge=1, le=20)):
    return _explore(fpl.suggest_captain, team_id=team_id, top_n=top_n)


@app.get("/api/transfers")
def transfers(position: str = Query("MID", pattern=POSITION),
              max_price: float = Query(8.0, gt=0, le=20),
              n_gws: int = Query(5, ge=1, le=10),
              exclude_team_id: Optional[int] = Query(None, ge=1),
              top_n: int = Query(12, ge=1, le=25)):
    return _explore(fpl.suggest_transfers, position=position, max_price=max_price,
                    n_gws=n_gws, exclude_team_id=exclude_team_id, top_n=top_n)


@app.get("/api/fixtures")
def fixtures(n_gws: int = Query(5, ge=1, le=10)):
    return _explore(fpl.fixture_grid, n_gws=n_gws)


@app.get("/api/differentials")
def differentials(max_ownership: float = Query(5.0, ge=0, le=100),
                  position: Optional[str] = Query(None, pattern=POSITION),
                  max_price: Optional[float] = Query(None, gt=0, le=20),
                  top_n: int = Query(12, ge=1, le=25)):
    return _explore(fpl.find_differentials, max_ownership=max_ownership, position=position,
                    max_price=max_price, top_n=top_n)


@app.get("/api/injuries")
def injuries(team: Optional[str] = Query(None, max_length=40)):
    return _explore(fpl.injury_news, team=team)


@app.get("/api/team/{team_id}")
def team(team_id: int):
    return _explore(fpl.get_my_team, team_id=team_id)


@app.get("/api/league/{league_id}")
def league(league_id: int):
    return _explore(fpl.mini_league, league_id=league_id)


# ---------- ask: AI model + tools ----------

def _check_password(supplied: Optional[str]) -> None:
    typed = (supplied or "").strip()
    if APP_PASSWORD and not hmac.compare_digest(typed.encode(), APP_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password.")


def _chat_error(e: Exception) -> HTTPException:
    if isinstance(e, ChatError):  # already carries a message that is safe to show
        return HTTPException(e.status, str(e))
    if isinstance(e, anthropic.AuthenticationError):  # 503, not 401: 401 means the app password
        return HTTPException(503, "Claude rejected the API key. Check ANTHROPIC_API_KEY, then "
                                  "restart the server.")
    if isinstance(e, anthropic.RateLimitError):
        return HTTPException(429, "Claude is rate-limiting requests. Wait a moment and try again.")
    if isinstance(e, anthropic.APIConnectionError):
        return HTTPException(502, "Couldn't reach Claude. Check your internet connection.")
    if isinstance(e, anthropic.APIStatusError):
        return HTTPException(502, f"Claude returned an error ({e.status_code}). Try again.")
    if not chat_configured():  # the Claude SDK raises a bare TypeError with no credentials
        return HTTPException(503, NO_KEY)
    return HTTPException(500, f"Something went wrong ({type(e).__name__}). Try again.")


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=100_000)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    team_id: Optional[int] = Field(None, ge=1)
    history: List[Turn] = Field(default_factory=list, max_length=40)
    password: Optional[str] = Field(None, max_length=200)


class UnlockIn(BaseModel):
    password: str = Field(max_length=200)


def _replay(history: List[Turn]) -> list:
    """Earlier turns as (role, text): the most recent few, starting on a user turn."""
    turns = history[-MAX_HISTORY_TURNS:]
    while turns and turns[0].role != "user":
        turns = turns[1:]
    return [(t.role, t.text[:MAX_TURN_CHARS]) for t in turns]


@app.post("/api/unlock")
def unlock(body: UnlockIn):
    """Lets the page check a password without spending a model call."""
    _check_password(body.password)
    return {"ok": True}


@app.post("/api/chat")
def chat(body: ChatIn):
    _check_password(body.password)
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Type a question first.")
    if body.team_id:
        text += f"\n\n(My FPL team ID is {body.team_id}.)"
    used = []
    try:
        bot = make_bot()
        bot.load_history(_replay(body.history))
        reply = bot.chat(text, on_tool=lambda name, args: used.append(name))
    except Exception as e:
        raise _chat_error(e)
    return {"reply": reply, "tools": used}


if __name__ == "__main__":
    import threading
    import webbrowser

    import uvicorn

    url = f"http://127.0.0.1:{PORT}"
    print(f"FPL Assistant is running at {url}  (Ctrl+C to stop)")
    if chat_configured():
        print(f"Ask is using {provider()} ({active_model()}).")
    else:
        print("Note: no API key found. Explore works; Ask needs GEMINI_API_KEY (free) or "
              "ANTHROPIC_API_KEY in .env.")
    threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
