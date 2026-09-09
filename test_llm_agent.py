"""Tests for the model-driven agent, against two local stubs: a Telarchy floor
and an OpenAI-style chat endpoint. No network, nothing to install.

    python3 -m unittest discover

What is worth testing here is the seam: what the model is asked, what is done
with its answer, and that a bad answer costs nothing.
"""

from __future__ import annotations

import io
import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import llm_agent
from telarchy import Telarchy
from test_agent import Handler as FloorHandler, SNAPSHOT, TRADES, market, metric

BRIEF = "# Acme\nThe owner's charter: ship the thing.\n"
ASKED: list[dict] = []
ANSWERS: list = []  # each: a str (the model's content), an int (HTTP status), or None (empty content)


class FloorWithBrief(FloorHandler):
    def do_GET(self):
        if "/context" in self.path:
            raw = BRIEF.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        return super().do_GET()


class LLMHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        ASKED.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        answer = ANSWERS.pop(0) if ANSWERS else '{"value": 10, "confidence": 0.9, "reason": "default"}'
        if isinstance(answer, int):
            raw = json.dumps({"error": {"message": "boom"}}).encode()
            self.send_response(answer)
        else:
            msg = {"role": "assistant", "content": answer if answer is not None else "", "reasoning_content": "thinking..."}
            raw = json.dumps({"choices": [{"message": msg, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.floor = HTTPServer(("127.0.0.1", 0), FloorWithBrief)
        cls.llm = HTTPServer(("127.0.0.1", 0), LLMHandler)
        for s in (cls.floor, cls.llm):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.floor.server_address[1]}/api"
        cls.llm_url = f"http://127.0.0.1:{cls.llm.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.floor.shutdown()
        cls.llm.shutdown()
        cls.floor.server_close()
        cls.llm.server_close()

    def setUp(self):
        TRADES.clear()
        SNAPSHOT.clear()
        ASKED.clear()
        ANSWERS.clear()
        self.env = mock.patch.dict(
            os.environ,
            {"LLM_API_KEY": "sk-test", "LLM_BASE_URL": self.llm_url, "LLM_MODEL": "stub/model"},
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def go(self, live=False, **kwargs):
        client = Telarchy(key="k", workspace="w", base_url=self.base)
        with redirect_stdout(io.StringIO()) as out:
            placed = llm_agent.run(client, live=live, **kwargs)
        return placed, out.getvalue()

    def one_market(self, price=50, total=10, lo=0, hi=100):
        m = metric("Revenue", total, [market("m1", price, lo=lo, hi=hi, resolves="2027-03-04T00:00:00Z")])
        m["definition"] = "Money in, net of refunds."
        SNAPSHOT["metrics"] = [m]


class TestWhatItAsks(Base):
    def test_the_model_is_asked_once_per_market_with_the_key_and_model_from_the_environment(self):
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 50), market("m2", 60)])]
        self.go()
        self.assertEqual(len(ASKED), 2)
        self.assertEqual(ASKED[0]["auth"], "Bearer sk-test")
        self.assertEqual(ASKED[0]["body"]["model"], "stub/model")
        self.assertTrue(ASKED[0]["path"].endswith("/chat/completions"))

    def test_the_prompt_carries_the_brief_the_metric_and_the_market(self):
        self.one_market(price=50, total=10)
        self.go()
        prompt = "\n".join(m["content"] for m in ASKED[0]["body"]["messages"])
        self.assertIn("ship the thing", prompt)  # the floor's brief
        self.assertIn("Revenue", prompt)
        self.assertIn("Money in, net of refunds.", prompt)  # the metric's definition
        self.assertIn("10", prompt)  # today's number
        self.assertIn("50", prompt)  # the market's price
        self.assertIn("2027-03-04", prompt)  # when it settles
        self.assertIn("0", prompt) and self.assertIn("100", prompt)  # its range

    def test_the_brief_is_fetched_once_not_per_market(self):
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 50), market("m2", 60)])]
        original = Telarchy.brief
        calls = []

        def counted(self, *a, **kw):
            calls.append(1)
            return original(self, *a, **kw)

        Telarchy.brief = counted
        try:
            self.go()
        finally:
            Telarchy.brief = original
        self.assertEqual(len(calls), 1)


