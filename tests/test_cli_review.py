"""Regression tests for the standalone-CLI blind-spot review (C1-C2, I1-I11, selected minors)."""
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.test_app import AppTestBase, run
from tests.test_ai import FakeHTTP, FakeRunner
import inaproc_ai as ai
import inaproc_app as app
import inaproc_config as cfg
import inaproc_penyedia as ip
import inaproc_ui as ui


class ConfirmPanelTest(AppTestBase):  # C1
    def _plan(self, writes, kind="update"):
        return json.load(open(ip.make_plan(kind, "BAHRUL", writes)))

    def test_update_panel_shows_field_changes(self):
        doc = self._plan([{"op": "updateProduct", "variables": {"id": "p1", "input": {}}, "productId": "p1",
                           "beforeHash": "h", "summary": "UPDATE Kertas",
                           "diff": [["variants[0].price", 540541, 6000]]}])
        out = io.StringIO()
        with mock.patch.object(app, "_interactive", return_value=False), mock.patch("sys.stdout", out):
            app._confirm_plan_factory(lambda: None)(doc, "/x/plan_1.json")
        text = out.getvalue()
        self.assertIn("variants[0].price", text)
        self.assertIn("540541", text)
        self.assertIn("6000", text)

    def test_create_panel_shows_submit_mode_price_and_stock(self):
        doc = self._plan([{"op": "createProduct", "summary": "CREATE Kotak", "variables": {"input": {
            "name": "Kotak", "category": {"id": "c"}, "variants": [{"price": 350000, "stock": 5}]}}}], kind="create")
        out = io.StringIO()
        with mock.patch.object(app, "_interactive", return_value=False), mock.patch("sys.stdout", out):
            app._confirm_plan_factory(lambda: None)(doc, "/x/plan_1.json")
        text = out.getvalue()
        self.assertIn("kurasi", text.lower())
        self.assertIn("350", text)


class TypeAheadTest(unittest.TestCase):  # C2
    def test_confirm_flushes_typed_ahead_input(self):
        with mock.patch.object(ui, "_flush_stdin") as fl, mock.patch("builtins.input", return_value="y"):
            self.assertTrue(ui.confirm("Kirim?", stream=io.StringIO(), flush=True))
        fl.assert_called_once()

    def test_only_a_clear_yes_counts(self):
        for word in ("ok lanjut", "oke", "iya dong", "lanjut"):
            with mock.patch("builtins.input", return_value=word):
                self.assertFalse(ui.confirm("Kirim?", stream=io.StringIO()), word)


class HistoryRecoveryTest(unittest.TestCase):  # I1
    def test_interrupt_during_a_tool_rolls_the_turn_back(self):
        http = FakeHTTP([(200, {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t1", "name": "inaproc", "input": {"args": ["ppn"]}}]})])
        runner = mock.Mock()
        runner.run.side_effect = KeyboardInterrupt
        agent = ai.Agent("anthropic", "k", "claude-opus-5-5", runner=runner, http=http, system="S")
        with self.assertRaises(KeyboardInterrupt):
            agent.send("x")
        self.assertEqual(agent.messages, [])

    def test_max_tokens_cutting_a_tool_call_does_not_leave_it_dangling(self):
        http = FakeHTTP([(200, {"stop_reason": "max_tokens", "content": [
            {"type": "text", "text": "mulai"}, {"type": "tool_use", "id": "t1", "name": "inaproc", "input": {}}]})])
        agent = ai.Agent("anthropic", "k", "claude-opus-5-5", runner=FakeRunner(), http=http, system="S")
        agent.send("x")
        self.assertEqual(agent.messages, [])


class ProviderRobustnessTest(unittest.TestCase):  # I2, I3
    def test_retries_on_overload_then_succeeds(self):
        http = FakeHTTP([(529, {"error": {"message": "overloaded"}}),
                         (200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]})])
        with mock.patch("time.sleep"):
            out = ai.Agent("anthropic", "k", "claude-opus-5-5", runner=FakeRunner(), http=http, system="S").send("x")
        self.assertEqual(out, "ok")

    def test_prompt_caching_on(self):
        http = FakeHTTP([(200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]})])
        ai.Agent("anthropic", "k", "claude-opus-5-5", runner=FakeRunner(), http=http, system="S").send("x")
        self.assertEqual(http.requests[0]["body"]["cache_control"], {"type": "ephemeral"})

    def test_non_json_reply_is_a_clean_error(self):
        def http(*a, **k):
            raise ai.AIError("bukan JSON")
        with mock.patch("urllib.request.urlopen") as uo:
            uo.return_value.__enter__.return_value.read.return_value = b"<html>portal</html>"
            uo.return_value.__enter__.return_value.status = 200
            with self.assertRaises(ai.AIError):
                ai.http_json("GET", "https://x.example/models", {})

    def test_long_context_error_suggests_new_chat(self):
        http = FakeHTTP([(400, {"error": {"message": "prompt is too long: 1200000 tokens > 1000000"}})])
        with self.assertRaises(ai.AIError) as cm:
            ai.Agent("anthropic", "k", "claude-opus-5-5", runner=FakeRunner(), http=http, system="S").send("x")
        self.assertIn("/baru", str(cm.exception))


