"""Settings and API keys for the standalone CLI's AI mode.

Keys live in the macOS Keychain (service "inaproc") when available, else in ~/.inaproc/keys.json (0600).
An environment variable (ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, DEEPSEEK_API_KEY) wins over both.
A key is never put on a command line (the Keychain write goes through `security -i` on stdin) and never printed.
"""
import json
import os
import pathlib
import platform
import shlex
import shutil
import subprocess

PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "kind": "anthropic", "base_url": "https://api.anthropic.com",
                  "env": "ANTHROPIC_API_KEY", "default_model": "claude-opus-5-5",
                  "key_hint": "console.anthropic.com → API Keys (diawali sk-ant-)"},
    "openai": {"label": "OpenAI (GPT)", "kind": "openai", "base_url": "https://api.openai.com/v1",
               "env": "OPENAI_API_KEY", "default_model": None,
               "key_hint": "platform.openai.com → API keys (diawali sk-)"},
    "openrouter": {"label": "OpenRouter (banyak model, satu key)", "kind": "openai",
                   "base_url": "https://openrouter.ai/api/v1", "env": "OPENROUTER_API_KEY",
                   "default_model": "anthropic/claude-opus-5-5",
                   "key_hint": "openrouter.ai → Keys (diawali sk-or-)"},
    "deepseek": {"label": "DeepSeek", "kind": "openai", "base_url": "https://api.deepseek.com",
                 "env": "DEEPSEEK_API_KEY", "default_model": "deepseek-chat",
                 "key_hint": "platform.deepseek.com → API keys (diawali sk-)"},
}
SERVICE = "inaproc"


def home():
    p = pathlib.Path(os.environ.get("INAPROC_HOME") or (pathlib.Path.home() / ".inaproc")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def load_settings():
    try:
        with open(home() / "config.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    clean = {k: v for k, v in settings.items() if "key" not in k.lower() or k == "api_key_source"}
    _write_private(home() / "config.json", clean)


class KeyStore:
    def __init__(self, runner=subprocess.run, use_keychain=None):
        self.run = runner
        if use_keychain is None:
            use_keychain = platform.system() == "Darwin" and shutil.which("security") is not None
        self.keychain = use_keychain

    def _file(self):
        return home() / "keys.json"

    def _file_data(self):
        try:
            with open(self._file(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def get(self, provider):
        """(key, where it came from) or (None, None)."""
        env = PROVIDERS[provider]["env"]
        if os.environ.get(env):
            return os.environ[env], f"env {env}"
        if self.keychain:
            p = self.run(["security", "find-generic-password", "-s", SERVICE, "-a", provider, "-w"],
                         capture_output=True, text=True, timeout=10)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip(), "Keychain"
        key = self._file_data().get(provider)
        return (key, "file ~/.inaproc/keys.json") if key else (None, None)

    def set(self, provider, key):
        if self.keychain:
            cmd = (f"add-generic-password -U -s {shlex.quote(SERVICE)} -a {shlex.quote(provider)} "
                   f"-l {shlex.quote('inaproc ' + provider + ' API key')} -w {shlex.quote(key)}\n")
            p = self.run(["security", "-i"], capture_output=True, text=True, timeout=10, input=cmd)
            if p.returncode == 0:
                return "Keychain"
        data = self._file_data()
        data[provider] = key
        _write_private(self._file(), data)
        return "file ~/.inaproc/keys.json"

    def delete(self, provider):
        if self.keychain:
            self.run(["security", "delete-generic-password", "-s", SERVICE, "-a", provider],
                     capture_output=True, text=True, timeout=10)
        data = self._file_data()
        if data.pop(provider, None) is not None:
            _write_private(self._file(), data)
