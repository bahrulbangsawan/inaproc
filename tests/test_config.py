import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import tests._util  # noqa: F401
import inaproc_config as cfg


class FakeSecurity:
    """Stands in for macOS `security` (Keychain)."""

    def __init__(self):
        self.store = {}
        self.calls = []

    def __call__(self, argv, capture_output=True, text=True, timeout=10, input=None):
        self.calls.append(argv)
        if argv == ["security", "-i"]:  # interactive mode: commands (and the key) arrive on stdin
            import shlex
            parts = shlex.split(input.strip())
            self.store[parts[parts.index("-a") + 1]] = parts[parts.index("-w") + 1]
            return subprocess.CompletedProcess(argv, 0, "", "")
        op = argv[1]
        acct = argv[argv.index("-a") + 1]
        if op == "add-generic-password":
            self.store[acct] = argv[argv.index("-w") + 1] if "-w" in argv and argv.index("-w") + 1 < len(argv) else input
            return subprocess.CompletedProcess(argv, 0, "", "")
        if op == "find-generic-password":
            if acct in self.store:
                return subprocess.CompletedProcess(argv, 0, self.store[acct] + "\n", "")
            return subprocess.CompletedProcess(argv, 44, "", "not found")
        if op == "delete-generic-password":
            self.store.pop(acct, None)
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"INAPROC_HOME": self.tmp.name}, clear=False)
        self.env.start()
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_providers(self):
        self.assertEqual(set(cfg.PROVIDERS), {"anthropic", "openai", "openrouter", "deepseek"})
        self.assertEqual(cfg.PROVIDERS["anthropic"]["default_model"], "claude-opus-5-5")
        self.assertEqual(cfg.PROVIDERS["openrouter"]["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(cfg.PROVIDERS["deepseek"]["base_url"], "https://api.deepseek.com")
        self.assertEqual(cfg.PROVIDERS["anthropic"]["kind"], "anthropic")
        self.assertEqual({p["kind"] for n, p in cfg.PROVIDERS.items() if n != "anthropic"}, {"openai"})

    def test_keychain_roundtrip_never_puts_key_on_the_command_line(self):
        sec = FakeSecurity()
        store = cfg.KeyStore(runner=sec, use_keychain=True)
        store.set("anthropic", "sk-ant-secret-1234")
        self.assertEqual(store.get("anthropic"), ("sk-ant-secret-1234", "Keychain"))
        self.assertTrue(all("sk-ant-secret-1234" not in " ".join(c) for c in sec.calls))  # never in argv / `ps`
        store.delete("anthropic")
        self.assertEqual(store.get("anthropic"), (None, None))

    def test_file_fallback_is_private(self):
        store = cfg.KeyStore(use_keychain=False)
        store.set("openai", "sk-proj-abc")
        path = os.path.join(self.tmp.name, "keys.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(store.get("openai"), ("sk-proj-abc", "file ~/.inaproc/keys.json"))

    def test_env_var_wins(self):
        store = cfg.KeyStore(use_keychain=False)
        store.set("deepseek", "from-file")
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "from-env"}):
            self.assertEqual(store.get("deepseek"), ("from-env", "env DEEPSEEK_API_KEY"))

    def test_settings_saved_without_keys(self):
        cfg.save_settings({"provider": "openrouter", "model": "anthropic/claude-opus-5-5"})
        doc = json.load(open(os.path.join(self.tmp.name, "config.json")))
        self.assertEqual(doc["provider"], "openrouter")
        self.assertNotIn("key", json.dumps(doc).lower().replace("api_key_source", ""))
        self.assertEqual(cfg.load_settings()["model"], "anthropic/claude-opus-5-5")


if __name__ == "__main__":
    unittest.main()


class SafetyNetTest(unittest.TestCase):
    def test_tests_never_use_the_real_home_or_keychain(self):
        self.assertNotEqual(os.path.realpath(cfg.home()),
                            os.path.realpath(os.path.join(os.path.expanduser("~"), ".inaproc")))
        self.assertFalse(cfg.KeyStore().keychain)