class TestWhatItDoesWithTheAnswer(Base):
    def test_a_confident_answer_far_from_the_price_is_traded_at_that_value(self):
        self.one_market(price=50)
        ANSWERS.append('{"value": 20, "confidence": 0.8, "reason": "the charter says so"}')
        placed, out = self.go(live=True)
        self.assertEqual(placed, 1)
        real = [t for t in TRADES if not t["dry"]]
        self.assertEqual(real[0]["body"]["targetValue"], 20)
        self.assertIn("the charter says so", out)  # the reason is printed next to the trade

    def test_an_answer_near_the_price_is_left_alone(self):
        # 52 vs 50 on a range of 100 is under the 5% threshold: no edge to pay for.
        self.one_market(price=50)
        ANSWERS.append('{"value": 52, "confidence": 0.9, "reason": "meh"}')
        placed, _ = self.go(live=True)
        self.assertEqual(placed, 0)
        self.assertEqual(TRADES, [])

    def test_THE_RULE_a_hesitant_model_does_not_trade(self):
        # Named after the rule: below MIN_CONFIDENCE the model's number is a guess, not a view.
        self.one_market(price=50)
        ANSWERS.append('{"value": 20, "confidence": 0.2, "reason": "no idea really"}')
        placed, out = self.go(live=True)
        self.assertEqual(placed, 0)
        self.assertEqual(TRADES, [])
        self.assertIn("not confident", out)

    def test_the_target_never_leaves_the_market_range(self):
        self.one_market(price=50, lo=0, hi=100)
        ANSWERS.append('{"value": 500, "confidence": 0.9, "reason": "moon"}')
        self.go(live=True)
        real = [t for t in TRADES if not t["dry"]]
        self.assertEqual(real[0]["body"]["targetValue"], 100)

    def test_json_wrapped_in_prose_or_fences_still_counts(self):
        self.one_market(price=50)
        ANSWERS.append('Sure! Here is my forecast:\n```json\n{"value": 20, "confidence": 0.8, "reason": "ok"}\n```\nHope this helps.')
        placed, _ = self.go(live=True)
        self.assertEqual(placed, 1)


    def test_THE_RULE_incomplete_JSON_never_authorizes_a_trade(self):
        self.one_market(price=50)
        ANSWERS.append('{"value": 20, "confidence": 0.8, "reason": "cut off')
        placed, _ = self.go(live=True)
        self.assertEqual(placed, 0)
        self.assertEqual(TRADES, [])

    def test_an_answer_cut_off_before_the_confidence_is_no_answer(self):
        # Without a confidence the number is unrated, and unrated is not a view.
        self.one_market(price=50)
        ANSWERS.append('{"value": 20, "confi')
        placed, out = self.go(live=True)
        self.assertEqual(placed, 0)
        self.assertIn("no usable answer", out)


class TestBadAnswersCostNothing(Base):
    def test_an_unparseable_answer_skips_that_market_and_continues(self):
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 50), market("m2", 60)])]
        ANSWERS.append("I would rather not say.")
        ANSWERS.append('{"value": 20, "confidence": 0.8, "reason": "ok"}')
        placed, out = self.go(live=True)
        self.assertEqual(placed, 1)
        self.assertIn("no usable answer", out)
        real = [t for t in TRADES if not t["dry"]]
        self.assertEqual([t["body"]["marketId"] for t in real], ["m2"])

    def test_an_empty_answer_that_was_all_reasoning_is_a_skip_not_a_crash(self):
        # Reasoning models spend their budget thinking and return no content.
        self.one_market(price=50)
        ANSWERS.append(None)
        placed, out = self.go(live=True)
        self.assertEqual(placed, 0)
        self.assertIn("no usable answer", out)

    def test_a_model_error_is_reported_and_the_run_goes_on(self):
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 50), market("m2", 60)])]
        ANSWERS.append(503)
        ANSWERS.append('{"value": 20, "confidence": 0.8, "reason": "ok"}')
        placed, out = self.go(live=True)
        self.assertEqual(placed, 1)
        self.assertIn("model refused", out)

    def test_THE_RULE_a_dry_run_places_nothing(self):
        self.one_market(price=50)
        ANSWERS.append('{"value": 20, "confidence": 0.9, "reason": "ok"}')
        placed, _ = self.go(live=False)
        self.assertEqual(placed, 1, "it should still report what it would do")
        self.assertTrue(all(t["dry"] for t in TRADES), "no live trade may be sent")


