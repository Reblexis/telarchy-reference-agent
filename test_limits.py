"""Execution limits belong to the runner, regardless of forecasting strategy."""
import math
from unittest import mock
from urllib.parse import parse_qs, urlparse

import agent
from telarchy import Telarchy, TelarchyError, NotAuthorized
from test_agent import Base, SNAPSHOT, TRADES, READS, market, metric


class TestExecutionLimits(Base):
    def setUp(self):
        super().setUp()
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market(str(i), 90) for i in range(8)])]

    def test_THE_RULE_each_cycle_reserves_no_more_than_its_budget(self):
        for live in (False, True):
            with self.subTest(live=live):
                TRADES.clear()
                placed, _ = self.go(live=live, budget_per_trade=1, cycle_budget=2.5)
                quotes = [t["body"]["maxBudget"] for t in TRADES if t["dry"]]
                self.assertEqual(quotes, [1, 1, 0.5])
                self.assertEqual(placed, 3)
                self.assertEqual(sum(t["body"]["maxBudget"] for t in TRADES if not t["dry"]), 2.5 if live else 0)

    def test_THE_RULE_lost_responses_do_not_release_reserved_allowance(self):
        original = Telarchy.trade
        def timeout(client, mid, **kwargs):
            result = original(client, mid, **kwargs)
            if not kwargs.get("dry_run"):
                raise TelarchyError("response lost", status=0)
            return result
        with mock.patch.object(Telarchy, "trade", timeout):
            placed, _ = self.go(live=True, cycle_budget=2)
        self.assertEqual(placed, 0)
        self.assertEqual(len([t for t in TRADES if not t["dry"]]), 2)

    def test_maximum_not_cheap_quote_is_reserved(self):
        self.go(live=True, budget_per_trade=2, cycle_budget=3)
        # Stub fills at most 1 credit, but price can move before submission.
        self.assertEqual([t["body"]["maxBudget"] for t in TRADES if not t["dry"]], [2, 1])

    def test_zero_limits_do_not_even_call_the_strategy(self):
        for kwargs in ({"cycle_budget": 0}, {"budget_per_trade": 0}):
            decide = mock.Mock(return_value=20)
            self.assertEqual(self.go(live=True, decide=decide, **kwargs)[0], 0)
            decide.assert_not_called()
        self.assertEqual(TRADES, [])

    def test_invalid_limits_fail_before_network(self):
        for name in ("cycle_budget", "budget_per_trade"):
            for bad in (-1, math.nan, math.inf, True, "1"):
                with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                    self.go(live=True, **{name: bad})
        self.assertEqual(READS, [])
        self.assertEqual(TRADES, [])

    def test_custom_strategies_cannot_send_invalid_targets(self):
        for bad in (math.nan, math.inf, -math.inf, True, "20", {}, []):
            with self.subTest(bad=bad):
                self.assertEqual(self.go(live=True, decide=lambda *args: bad)[0], 0)
        self.assertEqual(TRADES, [])

    def test_custom_strategy_targets_are_clamped_by_executor(self):
        self.go(live=True, decide=lambda *args: 200, cycle_budget=1)
        self.assertEqual(TRADES[-1]["body"]["targetValue"], 100)

    def test_history_is_requested_for_every_strategy(self):
        self.go()
        self.assertEqual(parse_qs(urlparse(READS[0]).query), {"markets": ["1"], "trends": ["1"]})

    def test_denied_quotes_never_count_as_live_trades(self):
        with mock.patch.object(Telarchy, "trade", side_effect=NotAuthorized("denied", status=403)):
            placed, out = self.go(live=True)
        self.assertEqual(placed, 0)
        self.assertNotIn("would trade", out)

    def test_anonymous_hypothetical_trades_obey_the_cycle_budget(self):
        with mock.patch.object(Telarchy, "trade", side_effect=NotAuthorized("denied", status=403)):
            self.assertEqual(self.go(cycle_budget=2)[0], 2)

    def test_invalid_market_numbers_skip_without_calling_strategy(self):
        for key, bad in (("prediction", None), ("rangeMin", math.nan), ("rangeMax", math.inf)):
            with self.subTest(key=key):
                m = market("bad", 90)
                m[key] = bad
                SNAPSHOT["metrics"] = [metric("Revenue", 10, [m])]
                decide = mock.Mock(return_value=20)
                self.assertEqual(self.go(live=True, decide=decide)[0], 0)
                decide.assert_not_called()
        self.assertEqual(TRADES, [])
