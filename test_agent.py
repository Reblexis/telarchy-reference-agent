"""Tests for the reference agent, against a local stub. No network, nothing to install.

    python3 -m unittest discover

Two things are worth testing in an agent this small, and they are the two that
cost money if they are wrong: WHICH markets it decides to trade, and that a dry
run never places one.
"""

from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

import agent
from telarchy import Telarchy

TRADES: list[dict] = []
SNAPSHOT: dict = {}
READS: list[str] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            READS.append(self.path)
            return self._json(200, SNAPSHOT)
        return self._json(404, {"error": "Not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        TRADES.append({"body": body, "dry": bool(body.get("dryRun"))})
        cost = min(float(body.get("maxBudget", 1)), 1.0)
        out = {"shares": 2.0, "cost": cost, "consensus": body.get("targetValue", 0), "tradeId": "t1"}
        if body.get("dryRun"):
            out.update({"dryRun": True, "affordable": True, "shortfall": 0, "balance": 100})
        return self._json(200 if body.get("dryRun") else 201, out)


def metric(name, total, markets):
    return {"id": f"m-{name}", "name": name, "value": total, "total": total, "markets": markets}


def market(mid, prediction, lo=0, hi=100, resolves="2027-01-01T00:00:00Z"):
    """A market exactly as GET /api/status returns one TO A KEY HOLDER.

    Deliberately without `targetDate`. That field comes back to an anonymous
    caller and not to a key holder, and the catalog documents only the fields
    below, so a fixture carrying it would let the agent depend on something
    that vanishes the moment anybody uses it for real. It did, once.
    """
    return {
        "id": mid,
        "resolvesOn": resolves,
        "prediction": prediction,
        "probability": 0.5,
        "rangeMin": lo,
        "rangeMax": hi,
    }


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/api"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        TRADES.clear()
        SNAPSHOT.clear()
        READS.clear()

    def go(self, live=False, **kwargs):
        client = Telarchy(key="k", workspace="w", base_url=self.base)
        with redirect_stdout(io.StringIO()) as out:
            placed = agent.run(client, live=live, **kwargs)
        return placed, out.getvalue()


class TestWhatItTrades(Base):
    def test_a_market_far_from_the_number_is_traded(self):
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 90)])]
        placed, _ = self.go(live=True)
        self.assertEqual(placed, 1)
        real = [t for t in TRADES if not t["dry"]]
        self.assertEqual(real[0]["body"]["targetValue"], 10)

    def test_a_market_already_near_the_number_is_left_alone(self):
        # 12 vs 10 on a range of 100 is a 2% gap, under the 5% threshold.
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 12)])]
        placed, _ = self.go(live=True)
        self.assertEqual(placed, 0)
        self.assertEqual(TRADES, [])

    def test_a_metric_never_measured_is_skipped(self):
        # Nothing to disagree with, so there is no view to take.
        SNAPSHOT["metrics"] = [{"id": "x", "name": "New", "total": None, "markets": [market("m1", 90)]}]
        placed, _ = self.go(live=True)
        self.assertEqual(placed, 0)

    def test_the_target_never_leaves_the_market_range(self):
        # The number is above what this market can express; aim at its ceiling.
        SNAPSHOT["metrics"] = [metric("Revenue", 500, [market("m1", 10, lo=0, hi=100)])]
        self.go(live=True)
        real = [t for t in TRADES if not t["dry"]]
        self.assertEqual(real[0]["body"]["targetValue"], 100)

    def test_a_zero_width_range_is_skipped_rather_than_dividing_by_zero(self):
        SNAPSHOT["metrics"] = [metric("Flat", 5, [market("m1", 5, lo=7, hi=7)])]
        placed, _ = self.go(live=True)
        self.assertEqual(placed, 0)


class TestNoKey(Base):
    """The first run, with nothing set up.

    A dry run needs an identity, so with no key the quote is refused. That must
    still tell you WHICH markets it would trade and why, because that is the
    whole first-run experience: one command, a real floor, a real answer.
    """

    def test_THE_FIRST_RUN_it_still_reports_what_it_would_do(self):
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 90)])]

        original = Telarchy.trade

        def refused(self, market_id, **kw):
            from telarchy import NotAuthorized

            raise NotAuthorized("Forbidden", status=403, code="not_authorized", body={})

        Telarchy.trade = refused
        try:
            placed, out = self.go(live=False)
        finally:
            Telarchy.trade = original

        self.assertEqual(placed, 1)
        self.assertIn("market says 90", out)
        self.assertIn("would trade", out)

    def test_a_real_refusal_is_not_swallowed(self):
        # A closed market is a different thing from "you have no key", and
        # reporting both as "would trade" would be a lie.
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 90)])]

        original = Telarchy.trade

        def closed(self, market_id, **kw):
            from telarchy import MarketClosed

            raise MarketClosed("Market is closed", status=400, code="market_closed", body={})

        Telarchy.trade = closed
        try:
            placed, out = self.go(live=False)
        finally:
            Telarchy.trade = original

        self.assertEqual(placed, 0)
        self.assertIn("refused", out)


class TestDryRun(Base):
    def test_THE_FIELD_it_reports_the_instant_a_market_settles(self):
        # Named after the rule in the guide: read resolvesOn, never targetDate.
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 90, resolves="2027-03-04T00:00:00Z")])]
        _, out = self.go(live=False)
        self.assertIn("2027-03-04", out)

    def test_THE_RULE_a_dry_run_places_nothing(self):
        # The default. Running this file must never cost anyone credits.
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 90)])]
        placed, out = self.go(live=False)
        self.assertEqual(placed, 1, "it should still report what it would do")
        self.assertTrue(all(t["dry"] for t in TRADES), "no live trade may be sent")
        self.assertIn("market says 90", out)

    def test_it_asks_before_every_real_trade(self):
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 90)])]
        self.go(live=True)
        self.assertTrue(TRADES[0]["dry"], "the quote comes first")
        self.assertFalse(TRADES[1]["dry"])

    def test_it_does_not_trade_what_it_cannot_afford(self):
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 90)])]

        original = Telarchy.trade

        def poor(self, market_id, **kw):
            res = original(self, market_id, **kw)
            if kw.get("dry_run"):
                res.update({"affordable": False, "shortfall": 5.0})
            return res

        Telarchy.trade = poor
        try:
            placed, out = self.go(live=True)
        finally:
            Telarchy.trade = original
        self.assertEqual(placed, 0)
        self.assertIn("short by", out)
        self.assertEqual([t for t in TRADES if not t["dry"]], [])


if __name__ == "__main__":
    unittest.main()
