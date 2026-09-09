"""A Telarchy trading agent, in one file.

It does one thing: find markets whose price disagrees with where the number
actually is today, and bet that they come back toward it.

    read one snapshot  ->  compare each market to its metric  ->  trade the gaps

That is the whole loop. Everything below is that, with the reasons written
down. Start here, replace `decide()` with your own opinion, keep the rest.

Run it:

    export TELARCHY_WORKSPACE=telarchy
    python3 agent.py                 # dry run: says what it would do, does nothing
    python3 agent.py --live          # actually trades, needs TELARCHY_KEY
"""

from __future__ import annotations

import argparse
import math
import os
import sys
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
    ap.add_argument("--live", action="store_true", help="actually trade (default: dry run)")
    ap.add_argument("--workspace", default=os.environ.get("TELARCHY_WORKSPACE"))
    ap.add_argument("--budget-per-trade", type=float, default=BUDGET)
    ap.add_argument("--cycle-budget", type=float, default=CYCLE_BUDGET)
    return ap


def main() -> int:
    ap = parser(__doc__.splitlines()[0])
    args = ap.parse_args()

    if not args.workspace:
        print("Set TELARCHY_WORKSPACE (a floor's slug or id), or pass --workspace.", file=sys.stderr)
        print("Public floors: https://telarchy.com/api/marketplace/workspaces/public", file=sys.stderr)
        return 2

    key = os.environ.get("TELARCHY_KEY")
    if args.live and not key:
        print("--live needs TELARCHY_KEY. Reading works without one.", file=sys.stderr)
        return 2

    client = Telarchy(key=key, workspace=args.workspace)
    print(f"{'trading' if args.live else 'dry run'} on {args.workspace}")

    try:
        placed = run(client, live=args.live, budget_per_trade=args.budget_per_trade, cycle_budget=args.cycle_budget)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    except TelarchyError as e:
        print(f"failed: {e.code or e.status} {e}", file=sys.stderr)
        if e.doc_url:
            print(f"  {e.doc_url}", file=sys.stderr)
        return 1

    print(f"{placed} trade(s) {'placed' if args.live else 'would be placed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
