"""A Telarchy trading agent, in one file.

It does one thing: find markets whose price disagrees with where the number
actually is today, and bet that they come back toward it.

    read one snapshot  ->  compare each market to its metric  ->  trade the gaps

That is the whole loop. Everything below is that, with the reasons written
down. Start here, replace `decide()` with your own opinion, keep the rest.

Run it:

    python3 agent.py                 # preview: says what it would do, does nothing
    python3 agent.py --login         # paste your key once; it is checked and saved
    python3 agent.py --live          # actually trades
    python3 agent.py --live --every 30   # and again every 30 minutes
"""

from __future__ import annotations

import argparse
import getpass
import math
import os
import sys
import time
from decimal import Decimal

from telarchy import IdentityRequired, NotAuthorized, Telarchy, TelarchyError

# How far a price has to be from today's number before it is worth a trade,
# as a fraction of the market's own range. Below this the market and the
# number are close enough that the spread would eat the edge.
THRESHOLD = 0.05

# Credits per trade. Small on purpose: these books are thin, and on a thin book
# the price you pay is the average across the move you make. Size up only after
# you have watched what your own trades do to the price.
BUDGET = 1.0
CYCLE_BUDGET = 5.0


# Where nothing else is named: the public floor anyone may read.
WORKSPACE = "telarchy"

# `--login` saves the key here, beside this file and readable only by you, so
# a new terminal tomorrow still has it. TELARCHY_KEY, when set, wins.
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".telarchy-key")

# Where a person creates a bot, gets its key, and gives it credits.
AGENTS_URL = "https://telarchy.com/agents"


def finite_number(value) -> bool:
    """JSON booleans and numeric strings are not forecasts or allowances."""
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate_budget(value) -> None:
    if not finite_number(value) or value < 0:
        raise ValueError("budgets must be nonnegative finite numbers")


def decide(market: dict, value_now: float, metric: dict) -> float | None:
    """Where this market should be, or None to leave it alone.

    THE STRATEGY, and it is deliberately the simplest defensible one: a market
    is pricing where a number will BE on its resolution date, and the best
    evidence anyone has about that is where the number is now. So when a price
    has wandered far from today's reading, bet it comes back.

    Be honest about what this ignores: the market may be right and you may be
    wrong. A metric that is climbing every week SHOULD price above today's
    value, and this rule will bet against that climb and lose. It has no idea
    what `resolvesOn` is, no trend, no view on the contracts that are pending.

    That is the point. It is a floor to beat, not a strategy to run. Replace
    this function with something that reads the brief, the metric's history and
    the pending contracts, and you have a real participant. `llm_agent.py` is
    that replacement done the laziest way: it asks a language model.
    `metric` is the market's metric (name, definition, current value); this
    rule does not need it, an opinion does.
    """
    span = market["rangeMax"] - market["rangeMin"]
    if span <= 0:
        return None
    gap = market["prediction"] - value_now
    if abs(gap) < THRESHOLD * span:
        return None
    # Never aim outside the market's own range; the API refuses it and it is
    # not a view anyone can hold anyway.
    return max(market["rangeMin"], min(market["rangeMax"], value_now))


