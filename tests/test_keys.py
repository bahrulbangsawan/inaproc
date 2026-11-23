"""Multiple API keys per provider and across providers, ordered by priority, with automatic failover."""
import os
import tempfile
import unittest
from unittest import mock

import tests._util  # noqa: F401
import inaproc_ai as ai
import inaproc_config as cfg
from tests.test_ai import FakeHTTP, FakeRunner


class ProfilesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"INAPROC_HOME": self.tmp.name})
        self.env.start()
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
            os.environ.pop(k, None)
        self.store = cfg.KeyStore(use_keychain=False)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class ProfilesTest(ProfilesBase):
    def test_several_keys_per_provider_and_across_providers_by_priority(self):
        p = cfg.Profiles(self.store)
        p.add("anthropic", "sk-ant-1", "claude-opus-5-5", label="kantor")
        p.add("anthropic", "sk-ant-2", "claude-sonnet-5-5", label="pribadi")
        p.add("openrouter", "sk-or-3", "anthropic/claude-opus-5.5", label="cadangan")
        order = [(e["label"], e["provider"]) for e in p.ordered()]
        self.assertEqual(order, [("kantor", "anthropic"), ("pribadi", "anthropic"), ("cadangan", "openrouter")])
        self.assertEqual(p.secret(p.ordered()[1]), "sk-ant-2")

    def test_reorder_and_remove(self):
        p = cfg.Profiles(self.store)
        for i in range(3):
            p.add("deepseek", f"sk-{i}", "deepseek-chat", label=f"k{i}")
        p.move("k2", 1)
        self.assertEqual([e["label"] for e in p.ordered()], ["k2", "k0", "k1"])
        p.remove("k0")
        self.assertEqual([e["label"] for e in p.ordered()], ["k2", "k1"])
        self.assertIsNone(self.store.get_account("deepseek:k0"))

    def test_keys_never_saved_in_config_json(self):
        p = cfg.Profiles(self.store)
        p.add("openai", "sk-proj-SECRET", "gpt-5", label="a")
        self.assertNotIn("SECRET", open(os.path.join(self.tmp.name, "config.json")).read())

    def test_migrates_the_old_single_key_setup(self):
        cfg.save_settings({"provider": "anthropic", "model": "claude-opus-5-5"})
        self.store.set("anthropic", "sk-ant-old")
        p = cfg.Profiles(self.store)
        e = p.ordered()[0]
        self.assertEqual((e["provider"], e["model"]), ("anthropic", "claude-opus-5-5"))
        self.assertEqual(p.secret(e), "sk-ant-old")

    def test_env_keys_appear_last_and_are_marked(self):
        p = cfg.Profiles(self.store)
        p.add("anthropic", "sk-ant-saved", "claude-opus-5-5", label="utama")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env"}):
            e = p.ordered()[-1]
            self.assertEqual((e["provider"], e["source"]), ("openai", "env OPENAI_API_KEY"))
            self.assertEqual(p.secret(e), "sk-env")

    def test_disabled_keys_are_skipped(self):
        p = cfg.Profiles(self.store)
        p.add("anthropic", "a", "claude-opus-5-5", label="a")
        p.add("anthropic", "b", "claude-opus-5-5", label="b")
        p.set_enabled("a", False)
        self.assertEqual([e["label"] for e in p.ordered(active_only=True)], ["b"])


class ClassifyTest(unittest.TestCase):
    def test_error_kinds(self):
        def kind(status, msg):
            with self.assertRaises(ai.AIError) as cm:
                ai._raise_for(status, {"error": {"message": msg}}, "anthropic")
            return cm.exception.kind, cm.exception.switchable
        self.assertEqual(kind(401, "invalid x-api-key"), ("auth", True))
        self.assertEqual(kind(400, "Your credit balance is too low to access the Anthropic API"), ("quota", True))
        self.assertEqual(kind(402, "Insufficient credits"), ("quota", True))
        self.assertEqual(kind(429, "You exceeded your current quota, please check your plan and billing"),
                         ("quota", True))
        self.assertEqual(kind(429, "rate limit"), ("rate", True))
        self.assertEqual(kind(404, "model not found"), ("model", True))
        self.assertEqual(kind(503, "unavailable"), ("server", True))
        self.assertEqual(kind(400, "messages: bad field"), ("request", False))


def ok(text):
    return (200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]})


def ok_openai(text):
    return (200, {"choices": [{"message": {"role": "assistant", "content": text}}]})


