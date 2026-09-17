"""The first ten minutes: no workspace to choose, one command to connect, and
every dead end says what to do next."""
import io
import os
import stat
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

import agent
import llm_agent
from telarchy import Telarchy
from test_agent import BALANCE, READS, SNAPSHOT, TRADES, market, metric
from test_llm_agent import Base

AGENTS_URL = "https://telarchy.com/agents"


class Setup(Base):
    def setUp(self):
        super().setUp()
        READS.clear()
        BALANCE[0] = 100.0
        self.dir = tempfile.TemporaryDirectory()
        self.key_file = os.path.join(self.dir.name, ".telarchy-key")
        self.made = []
        SNAPSHOT["metrics"] = [metric("Revenue", 10, [market("m1", 90)])]

    def tearDown(self):
        self.dir.cleanup()
        super().tearDown()

    def invoke(self, module, *flags, env=None, typed=None, sleeps=None):
        def make(key=None, workspace=None, **_):
            self.made.append({"key": key, "workspace": workspace})
            return Telarchy(key=key, workspace=workspace, base_url=self.base)

        environ = {k: v for k, v in os.environ.items() if not k.startswith("TELARCHY_")}
        environ.update(env or {})
        out = io.StringIO()
        getpass = mock.Mock(side_effect=typed if isinstance(typed, BaseException) else None, return_value=typed)
        with mock.patch.dict(os.environ, environ, clear=True), \
             mock.patch.object(agent, "KEY_FILE", self.key_file), \
             mock.patch.object(module, "Telarchy", side_effect=make), \
             mock.patch.object(agent.getpass, "getpass", getpass), \
             mock.patch.object(agent.time, "sleep", side_effect=sleeps), \
             mock.patch("sys.argv", [module.__name__ + ".py", *flags]), \
             redirect_stdout(out), redirect_stderr(out):
            code = module.main()
        return code, out.getvalue()

    def read_key(self):
        with open(self.key_file) as f:
            return f.read().strip()

    def save(self, key):
        with open(self.key_file, "w") as f:
            f.write(key + "\n")


class TestNoWorkspaceToChoose(Setup):
    def test_with_no_workspace_named_it_previews_the_public_telarchy_floor(self):
        for module in (agent, llm_agent):
            self.made.clear()
            code, out = self.invoke(module)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.made[0]["workspace"], "telarchy")
            self.assertIn("telarchy", out)

    def test_flag_beats_environment_beats_default(self):
        self.invoke(agent, env={"TELARCHY_WORKSPACE": "envfloor"})
        self.invoke(agent, "--workspace", "flagfloor", env={"TELARCHY_WORKSPACE": "envfloor"})
        self.assertEqual([m["workspace"] for m in self.made], ["envfloor", "flagfloor"])


class TestLogin(Setup):
    def test_login_checks_the_key_then_saves_it_private_to_the_user(self):
        BALANCE[0] = 12.5
        code, out = self.invoke(agent, "--login", typed="  good-key \n")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_key(), "good-key")
        self.assertEqual(stat.S_IMODE(os.stat(self.key_file).st_mode), 0o600)
        self.assertIn("12.5", out)
        self.assertNotIn("good-key", out)
        self.assertEqual(TRADES, [])

    def test_login_with_a_refused_key_saves_nothing_and_says_where_keys_come_from(self):
        code, out = self.invoke(agent, "--login", typed="bad")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.key_file))
        self.assertIn(AGENTS_URL, out)

    def test_login_with_a_refused_key_keeps_the_key_already_saved(self):
        self.save("old-key")
        self.invoke(agent, "--login", typed="bad")
        self.assertEqual(self.read_key(), "old-key")

    def test_login_with_nothing_typed_saves_nothing(self):
        for typed in ("", "   ", EOFError(), KeyboardInterrupt()):
            code, out = self.invoke(agent, "--login", typed=typed)
            self.assertEqual(code, 2, out)
            self.assertFalse(os.path.exists(self.key_file))

    def test_login_never_trades_even_with_live(self):
        self.invoke(agent, "--login", "--live", typed="good-key")
        self.assertEqual(TRADES, [])

    def test_login_works_from_the_llm_starter_too(self):
        code, _ = self.invoke(llm_agent, "--login", typed="good-key")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_key(), "good-key")


class TestWhichKeyIsUsed(Setup):
    def test_the_saved_key_is_used_when_the_environment_has_none(self):
        self.save("saved-key")
        self.invoke(agent)
        self.assertEqual(self.made[0]["key"], "saved-key")

    def test_the_environment_key_beats_the_saved_key(self):
        self.save("saved-key")
        self.invoke(agent, env={"TELARCHY_KEY": "env-key"})
        self.assertEqual(self.made[0]["key"], "env-key")

    def test_no_key_anywhere_still_previews(self):
        code, out = self.invoke(agent)
        self.assertEqual(code, 0)
        self.assertIsNone(self.made[0]["key"])
        self.assertIn("1 trade(s) would be placed", out)
        self.assertIn("--login", out)  # the next step is named

    def test_an_empty_saved_key_counts_as_no_key(self):
        self.save("")
        self.invoke(agent)
        self.assertIsNone(self.made[0]["key"])