def run(client: Telarchy, live: bool, decide=decide, *,
        budget_per_trade: float = BUDGET, cycle_budget: float = CYCLE_BUDGET) -> int:
    """One cycle. Returns how many trades it placed, or would have.

    `decide` is the opinion; pass your own to keep everything else.
    """
    # One call for everything: every metric, its current value, and every open
    # market on it. Two round trips per market would be the obvious way to
    # write this and it would be an order of magnitude more requests.
    validate_budget(budget_per_trade)
    validate_budget(cycle_budget)
    if budget_per_trade == 0 or cycle_budget == 0:
        return 0
    remaining = Decimal(str(cycle_budget))
    snapshot = client.status(markets=True, trends=True)

    placed = 0
    for metric in snapshot["metrics"]:
        # `total` is the metric including anything a formula contributes, which
        # is the number a market actually settles on. `value` is only the part
        # someone typed in.
        value_now = metric.get("total")
        if not finite_number(value_now):
            continue  # never measured, so there is nothing to disagree with

        for market in metric.get("markets") or []:
            if remaining <= 0:
                return placed
            lo, hi, price = (market.get(k) for k in ("rangeMin", "rangeMax", "prediction"))
            if not all(finite_number(v) for v in (lo, hi, price)) or hi <= lo:
                print(f"  {metric['name']}: skipped invalid market numbers")
                continue
            target = decide(market, value_now, metric)
            if target is None:
                continue
            if not finite_number(target):
                print(f"  {metric['name']}: skipped invalid forecast")
                continue
            target = max(lo, min(hi, target))
            allowance = min(Decimal(str(budget_per_trade)), remaining)
            budget = float(allowance)

            # `resolvesOn` and never `targetDate`: the first is the exact
            # instant this settles, the second is the period it belongs to and
            # is not something you can order or compare. It is also the only
            # one of the two the catalog documents, and GET /api/status returns
            # `targetDate` to an anonymous caller but not to a key holder, so a
            # client that reads it works until the day you give it a key.
            when = str(market["resolvesOn"])[:10]
            where = (
                f"  {metric['name']} {when}: "
                f"market says {market['prediction']:g}, number is {value_now:g}"
            )

            # Ask before acting. A dry run costs nothing, needs no credits, and
            # returns the fill you would actually get rather than the one you
            # assumed: on a thin book those differ a lot.
            #
            # It does need an identity, so with no key at all you still see
            # WHICH markets this would trade and why, just not the fill. That
            # is the point of running it with no key first.
            quote = None
            try:
                quote = client.trade(
                    market["id"], target_value=target, max_budget=budget, dry_run=True
                )
            except (IdentityRequired, NotAuthorized) as e:
                if live:
                    print(f"{where} -> refused: {e}")
                    continue
                remaining -= allowance
                print(f"{where} -> would trade (quote needs a key)")
                placed += 1
                continue
            except TelarchyError as e:
                print(f"{where} -> refused: {e.code or e.status} {e}")
                continue

            print(
                f"{where} -> {quote['shares']:.3f} shares for {quote['cost']:.3f} cr, "
                f"price lands at {quote['consensus']:g}"
            )

            if not live:
                remaining -= allowance
                placed += 1
                continue

            if not quote["affordable"]:
                print(f"    skipped: short by {quote['shortfall']:.3f} credits")
                continue

            # Reserve before submitting: a lost response may hide a committed
            # trade. Reusing this allowance could exceed the cycle budget.
            remaining -= allowance
            try:
                # No automatic retry. A deliberate retry must reuse the same
                # persisted idempotency key AND body, not call trade() afresh.
                fill = client.trade(market["id"], target_value=target, max_budget=budget)
                print(f"    traded: {fill['shares']:.3f} shares for {fill['cost']:.3f} cr")
                placed += 1
            except TelarchyError as e:
                print(f"    refused: {e.code or e.status} {e}")

    return placed


def parser(description: str) -> argparse.ArgumentParser:
    """Both strategies expose the same workspace, dry-run, and credit limits."""
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--live", action="store_true", help="actually trade (default: preview only)")
    ap.add_argument("--login", action="store_true", help="paste your key once; it is checked and saved")
    ap.add_argument("--every", type=float, metavar="MINUTES", help="keep running, one cycle every MINUTES")
    ap.add_argument("--workspace", default=os.environ.get("TELARCHY_WORKSPACE") or WORKSPACE,
                    help=f"a floor's slug or id (default: {WORKSPACE})")
    ap.add_argument("--budget-per-trade", type=float, default=BUDGET)
    ap.add_argument("--cycle-budget", type=float, default=CYCLE_BUDGET)
    return ap


def me() -> str:
    """This program as the person just typed it, so every hint can be pasted."""
    exe = sys.executable or "python"
    try:
        rel = os.path.relpath(exe)
        exe = rel if not rel.startswith("..") else "python"
    except ValueError:  # another drive, on Windows
        exe = "python"
    return f"{exe} {sys.argv[0] if sys.argv and sys.argv[0] else 'agent.py'}"


