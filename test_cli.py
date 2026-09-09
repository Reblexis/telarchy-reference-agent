"""The commands builders run exercise the same limits as direct calls."""
import io
import os
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

import agent
import llm_agent
from telarchy import Telarchy
from test_agent import SNAPSHOT, TRADES, market, metric
from test_llm_agent import Base, ASKED


class TestCommands(Base):
    def invoke(self, module, *flags, key="k"):
        client = Telarchy(key=key, workspace="w", base_url=self.base)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TELARCHY_KEY": key, "TELARCHY_WORKSPACE": "w"}), \
             mock.patch.object(module, "Telarchy", return_value=client), \
             mock.patch("sys.argv", [module.__name__ + ".py", *flags]), \
             redirect_stdout(out), redirect_stderr(out):
            code = module.main()
        return code, out.getvalue()

    def test_both_commands_forward_credit_limits_in_dry_and_live_modes(self):
        for module in (agent, llm_agent):
            for live in (False, True):
                with self.subTest(module=module.__name__, live=live):
                    TRADES.clear()
                    SNAPSHOT["metrics"] = [metric("Revenue", 10, [market(str(i), 90) for i in range(4)])]
                    flags = ["--budget-per-trade", "1", "--cycle-budget", "1.5"]
                    if live:
                        flags.append("--live")
                    code, out = self.invoke(module, *flags)
                    self.assertEqual(code, 0)
                    self.assertIn("2 trade(s)", out)
                    self.assertEqual([t["body"]["maxBudget"] for t in TRADES if t["dry"]], [1, 0.5])
                    self.assertEqual(len([t for t in TRADES if not t["dry"]]), 2 if live else 0)

    def test_model_command_forwards_inference_limits(self):
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market(str(i), 90) for i in range(4)])]
        code, _ = self.invoke(llm_agent, "--max-model-calls", "1", "--max-tokens", "73", "--model-timeout", "2")
        self.assertEqual(code, 0)
        self.assertEqual(len(ASKED), 1)
        self.assertEqual(ASKED[0]["body"]["max_tokens"], 73)

    def test_live_without_a_key_exits_before_any_trade(self):
        for module in (agent, llm_agent):
            self.assertEqual(self.invoke(module, "--live", key="")[0], 2)
        self.assertEqual(TRADES, [])
        self.assertEqual(ASKED, [])

    def test_bad_cli_limits_report_configuration_error(self):
        for module in (agent, llm_agent):
            code, out = self.invoke(module, "--cycle-budget", "nan")
            self.assertEqual(code, 2)
            self.assertIn("finite", out)
        self.assertEqual(TRADES, [])
        self.assertEqual(ASKED, [])
