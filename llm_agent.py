"""The reference agent with an opinion: it asks a language model.

`agent.py` bets that every price comes back to today's number. This file keeps
all of that (the snapshot, the quote before the trade, the dry run) and swaps
the one function that holds a view. For each open market the model reads the
floor's brief, the metric's definition and history, and the market's price and
range, and answers with where the number will be when the market settles and
how sure it is. A confident answer far from the price is traded; anything
else is left alone.

It is written against the OpenAI chat format because every provider speaks
it, and it defaults to logfare.ai, which serves frontier models for free
(no card, no email) in exchange for logging every prompt. So the whole bot
costs nothing to run. Point LLM_BASE_URL and LLM_MODEL anywhere else to pay
for privacy.

Run it:

    export LLM_API_KEY=...           # free: https://logfare.ai/register
    export TELARCHY_WORKSPACE=telarchy
    python3 llm_agent.py             # dry run: says what it would do, does nothing
    python3 llm_agent.py --live      # actually trades, needs TELARCHY_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

import agent
from telarchy import Telarchy, TelarchyError

# Below this the model's number is a guess, not a view, and a guess is not
# worth paying the spread for. The model rates itself, so this is honest only
# to the extent the model is; watch what its 0.6s are worth before trusting them.
MIN_CONFIDENCE = 0.6

# The brief is long (tens of KB on a busy floor). This keeps the front of it,
# which is the charter and the numbers, the part an opinion needs.
BRIEF_CHARS = 24_000

# Enough for a reasoning model to think AND answer. Too small and it spends
# the whole budget thinking and returns nothing, which counts as no answer.
# (logfare/auto routed to a model that thought for 3,900 tokens on a 7,700
# token brief; 4,000 cut it off mid-answer.)
MAX_TOKENS = 12_000


def base_url() -> str:
    return os.environ.get("LLM_BASE_URL") or "https://logfare.ai/v1"


def model() -> str:
    return os.environ.get("LLM_MODEL") or "logfare/auto"


def ask(prompt: str) -> str:
    """One chat completion, plain urllib. Returns the model's text, "" if it
    produced none (a reasoning model that ran out of budget does that)."""
    req = urllib.request.Request(
        base_url().rstrip("/") + "/chat/completions",
        data=json.dumps(
            {
                "model": model(),
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": MAX_TOKENS,
                "temperature": 0.2,
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as res:  # a thinking model on a long brief takes minutes
        body = json.loads(res.read())
    return (body["choices"][0]["message"].get("content") or "").strip()


def parse(text: str) -> dict | None:
    """The {"value", "confidence", "reason"} object, wherever the model put it.

    Models wrap JSON in prose and code fences however firmly they are told
    not to, so this takes the first {...} that parses and has a number in it.
    """
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("value"), (int, float)):
            return obj
    # Cut off mid-"reason"? A model that thinks for most of its budget does
    # that. The two numbers are what matter; take them if both are there.
    num = r'"%s"\s*:\s*(-?\d+(?:\.\d+)?)'
    value = re.search(num % "value", text)
    confidence = re.search(num % "confidence", text)
    if value and confidence:
        reason = re.search(r'"reason"\s*:\s*"([^"]*)', text)
        return {
            "value": float(value.group(1)),
            "confidence": float(confidence.group(1)),
            "reason": (reason.group(1) if reason else "") + " [cut off]",
        }
    return None


def prompt_for(brief: str, metric: dict, market: dict, value_now: float) -> str:
    history = metric.get("history") or metric.get("recent") or []
    return f"""You are a forecaster trading on a Telarchy floor. Below is the floor's brief (the owner's charter, the numbers, the open proposals), then one market on one of those numbers.

Answer with ONE JSON object and nothing else: {{"value": <number>, "confidence": <0 to 1>, "reason": "<one sentence>"}}.
"value" is your forecast of what the number will be at the instant the market settles. "confidence" is how sure you are that your value is closer to the truth than the market's current price; 0.5 means you have no edge over the market.

=== FLOOR BRIEF ===
{brief[:BRIEF_CHARS]}

=== THE METRIC ===
Name: {metric.get('name')}
Definition: {metric.get('definition') or '(none given)'}
Number today: {value_now:g}
{('Recent readings: ' + json.dumps(history)) if history else ''}

=== THE MARKET ===
Settles at: {str(market.get('resolvesOn'))[:10]}
Market price now: {market.get('prediction'):g}
Allowed range: {market.get('rangeMin'):g} to {market.get('rangeMax'):g}
"""


def run(client: Telarchy, live: bool) -> int:
    """One cycle: the reference loop with the model as its opinion."""
    # The brief once, not once per market: it is the same document each time
    # and it is the biggest thing in every prompt.
    brief = client.brief()
    if not isinstance(brief, str):
        brief = json.dumps(brief)

    def decide(market: dict, value_now: float, metric: dict) -> float | None:
        lo, hi = market["rangeMin"], market["rangeMax"]
        span = hi - lo
        if span <= 0:
            return None
        where = f"  {metric.get('name')} {str(market.get('resolvesOn'))[:10]}"
        try:
            text = ask(prompt_for(brief, metric, market, value_now))
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            print(f"{where}: model refused: {e}")
            return None
        view = parse(text)
        if view is None:
            print(f"{where}: no usable answer from the model")
            return None
        confidence = float(view.get("confidence") or 0)
        reason = str(view.get("reason") or "").strip()
        if confidence < MIN_CONFIDENCE:
            print(f"{where}: model says {view['value']:g} but is not confident ({confidence:g}): {reason}")
            return None
        target = max(lo, min(hi, float(view["value"])))
        if abs(target - market["prediction"]) < agent.THRESHOLD * span:
            return None  # agrees with the market, near enough; nothing to pay for
        print(f"{where}: model says {target:g} ({confidence:g}): {reason}")
        return target

    return agent.run(client, live=live, decide=decide)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true", help="actually trade (default: dry run)")
    ap.add_argument("--workspace", default=os.environ.get("TELARCHY_WORKSPACE"))
    args = ap.parse_args()

    if not args.workspace:
        print("Set TELARCHY_WORKSPACE (a floor's slug or id), or pass --workspace.", file=sys.stderr)
        print("Public floors: https://telarchy.com/api/marketplace/workspaces/public", file=sys.stderr)
        return 2

    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY. A free one takes a minute: https://logfare.ai/register", file=sys.stderr)
        print("(any OpenAI-compatible endpoint works: LLM_BASE_URL and LLM_MODEL)", file=sys.stderr)
        return 2

    key = os.environ.get("TELARCHY_KEY")
    if args.live and not key:
        print("--live needs TELARCHY_KEY. Reading works without one.", file=sys.stderr)
        return 2

    client = Telarchy(key=key, workspace=args.workspace)
    print(f"{'trading' if args.live else 'dry run'} on {args.workspace}, asking {model()} at {base_url()}")

    try:
        placed = run(client, live=args.live)
    except TelarchyError as e:
        print(f"failed: {e.code or e.status} {e}", file=sys.stderr)
        if e.doc_url:
            print(f"  {e.doc_url}", file=sys.stderr)
        return 1

    print(f"{placed} trade(s) {'placed' if args.live else 'would be placed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
