"""FPL chatbot: an LLM + FPL tools. Run `python bot.py` for a terminal chat.

Two interchangeable back ends with the same interface:
- FPLBot     Claude (paid API). Used when ANTHROPIC_API_KEY is set.
- GeminiBot  Google Gemini, which has a free tier. Used when GEMINI_API_KEY is set.
Set LLM_PROVIDER=claude or LLM_PROVIDER=gemini to force one.

Import elsewhere (the web server does this):
    bot = make_bot()
    reply = bot.chat("Who should I captain?")
"""

import json
import os
import time
from datetime import date

import anthropic
import requests
from dotenv import load_dotenv

from tools import TOOLS, gemini_tools, run_tool

load_dotenv()

# Sonnet is the cost-sensible default; set FPL_BOT_MODEL=claude-fable-5-2 to try Fable.
MODEL = os.getenv("FPL_BOT_MODEL", "claude-sonnet-5-5")
# Flash-Lite is on Gemini's free tier; set GEMINI_MODEL=gemini-3.8-flash for stronger answers.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_TOOL_ROUNDS = 8

SYSTEM = f"""You are an assistant for Fantasy Premier League (FPL) managers. Today is {date.today():%d %B %Y}.

Rules:
- Every stat, price, fixture, injury and deadline you state must come from a tool result in this
  conversation. Never quote numbers from memory; your training data is out of date.
- If a question needs live data, call the tools first. Call several in one turn when helpful.
- Prices are in £m. FDR is 1 (easiest) to 5 (hardest).
- If a manager asks about "my team" and you don't have their team ID, ask for it (it's the number
  in the URL of their FPL Points page).
- Give a clear recommendation with the 2-3 reasons that matter, then the key numbers. Keep it short.
- Suggestions are heuristics, not guarantees. Say so briefly when recommending captains or transfers.
- Convert UTC deadlines to the user's timezone if they tell you it."""

CUT_OFF = "\n\n(Cut off: the answer hit the length limit. Try a narrower question.)"
NO_ANSWER = "Sorry, I couldn't answer that. Try rephrasing the question."
TOO_MANY_STEPS = "Sorry, that took too many steps. Try a narrower question."
NO_KEY = ("Chat needs an API key. Set GEMINI_API_KEY (free, from Google AI Studio) or "
          "ANTHROPIC_API_KEY, in the .env file locally or in your host's environment "
          "variables, then restart the server.")


class ChatError(Exception):
    """A chat failure whose message is safe to show to the user."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class FPLBot:
    """Chat on Claude."""

    def __init__(self, model: str = MODEL):
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.model = model
        self.messages: list = []

    def load_history(self, turns):
        """Start from earlier turns: a list of (role, text), role being 'user' or 'assistant'."""
        self.messages = [{"role": role, "content": text} for role, text in turns]

    def chat(self, user_text: str, on_tool=None) -> str:
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.messages.create(
                # Thinking counts against max_tokens, so leave room for it plus the answer.
                model=self.model, max_tokens=16000, system=SYSTEM,
                tools=TOOLS, messages=self.messages,
            )
            self.messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                text = "".join(b.text for b in resp.content if b.type == "text").strip()
                if resp.stop_reason == "max_tokens":
                    text += CUT_OFF
                return text or NO_ANSWER

            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    if on_tool:
                        on_tool(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": run_tool(block.name, block.input)})
            self.messages.append({"role": "user", "content": results})
        return TOO_MANY_STEPS

    def reset(self):
        self.messages = []


def _whole_numbers(value):
    """Gemini sends every number as a float; turn 5.0 back into 5 so IDs and counts work."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _whole_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_whole_numbers(v) for v in value]
    return value