class OpenRouterKeyTest(unittest.TestCase):  # I4
    def test_openrouter_key_is_checked_on_an_authenticated_endpoint(self):
        http = FakeHTTP([(401, {"error": {"message": "No auth credentials found"}})])
        with self.assertRaises(ai.AIError):
            ai.verify_key("openrouter", "bad", http=http)
        self.assertTrue(http.requests[0]["url"].endswith("/key"))


class ModelPickerTest(unittest.TestCase):  # I5
    def test_openai_default_is_a_chat_model(self):
        ids = app.model_choices("openai", ["omni-moderation-latest", "o1-pro", "gpt-5-codex", "gpt-4o-mini",
                                           "gpt-5", "gpt-realtime", "text-embedding-3-large"])
        self.assertNotIn("omni-moderation-latest", ids)
        self.assertNotIn("o1-pro", ids)
        self.assertNotIn("gpt-5-codex", ids)
        self.assertEqual(ids[0], "gpt-5")

    def test_openrouter_recommends_the_newest_claude_opus(self):
        ids = app.model_choices("openrouter", ["anthropic/claude-3-haiku", "anthropic/claude-opus-4.5",
                                               "anthropic/claude-opus-5.5", "openai/gpt-5"])
        self.assertEqual(ids[0], "anthropic/claude-opus-5.5")


class EnvKeyTest(AppTestBase):  # I6
    def test_setup_warns_when_an_env_key_overrides(self):
        store = cfg.KeyStore(use_keychain=False)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-old-1111"}), \
                mock.patch.object(app, "_store", return_value=store), \
                mock.patch("inaproc_ai.verify_key", return_value=["claude-opus-5-5"]):
            code, out, err = run(["setup"], inputs=["1", "1", ""], secret=["sk-ant-new-2222"])
        self.assertIn("ANTHROPIC_API_KEY", out + err)


class EofTest(AppTestBase):  # I7
    def test_ctrl_d_in_the_menu_exits_cleanly(self):
        with mock.patch.object(app, "_interactive", return_value=True):
            code, out, err = run(["menu"], inputs=[EOFError()])
        self.assertNotIn("Traceback", err)
        self.assertIn(code, (0, 130))


class TruncationTest(unittest.TestCase):  # I8
    def test_long_output_keeps_the_plan_path_at_the_end(self):
        long = "x\n" * 40000 + "Nothing was sent. To send exactly this plan: penyedia apply /h/plan_9.json"
        r = ai.ToolRunner(executor=mock.Mock(return_value=(0, long, "")), confirm=mock.Mock())
        text, err = r.run("inaproc", {"args": ["penyedia", "product", "create", "--from", "a", "--name", "bbbbb"]})
        self.assertIn("plan_9.json", text)
        self.assertLess(len(text), 40000)


