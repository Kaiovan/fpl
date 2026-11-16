"""FPL chatbot: Claude + FPL tools. Run `python bot.py` for a terminal chat.

Import FPLBot elsewhere (e.g. a Streamlit or Telegram front end):
    bot = FPLBot()
    reply = bot.chat("Who should I captain?")
"""

import os
from datetime import date

import anthropic
from dotenv import load_dotenv

from tools import TOOLS, run_tool

load_dotenv()

# Sonnet is the cost-sensible default; set FPL_BOT_MODEL=claude-fable-5-2 to try Fable.
MODEL = os.getenv("FPL_BOT_MODEL", "claude-sonnet-5-5")
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


class FPLBot:
    def __init__(self, model: str = MODEL):
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.model = model
        self.messages: list = []

    def chat(self, user_text: str, on_tool=None) -> str:
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.messages.create(
                model=self.model, max_tokens=2000, system=SYSTEM,
                tools=TOOLS, messages=self.messages,
            )
            self.messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                return "".join(b.text for b in resp.content if b.type == "text").strip()

            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    if on_tool:
                        on_tool(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": run_tool(block.name, block.input)})
            self.messages.append({"role": "user", "content": results})
        return "Sorry, that took too many steps. Try a narrower question."

    def reset(self):
        self.messages = []


def main():
    bot = FPLBot()
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
        reply = bot.chat(q, on_tool=lambda n, a: print(f"  [tool] {n} {a}"))
        print(f"\nBot: {reply}\n")


if __name__ == "__main__":
    main()
