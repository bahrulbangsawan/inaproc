import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

import tests._util  # noqa: F401
import inaproc_app as app
import inaproc_config as cfg
import inaproc_safari as sf


def run(argv, inputs=None, secret=None):
    out, err = io.StringIO(), io.StringIO()
    patches = [mock.patch("builtins.input", side_effect=list(inputs or []))]
    if secret is not None:
        patches.append(mock.patch("getpass.getpass", side_effect=list(secret)))
    with contextlib.ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        st.enter_context(contextlib.redirect_stdout(out))
        st.enter_context(contextlib.redirect_stderr(err))
        code = app.main(argv)
    return code, out.getvalue(), err.getvalue()


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"INAPROC_HOME": self.tmp.name, "INAPROC_AUTO_UPDATE": "0"})
        self.env.start()
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
            os.environ.pop(k, None)
        self.ks = mock.patch.object(cfg, "KeyStore", lambda *a, **k: cfg.KeyStore.__wrapped__(use_keychain=False)
                                    if hasattr(cfg.KeyStore, "__wrapped__") else None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class HomeTest(AppTestBase):
    def test_home_when_not_a_terminal_shows_logo_and_commands(self):
        code, out, err = run([])
        self.assertEqual(code, 0)
        self.assertIn("██╗", out)
        for word in ("setup", "ai", "whoami", "doctor", "penyedia"):
            self.assertIn(word, out)

    def test_help(self):
        code, out, _ = run(["help"])
        self.assertEqual(code, 0)
        self.assertIn("cheapest", out)


class PassthroughTest(AppTestBase):
    def test_runs_a_catalogue_command_and_draws_tables(self):
        def fake_main(argv):
            print("**Hasil**\n\n| No | Produk |\n|---|---|\n| 1 | Kertas |")
            return 0
        with mock.patch("inaproc.main", side_effect=fake_main) as m, \
                mock.patch.dict(os.environ, {"INAPROC_PRETTY": "1"}):
            code, out, err = run(["cheapest", "kertas"])
        self.assertEqual(code, 0)
        m.assert_called_once_with(["cheapest", "kertas"])
        self.assertIn("╭", out)       # markdown table drawn as a box
        self.assertIn("Kertas", out)
        self.assertNotIn("|---|", out)

    def test_piped_output_stays_markdown(self):
        with mock.patch("inaproc.main", side_effect=lambda a: print("| a |\n|---|\n| 1 |") or 0):
            code, out, _ = run(["ppn"])
        self.assertIn("|---|", out)

    def test_json_output_is_left_untouched(self):
        with mock.patch("inaproc.main", side_effect=lambda a: print('{"a": 1}') or 0):
            code, out, _ = run(["ppn", "--json"])
        self.assertEqual(out.strip(), '{"a": 1}')

    def test_free_text_goes_to_the_ai_when_configured(self):
        cfg.save_settings({"provider": "deepseek", "model": "deepseek-chat"})
        with mock.patch.object(app, "_key_for", return_value=("k", "env")), \
                mock.patch.object(app, "run_ask", return_value=0) as ask:
            code, _, _ = run(["carikan", "kertas", "termurah"])
        ask.assert_called_once()
        self.assertEqual(ask.call_args[0][0], "carikan kertas termurah")

    def test_free_text_without_ai_points_to_setup(self):
        code, out, err = run(["carikan", "kertas"])
        self.assertNotEqual(code, 0)
        self.assertIn("inaproc setup", out + err)


class SafariGuideTest(AppTestBase):
    def test_setting_error_shows_the_step_by_step_guide(self):
        def fake_main(argv):
            print("error: " + sf.SETTING_HELP, file=sys.stderr)
            return 1
        with mock.patch("inaproc.main", side_effect=fake_main):
            code, out, err = run(["penyedia", "products"])
        text = out + err
        self.assertEqual(code, 1)
        self.assertIn("Show features for web developers", text)
        self.assertIn("Allow JavaScript from Apple Events", text)
        self.assertIn("Develop", text)
        self.assertIn("1.", text)

    def test_automation_error_points_to_privacy_settings(self):
        with mock.patch("inaproc.main", side_effect=lambda a: print("error: " + sf.AUTOMATION_HELP, file=sys.stderr) or 1):
            code, out, err = run(["penyedia", "status"])
        self.assertIn("Automation", out + err)

    def test_safari_timeout_explains_the_permission_dialog(self):
        g = app.guide_for("Safari did not answer in time")
        self.assertIsNotNone(g)
        self.assertIn("OK", " ".join(g[2]))

    def test_not_signed_in_guide(self):
        with mock.patch("inaproc.main",
                        side_effect=lambda a: print("error: not signed in to penyedia.inaproc.id; run `penyedia login`",
                                                    file=sys.stderr) or 3):
            code, out, err = run(["penyedia", "products"])
        self.assertEqual(code, 3)
        self.assertIn("inaproc login", out + err)


class SetupTest(AppTestBase):
    def test_wizard_saves_provider_model_and_key(self):
        store = cfg.KeyStore(use_keychain=False)
        with mock.patch.object(app, "_store", return_value=store), \
                mock.patch("inaproc_ai.list_models", return_value=["deepseek-chat", "deepseek-reasoner"]):
            code, out, err = run(["setup"], inputs=["4", "1"], secret=["sk-ds-secret-9999"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(cfg.load_settings(), {"provider": "deepseek", "model": "deepseek-chat"})
        self.assertEqual(store.get("deepseek")[0], "sk-ds-secret-9999")
        self.assertNotIn("sk-ds-secret-9999", out + err)
        self.assertIn("…9999", out + err)

    def test_wizard_stops_on_a_rejected_key(self):
        import inaproc_ai
        store = cfg.KeyStore(use_keychain=False)
        with mock.patch.object(app, "_store", return_value=store), \
                mock.patch("inaproc_ai.list_models", side_effect=inaproc_ai.AIError("ditolak")):
            code, out, err = run(["setup"], inputs=["1"], secret=["bad"])
        self.assertNotEqual(code, 0)
        self.assertEqual(store.get("anthropic"), (None, None))


class WhoamiTest(AppTestBase):
    def test_whoami_shows_account_and_ai(self):
        cfg.save_settings({"provider": "anthropic", "model": "claude-opus-5-5"})
        with mock.patch.object(app, "_key_for", return_value=("sk-ant-api03-zzzz1234", "Keychain")), \
                mock.patch.object(app, "_penyedia_info",
                                  return_value={"signedIn": True, "name": "BAHRUL", "company": None,
                                                "expires": "2026-11-23T13:00:00Z"}):
            code, out, err = run(["whoami"])
        self.assertEqual(code, 0)
        self.assertIn("BAHRUL", out)
        self.assertIn("claude-opus-5-5", out)
        self.assertIn("…1234", out)
        self.assertNotIn("zzzz1234", out)


class AskFlowTest(AppTestBase):
    def test_ask_shows_live_steps_for_thinking_and_tools(self):
        cfg.save_settings({"provider": "anthropic", "model": "claude-opus-5-5"})
        replies = [(200, {"stop_reason": "tool_use", "content": [
                        {"type": "tool_use", "id": "t1", "name": "inaproc", "input": {"args": ["ppn"]}}]}),
                   (200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "PPN **12%**"}]})]
        with mock.patch.object(app, "_key_for", return_value=("k", "env")), \
                mock.patch("inaproc_ai.http_json", side_effect=lambda *a, **k: replies.pop(0)), \
                mock.patch("inaproc_ai.run_cli", return_value=(0, "PPN rates: 12%", "")):
            code, out, err = run(["ask", "berapa ppn"])
        self.assertEqual(code, 0, err)
        self.assertIn("AI sedang berpikir", err)
        self.assertIn("inaproc ppn", err)
        self.assertIn("12%", out)

    def test_bold_inside_table_cells_is_rendered(self):
        text = app.pretty("| A | B |\n|---|---|\n| **Rp48.000** | x |", color=False)
        self.assertIn("Rp48.000", text)
        self.assertNotIn("**", text)


class ProgressHookTest(unittest.TestCase):
    def test_catalogue_calls_report_progress(self):
        import inaproc
        import inaproc_ui as ui
        seen = []
        with mock.patch.object(ui, "progress", side_effect=seen.append), \
                mock.patch.object(inaproc.Client, "_post", return_value=(200, '{"data": {"x": 1}}')):
            c = inaproc.Client(min_interval=0.5)
            c.gql("getAllPPN", {}) if "getAllPPN" in inaproc.QUERIES else c.gql(next(iter(inaproc.QUERIES)), {})
        self.assertTrue(seen)


if __name__ == "__main__":
    unittest.main()