class FailoverTest(ProfilesBase):
    def _profiles(self):
        p = cfg.Profiles(self.store)
        p.add("anthropic", "k1", "claude-opus-5-5", label="habis")
        p.add("anthropic", "k2", "claude-opus-5-5", label="cadangan")
        p.add("openrouter", "k3", "anthropic/claude-opus-5.5", label="router")
        return p

    def test_switches_to_the_next_key_when_credit_runs_out(self):
        p = self._profiles()
        calls = []

        def http(method, url, headers, body=None, timeout=None):
            calls.append(headers.get("x-api-key") or headers.get("Authorization"))
            if headers.get("x-api-key") == "k1":
                return 400, {"error": {"message": "Your credit balance is too low"}}
            return ok("jawaban")
        switches = []
        m = ai.FailoverAgent(p, runner=FakeRunner(), http=http, system="S",
                             on_switch=lambda frm, to, err: switches.append((frm["label"], to["label"], err.kind)))
        self.assertEqual(m.send("halo"), "jawaban")
        self.assertEqual(switches, [("habis", "cadangan", "quota")])
        self.assertEqual(m.current["label"], "cadangan")
        status = {e["label"]: e for e in p.ordered()}
        self.assertEqual(status["habis"]["lastError"], "quota")

    def test_switches_across_providers_and_rebuilds_history(self):
        p = self._profiles()
        seen = {}

        def http(method, url, headers, body=None, timeout=None):
            if "anthropic.com" in url:
                return 401, {"error": {"message": "invalid x-api-key"}}
            seen["messages"] = list(body["messages"])
            return ok_openai("dari openrouter")
        m = ai.FailoverAgent(p, runner=FakeRunner(), http=http, system="S")
        m.transcript = [("pertanyaan lama", "jawaban lama")]
        self.assertEqual(m.send("baru"), "dari openrouter")
        self.assertEqual(m.current["provider"], "openrouter")
        roles = [x["role"] for x in seen["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(seen["messages"][2]["content"], "jawaban lama")

    def test_a_bad_request_does_not_burn_through_every_key(self):
        p = self._profiles()
        http = FakeHTTP([(400, {"error": {"message": "messages: bad field"}})])
        m = ai.FailoverAgent(p, runner=FakeRunner(), http=http, system="S")
        with self.assertRaises(ai.AIError):
            m.send("x")
        self.assertEqual(len(http.requests), 1)

    def test_all_keys_failing_reports_each_reason(self):
        p = self._profiles()

        def http(method, url, headers, body=None, timeout=None):
            return 401, {"error": {"message": "nope"}}
        m = ai.FailoverAgent(p, runner=FakeRunner(), http=http, system="S")
        with self.assertRaises(ai.AIError) as cm:
            m.send("x")
        for label in ("habis", "cadangan", "router"):
            self.assertIn(label, str(cm.exception))


if __name__ == "__main__":
    unittest.main()


from tests.test_app import AppTestBase, run  # noqa: E402
import inaproc_app as app  # noqa: E402


class KeysCommandTest(AppTestBase):
    def test_keys_list_shows_priority_and_masked_keys(self):
        self.add_key("anthropic", "claude-opus-5-5", key="sk-ant-secret-AAAA1111", label="kantor")
        self.add_key("openrouter", "anthropic/claude-opus-5.5", key="sk-or-secret-BBBB2222", label="cadangan")
        code, out, err = run(["keys"])
        self.assertEqual(code, 0, err)
        self.assertLess(out.index("kantor"), out.index("cadangan"))
        self.assertIn("…1111", out)
        self.assertNotIn("secret-AAAA", out)

    def test_keys_add_non_interactive(self):
        with mock.patch("inaproc_ai.verify_key", return_value=["deepseek-chat"]), \
                mock.patch("sys.stdin", io.StringIO("sk-ds-xyz\n")):
            code, out, err = run(["keys", "add", "--provider", "deepseek", "--model", "deepseek-chat",
                                  "--label", "murah", "--key-stdin"])
        self.assertEqual(code, 0, err)
        e = cfg.Profiles(self.store).find("murah")
        self.assertEqual((e["provider"], e["model"]), ("deepseek", "deepseek-chat"))

    def test_keys_move_and_remove(self):
        self.add_key(label="a")
        self.add_key(label="b")
        self.assertEqual(run(["keys", "move", "b", "1"])[0], 0)
        self.assertEqual([e["label"] for e in cfg.Profiles(self.store).ordered()], ["b", "a"])
        self.assertEqual(run(["keys", "remove", "a"])[0], 0)
        self.assertEqual([e["label"] for e in cfg.Profiles(self.store).ordered()], ["b"])

    def test_model_command_changes_the_model(self):
        self.add_key("anthropic", "claude-opus-5-5", label="utama")
        with mock.patch("inaproc_ai.verify_key", return_value=["claude-opus-5-5", "claude-sonnet-5-5"]):
            code, out, err = run(["model"], inputs=["2"])
        self.assertEqual(code, 0, err)
        self.assertEqual(cfg.Profiles(self.store).find("utama")["model"], "claude-sonnet-5-5")
        code, out, err = run(["model", "utama", "claude-haiku-5-5"])
        self.assertEqual(cfg.Profiles(self.store).find("utama")["model"], "claude-haiku-5-5")

    def test_ask_shows_the_switch(self):
        self.add_key("anthropic", "claude-opus-5-5", key="k-habis", label="habis")
        self.add_key("anthropic", "claude-opus-5-5", key="k-ok", label="cadangan")

        def http(method, url, headers, body=None, timeout=None):
            if headers.get("x-api-key") == "k-habis":
                return 400, {"error": {"message": "Your credit balance is too low"}}
            return ok("beres")
        with mock.patch("inaproc_ai.http_json", side_effect=http):
            code, out, err = run(["ask", "halo"])
        self.assertEqual(code, 0, err)
        self.assertIn("beres", out)
        self.assertIn("cadangan", err)
        self.assertIn("saldo", err.lower())


import io  # noqa: E402