class TestDeadEndsSayWhatToDoNext(Setup):
    def test_live_without_a_key_names_login_and_trades_nothing(self):
        for module in (agent, llm_agent):
            code, out = self.invoke(module, "--live")
            self.assertEqual(code, 2)
            self.assertIn("--login", out)
        self.assertEqual(TRADES, [])

    def test_a_refused_key_says_so_before_anything_else_and_points_to_keys(self):
        code, out = self.invoke(agent, env={"TELARCHY_KEY": "bad"})
        self.assertEqual(code, 1)
        self.assertIn(AGENTS_URL, out)
        self.assertIn("--login", out)
        self.assertNotIn("dry run on", out)
        self.assertEqual(TRADES, [])

    def test_a_key_holder_sees_the_balance_up_front(self):
        BALANCE[0] = 42.0
        _, out = self.invoke(agent, env={"TELARCHY_KEY": "k"})
        self.assertIn("42", out)

    def test_live_with_no_credits_trades_nothing_and_says_where_credits_come_from(self):
        BALANCE[0] = 0
        code, out = self.invoke(agent, "--live", env={"TELARCHY_KEY": "k"})
        self.assertEqual(code, 1)
        self.assertIn(AGENTS_URL, out)
        self.assertIn("credits", out)
        self.assertEqual(TRADES, [])

    def test_a_preview_with_no_credits_still_previews(self):
        BALANCE[0] = 0
        code, out = self.invoke(agent, env={"TELARCHY_KEY": "k"})
        self.assertEqual(code, 0)
        self.assertIn("would be placed", out)

    def test_after_live_trades_it_says_where_to_see_them(self):
        code, out = self.invoke(agent, "--live", env={"TELARCHY_KEY": "k"})
        self.assertEqual(code, 0)
        self.assertIn("1 trade(s) placed", out)
        self.assertIn(AGENTS_URL, out.split("1 trade(s) placed")[1])

    def test_a_preview_with_a_key_names_the_live_command(self):
        _, out = self.invoke(agent, env={"TELARCHY_KEY": "k"})
        self.assertIn("--live", out.split("would be placed")[1])


class Stop(Exception):
    pass


class TestKeepsRunning(Setup):
    def test_every_repeats_the_cycle_and_waits_that_many_minutes(self):
        with self.assertRaises(Stop):
            self.invoke(agent, "--every", "30", sleeps=[None, Stop()])
        self.assertEqual(len([r for r in READS if r.startswith("/api/status")]), 2)

    def test_every_sleeps_in_minutes(self):
        with mock.patch.object(agent.time, "sleep", side_effect=Stop()) as sleep, self.assertRaises(Stop):
            self.invoke_no_sleep_patch(agent, "--every", "30")
        sleep.assert_called_once_with(1800)

    def invoke_no_sleep_patch(self, module, *flags):
        def make(key=None, workspace=None, **_):
            return Telarchy(key=key, workspace=workspace, base_url=self.base)
        environ = {k: v for k, v in os.environ.items() if not k.startswith("TELARCHY_")}
        with mock.patch.dict(os.environ, environ, clear=True), \
             mock.patch.object(agent, "KEY_FILE", self.key_file), \
             mock.patch.object(module, "Telarchy", side_effect=make), \
             mock.patch("sys.argv", [module.__name__ + ".py", *flags]), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return module.main()

    def test_a_failed_cycle_does_not_end_a_repeating_run(self):

        with mock.patch.object(agent, "run", side_effect=[agent.TelarchyError("down", status=503), 0]) as run, \
             self.assertRaises(Stop):
            self.invoke(agent, "--every", "1", sleeps=[None, Stop()])
        self.assertEqual(run.call_count, 2)

    def test_every_refuses_zero_negative_and_nonsense(self):
        for bad in ("0", "-5", "nan", "inf"):
            code, out = self.invoke(agent, "--every", bad)
            self.assertEqual(code, 2, bad)
        self.assertEqual(TRADES, [])

    def test_without_every_it_runs_once(self):
        self.invoke(agent)
        self.assertEqual(len([r for r in READS if r.startswith("/api/status")]), 1)


class TestHintsCanBePasted(Setup):
    def test_hints_name_the_interpreter_and_file_actually_running(self):
        with mock.patch.object(agent.sys, "executable", os.path.join(os.getcwd(), ".venv", "bin", "python")):
            _, out = self.invoke(llm_agent)
        self.assertIn(os.path.join(".venv", "bin", "python") + " llm_agent.py --login", out)

    def test_an_interpreter_outside_this_folder_is_just_python(self):
        with mock.patch.object(agent.sys, "executable", "/usr/bin/python3"), \
             mock.patch.object(agent.os, "getcwd", return_value="/home/someone/agent"), \
             mock.patch.object(agent.os.path, "relpath", return_value="../../../usr/bin/python3"):
            _, out = self.invoke(agent)
        self.assertIn("python agent.py --login", out)