class GeminiBot:
    """Chat on Google Gemini (free tier available). Plain HTTPS calls, no extra package."""

    def __init__(self, model: str = GEMINI_MODEL):
        self.model = model
        self.contents: list = []

    def load_history(self, turns):
        self.contents = [{"role": "model" if role == "assistant" else "user",
                          "parts": [{"text": text}]} for role, text in turns]

    def chat(self, user_text: str, on_tool=None) -> str:
        self.contents.append({"role": "user", "parts": [{"text": user_text}]})
        for _ in range(MAX_TOOL_ROUNDS):
            candidate = (self._generate().get("candidates") or [{}])[0]
            parts = (candidate.get("content") or {}).get("parts") or []
            if parts:
                # Send the model's turn back exactly as received: its parts carry the
                # thought signatures Gemini needs on the next request.
                self.contents.append({"role": "model", "parts": parts})

            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                if candidate.get("finishReason") == "MAX_TOKENS":
                    text += CUT_OFF
                return text or NO_ANSWER

            results = []
            for call in calls:
                args = _whole_numbers(call.get("args") or {})
                if on_tool:
                    on_tool(call["name"], args)
                response = {"name": call["name"],
                            "response": {"result": json.loads(run_tool(call["name"], args))}}
                if call.get("id"):
                    response["id"] = call["id"]
                results.append({"functionResponse": response})
            self.contents.append({"role": "user", "parts": results})
        return TOO_MANY_STEPS

    def reset(self):
        self.contents = []

    def _generate(self) -> dict:
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            raise ChatError(NO_KEY, 503)
        body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
                "contents": self.contents,
                "tools": [{"functionDeclarations": gemini_tools()}]}
        for attempt in (1, 2):
            try:
                r = requests.post(f"{GEMINI_URL}/{self.model}:generateContent",
                                  headers={"x-goog-api-key": key}, json=body, timeout=120)
            except requests.Timeout:
                raise ChatError("Gemini took too long to answer. Try again in a moment.", 504)
            except requests.RequestException:
                raise ChatError("Couldn't reach Gemini. Check your internet connection.")
            if r.status_code in (500, 503) and attempt == 1:
                time.sleep(1.5)  # brief overloads are common on the free tier: retry once
                continue
            break
        if r.status_code == 200:
            return r.json()
        raise self._error(r)

    def _error(self, r) -> ChatError:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        message, status = err.get("message", ""), err.get("status", "")
        if r.status_code == 429:
            return ChatError("The free Gemini limit has been reached for now. Wait a minute and "
                             "try again. If it keeps failing, today's free quota is used up and "
                             "resets tomorrow. Explore still works.", 429)
        if r.status_code in (401, 403) or "API key" in message:
            return ChatError("Gemini rejected the API key. Check GEMINI_API_KEY, then restart "
                             "the server.", 503)
        if r.status_code == 404:
            return ChatError(f"Gemini has no model called '{self.model}'. Check GEMINI_MODEL.", 503)
        if r.status_code >= 500:
            return ChatError("Gemini is busy right now. Try again in a moment.")
        return ChatError(f"Gemini rejected the request ({status or r.status_code}): {message[:200]}")


def provider() -> str:
    """Which back end runs the chat: LLM_PROVIDER if set, else whichever key is present."""
    forced = os.getenv("LLM_PROVIDER", "").strip().lower()
    if forced in ("claude", "gemini"):
        return forced
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return "claude"
    return "gemini" if os.getenv("GEMINI_API_KEY") else "claude"


def chat_configured() -> bool:
    if provider() == "gemini":
        return bool(os.getenv("GEMINI_API_KEY"))
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def active_model() -> str:
    return GEMINI_MODEL if provider() == "gemini" else MODEL


def make_bot():
    return GeminiBot() if provider() == "gemini" else FPLBot()


def main():
    bot = make_bot()
    print(f"FPL bot ({bot.model}). Type 'reset' to clear, 'quit' to exit.\n")
    while True:
        try:
            q = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q.lower() in ("quit", "exit"):
            break
        if q.lower() == "reset":
            bot.reset()
            print("(cleared)\n")
            continue
        try:
            reply = bot.chat(q, on_tool=lambda n, a: print(f"  [tool] {n} {a}"))
        except ChatError as e:
            print(f"\n(!) {e}\n")
            continue
        print(f"\nBot: {reply}\n")


if __name__ == "__main__":
    main()