class RabSpecToolTest(AppTestBase):  # I9
    def test_spec_tool_writes_only_json_into_the_specs_folder(self):
        r = ai.ToolRunner(executor=mock.Mock(), confirm=mock.Mock())
        text, err = r.run("save_spec", {"name": "rab marka", "spec": {"title": "RAB", "items": []}})
        self.assertFalse(err, text)
        path = text.split()[-1]
        self.assertTrue(path.startswith(os.path.join(self.tmp.name, "specs")))
        self.assertEqual(json.load(open(path))["title"], "RAB")
        text, err = r.run("save_spec", {"name": "../../evil", "spec": {}})
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "..", "evil.json")))

    def test_system_prompt_includes_the_rab_reference(self):
        self.assertIn("Perbandingan", ai.system_prompt())
        self.assertIn("save_spec", ai.system_prompt())


class RiskyFlagsTest(unittest.TestCase):  # I10
    def test_dangerous_flags_are_refused_and_pages_capped(self):
        r = ai.ToolRunner(executor=mock.Mock(return_value=(0, "ok", "")), confirm=mock.Mock())
        for args in (["penyedia", "products", "--chrome-path", "/bin/sh"], ["search", "x", "--proxy", "http://e"],
                     ["search", "x", "--min-interval", "0.5"], ["search", "x", "--concurrency", "4"]):
            text, err = r.run("inaproc", {"args": args})
            self.assertTrue(err, args)
        text, err = r.run("inaproc", {"args": ["search", "x", "--pages", "50"]})
        self.assertTrue(err)


class KeychainTimeoutTest(unittest.TestCase):  # I11
    def test_locked_keychain_is_a_clear_error(self):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired(a[0], 10)
        store = cfg.KeyStore(runner=slow, use_keychain=True)
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("ANTHROPIC_API_KEY",):
                os.environ.pop(k, None)
            with self.assertRaises(cfg.KeyStoreError) as cm:
                store.get("anthropic")
        self.assertIn("Keychain", str(cm.exception))


class MinorsTest(AppTestBase):
    def test_setup_flag_without_value(self):
        code, out, err = run(["setup", "--provider"])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)

    def test_openai_arguments_already_a_dict(self):
        first = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "inaproc", "arguments": {"args": ["ppn"]}}}]}}]}
        second = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        runner = FakeRunner()
        ai.Agent("openai", "k", "m", runner=runner, http=FakeHTTP([(200, first), (200, second)]), system="S").send("x")
        self.assertEqual(runner.calls, [("inaproc", {"args": ["ppn"]})])

    def test_apply_plan_with_bad_args_is_an_error_not_a_crash(self):
        r = ai.ToolRunner(executor=mock.Mock(), confirm=mock.Mock())
        text, err = r.run("apply_plan", ["not", "a", "dict"])
        self.assertTrue(err)

    def test_typo_suggests_the_command_instead_of_calling_the_ai(self):
        self.add_key("deepseek", "deepseek-chat")
        with mock.patch.object(app, "run_ask") as ask:
            code, out, err = run(["serch", "kertas"])
        ask.assert_not_called()
        self.assertIn("search", out + err)

    def test_ai_help(self):
        with mock.patch.object(app, "run_ask") as ask:
            code, out, err = run(["ai", "--help"])
        ask.assert_not_called()
        self.assertEqual(code, 0)

    def test_guide_not_printed_twice(self):
        import sys
        import inaproc_safari as sf
        with mock.patch("inaproc.main", side_effect=lambda a: print("error: " + sf.SETTING_HELP, file=sys.stderr) or 1):
            code, out, err = run(["penyedia", "products"])
        self.assertEqual((out + err).count("Show features for web developers"), 1)


if __name__ == "__main__":
    unittest.main()