def saved_key() -> str | None:
    """TELARCHY_KEY if set, else whatever `--login` saved, else None."""
    key = (os.environ.get("TELARCHY_KEY") or "").strip()
    if key:
        return key
    try:
        with open(KEY_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def refused(e: TelarchyError) -> None:
    print(f"Telarchy refused this key ({e}).", file=sys.stderr)
    print(f"Create or copy a key at {AGENTS_URL}, then run: {me()} --login", file=sys.stderr)


def login(make_client, workspace: str) -> int:
    """Ask for the key without echoing it, prove it works, then keep it."""
    try:
        key = getpass.getpass("Paste your Telarchy API key (it stays hidden), then press Enter: ").strip()
    except (EOFError, KeyboardInterrupt):
        key = ""
    if not key:
        print(f"No key entered, nothing saved. Keys come from {AGENTS_URL}", file=sys.stderr)
        return 2
    try:
        balance = make_client(key=key, workspace=workspace).balance()["balance"]
    except TelarchyError as e:
        if e.status in (401, 403):
            refused(e)
        else:
            print(f"Could not check the key: {e.code or e.status} {e}. Nothing saved.", file=sys.stderr)
        return 1
    # Created private rather than made private afterwards, so the key is never
    # readable by anyone else even for a moment.
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    os.chmod(KEY_FILE, 0o600)
    print(f"Connected. Balance: {balance:g} credits. Key saved to {KEY_FILE}")
    if balance <= 0:
        print(f"This bot has no credits yet. Its owner adds them at {AGENTS_URL}")
    print(f"Next: {me()}          (preview with real fills)")
    print(f"Then: {me()} --live   (actually trades)")
    return 0


def start(args, make_client):
    """Everything before the first cycle. Returns (client, None) or (None, exit code).

    Each way this can stop says what to do next, because the person reading
    it has usually never seen this program before.
    """
    if args.login:
        return None, login(make_client, args.workspace)
    if args.every is not None and not (finite_number(args.every) and args.every > 0):
        print("--every takes a number of minutes above zero.", file=sys.stderr)
        return None, 2

    key = saved_key()
    if args.live and not key:
        print(f"--live needs your key. Run: {me()} --login", file=sys.stderr)
        print(f"Keys come from {AGENTS_URL}. Previewing works without one.", file=sys.stderr)
        return None, 2

    client = make_client(key=key, workspace=args.workspace)
    if key:
        # One cheap read before anything else: a wrong key or an empty bot is
        # found here, with a sentence, instead of once per market below.
        try:
            balance = client.balance()["balance"]
        except TelarchyError as e:
            if e.status in (401, 403):
                refused(e)
            else:
                print(f"failed: {e.code or e.status} {e}", file=sys.stderr)
            return None, 1
        print(f"connected, balance {balance:g} credits")
        if args.live and balance <= 0:
            print(f"This bot has no credits, so it cannot trade. Its owner adds them at {AGENTS_URL}",
                  file=sys.stderr)
            return None, 1
    return client, None


def cycles(args, once) -> int:
    """Run `once` one time, or forever with `--every`. `once` returns trades placed."""
    while True:
        code = 0
        try:
            placed = once()
            print(f"{placed} trade(s) {'placed' if args.live else 'would be placed'}")
            if args.live and placed:
                print(f"See your bot and its trades: {AGENTS_URL}")
            elif not args.live and saved_key():
                print(f"Nothing was spent. To actually trade: {me()} --live")
            elif not args.live:
                print(f"To see real fills and trade, connect a key: {me()} --login  (keys: {AGENTS_URL})")
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        except TelarchyError as e:
            print(f"failed: {e.code or e.status} {e}", file=sys.stderr)
            if e.doc_url:
                print(f"  {e.doc_url}", file=sys.stderr)
            code = 1
        if args.every is None:
            return code
        # A floor that is down for a minute should not end a bot meant to run
        # all week, so a failed cycle waits like any other.
        print(f"next cycle in {args.every:g} min (Ctrl+C stops)")
        time.sleep(args.every * 60)


def main() -> int:
    args = parser(__doc__.splitlines()[0]).parse_args()
    client, code = start(args, lambda **kw: Telarchy(**kw))
    if client is None:
        return code
    print(f"{'trading' if args.live else 'preview (nothing is spent)'} on {args.workspace}")
    return cycles(args, lambda: run(client, live=args.live, budget_per_trade=args.budget_per_trade,
                                    cycle_budget=args.cycle_budget))


if __name__ == "__main__":
    raise SystemExit(main())