class TestFirstRun(Base):
    def test_provider_and_model_must_be_chosen_before_any_network(self):
        for missing in ("LLM_BASE_URL", "LLM_MODEL"):
            with mock.patch.dict(os.environ, {missing: "", "TELARCHY_WORKSPACE": "w"}):
                err = io.StringIO()
                with mock.patch("sys.stderr", err), mock.patch("sys.argv", ["llm_agent.py"]):
                    self.assertEqual(llm_agent.main(), 2)
                self.assertIn(missing, err.getvalue())
        self.assertEqual(ASKED, [])

    def test_local_provider_can_omit_authorization(self):
        self.one_market()
        with mock.patch.dict(os.environ, {"LLM_API_KEY": ""}):
            self.go()
        self.assertIsNone(ASKED[0]["auth"])


class TestForecastContract(Base):
    def test_THE_RULE_invalid_forecasts_never_reach_trading(self):
        bad = [
            {"value": v, "confidence": 0.9, "reason": "x"}
            for v in (True, "20", None, float("nan"), float("inf"), -float("inf"))
        ] + [
            {"value": 20, "confidence": c, "reason": "x"}
            for c in (True, "0.9", None, -0.1, 1.1, float("nan"), float("inf"))
        ] + [{"value": 20, "confidence": 0.9, "reason": r} for r in (None, "", "  ", 3)]
        for answer in bad:
            with self.subTest(answer=answer):
                self.one_market()
                ANSWERS.append(json.dumps(answer))
                self.assertEqual(self.go(live=True)[0], 0)
        self.assertEqual(TRADES, [])

    def test_trend_readings_and_full_settlement_instant_reach_model(self):
        self.one_market()
        SNAPSHOT["metrics"][0]["trend"] = [[1800000000, 12], [1800003600, 15]]
        self.go()
        prompt = ASKED[0]["body"]["messages"][-1]["content"]
        self.assertIn("[[1800000000, 12], [1800003600, 15]]", prompt)
        self.assertIn("2027-03-04T00:00:00Z", prompt)

    def test_incomplete_outer_object_cannot_smuggle_an_inner_forecast(self):
        text = '{"unfinished": {"value": 20, "confidence": 0.9, "reason": "inner"}'
        self.assertIsNone(llm_agent.parse(text))

    def test_unrepresentably_large_integer_is_invalid_not_a_crash(self):
        self.assertIsNone(llm_agent.parse(json.dumps({"value": 10**400, "confidence": 1, "reason": "x"})))

    def test_missing_fields_are_invalid(self):
        for key in ("value", "confidence", "reason"):
            answer = {"value": 20, "confidence": 0.9, "reason": "x"}
            del answer[key]
            self.assertIsNone(llm_agent.parse(json.dumps(answer)))

    def test_reason_may_contain_braces_and_escaped_quotes(self):
        answer = {"value": 20, "confidence": 1, "reason": 'Formula {Revenue} says "up".'}
        self.assertEqual(llm_agent.parse(json.dumps(answer)), answer)

    def test_model_failures_still_consume_the_call_limit(self):
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market(str(i), 90) for i in range(8)])]
        ANSWERS.extend([503, "invalid", '{"value": 20, "confidence": 1, "reason": "ok"}'])
        self.go(live=True, max_model_calls=2)
        self.assertEqual(len(ASKED), 2)
        self.assertEqual(TRADES, [])

    def test_zero_calls_or_zero_trading_budget_spends_no_inference(self):
        self.one_market()
        for kwargs in ({"max_model_calls": 0}, {"cycle_budget": 0}, {"budget_per_trade": 0}):
            self.go(live=True, **kwargs)
        self.assertEqual(ASKED, [])
        self.assertEqual(TRADES, [])

    def test_token_and_timeout_limits_reach_provider(self):
        self.one_market()
        original = llm_agent.urllib.request.urlopen
        timeouts = []
        def capture(req, *args, **kwargs):
            if "/chat/completions" in req.full_url:
                timeouts.append(kwargs.get("timeout"))
            return original(req, *args, **kwargs)
        with mock.patch.object(llm_agent.urllib.request, "urlopen", capture):
            self.go(max_tokens=77, model_timeout=3)
        self.assertEqual(ASKED[0]["body"]["max_tokens"], 77)
        self.assertEqual(timeouts, [3])

    def test_invalid_inference_limits_fail_before_requests(self):
        for kwargs in ({"max_model_calls": -1}, {"max_model_calls": 1.5}, {"max_tokens": 0},
                       {"max_tokens": True}, {"model_timeout": 0}, {"model_timeout": float("nan")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.go(**kwargs)
        self.assertEqual(ASKED, [])


if __name__ == "__main__":
    unittest.main()
