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


class KeyStoreError(Exception):
    exit_code = 1


LOCKED = ("Keychain macOS terkunci atau tidak bisa dibuka dari sesi ini (misalnya lewat SSH). Buka kunci Mac Anda "
          "lalu coba lagi, atau pakai variabel lingkungan API key untuk sesi ini.")


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
    # Never a secret in this file: drop any single "…key" value. The "keys" list holds names/models only.
    clean = {k: v for k, v in settings.items() if k == "keys" or "key" not in k.lower()}
    for e in clean.get("keys", []):
        for k in [k for k in e if "key" in k.lower() or k in ("secret", "token")]:
            e.pop(k)
    _write_private(home() / "config.json", clean)


class KeyStore:
    def __init__(self, runner=subprocess.run, use_keychain=None):
        self.run = runner
        if use_keychain is None:
            use_keychain = (platform.system() == "Darwin" and shutil.which("security") is not None
                            and os.environ.get("INAPROC_NO_KEYCHAIN") != "1")
        self.keychain = use_keychain

    def _file(self):
        return home() / "keys.json"

    def _file_data(self):
        try:
            with open(self._file(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _security(self, argv, **kw):
        try:
            p = self.run(argv, capture_output=True, text=True, timeout=30, **kw)
        except subprocess.TimeoutExpired:
            raise KeyStoreError(LOCKED)
        if p.returncode != 0 and "interaction" in (p.stderr or "").lower():
            raise KeyStoreError(LOCKED)
        return p

    def get(self, provider):
        """(key, where it came from) or (None, None)."""
        env = PROVIDERS[provider]["env"]
        if os.environ.get(env):
            return os.environ[env], f"env {env}"
        if self.keychain:
            p = self._security(["security", "find-generic-password", "-s", SERVICE, "-a", provider, "-w"])
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip(), "Keychain"
        key = self._file_data().get(provider)
        return (key, "file ~/.inaproc/keys.json") if key else (None, None)

    def set(self, provider, key):
        if self.keychain:
            cmd = (f"add-generic-password -U -s {shlex.quote(SERVICE)} -a {shlex.quote(provider)} "
                   f"-l {shlex.quote('inaproc ' + provider + ' API key')} -w {shlex.quote(key)}\n")
            p = self._security(["security", "-i"], input=cmd)
            if p.returncode == 0:
                return "Keychain"
        data = self._file_data()
        data[provider] = key
        _write_private(self._file(), data)
        return "file ~/.inaproc/keys.json"

    # raw accounts (used by Profiles: "<provider>:<label>") ------------------------------------------------------
    def get_account(self, account):
        if self.keychain:
            p = self._security(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"])
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        return self._file_data().get(account)

    def set_account(self, account, key):
        if self.keychain:
            cmd = (f"add-generic-password -U -s {shlex.quote(SERVICE)} -a {shlex.quote(account)} "
                   f"-l {shlex.quote('inaproc API key ' + account)} -w {shlex.quote(key)}\n")
            if self._security(["security", "-i"], input=cmd).returncode == 0:
                return "Keychain"
        data = self._file_data()
        data[account] = key
        _write_private(self._file(), data)
        return "file ~/.inaproc/keys.json"

    def delete_account(self, account):
        if self.keychain:
            self._security(["security", "delete-generic-password", "-s", SERVICE, "-a", account])
        data = self._file_data()
        if data.pop(account, None) is not None:
            _write_private(self._file(), data)

    def delete(self, provider):
        if self.keychain:
            self._security(["security", "delete-generic-password", "-s", SERVICE, "-a", provider])
        data = self._file_data()
        if data.pop(provider, None) is not None:
            _write_private(self._file(), data)


def _account(provider, label):
    return f"{provider}:{label}"


class Profiles:
    """API keys in priority order. Several keys per provider and several providers are allowed.

    config.json keeps only {provider, label, model, priority, enabled, status}; each secret lives in the key store
    under the account "<provider>:<label>". Keys found in environment variables are listed last, marked `env`."""

    def __init__(self, store=None):
        self.store = store or KeyStore()
        self.settings = load_settings()
        self._migrate()

    # storage -------------------------------------------------------------------------------------------------
    def _entries(self):
        return self.settings.setdefault("keys", [])

    def _save(self):
        for i, e in enumerate(sorted(self._entries(), key=lambda x: x.get("priority", 99)), 1):
            e["priority"] = i
        self.settings["keys"] = sorted(self._entries(), key=lambda x: x["priority"])
        self.settings.pop("provider", None)
        self.settings.pop("model", None)
        save_settings(self.settings)

    def _migrate(self):
        old_p, old_m = self.settings.get("provider"), self.settings.get("model")
        if old_p in PROVIDERS and old_m and not self._entries():
            secret = self.store.get_account(old_p)
            if secret:
                self.store.set_account(_account(old_p, "utama"), secret)
                self._entries().append({"provider": old_p, "label": "utama", "model": old_m, "priority": 1,
                                        "enabled": True})
                self._save()

    # queries ---------------------------------------------------------------------------------------------------
    def find(self, ref):
        """By label, or by 1-based position in the priority list."""
        entries = self.ordered()
        if str(ref).isdigit() and 1 <= int(ref) <= len(entries):
            return entries[int(ref) - 1]
        for e in entries:
            if e["label"] == ref:
                return e
        return None

    def ordered(self, active_only=False):
        out = [dict(e, source=e.get("source") or "tersimpan") for e in
               sorted(self._entries(), key=lambda x: x.get("priority", 99))]
        for name, p in PROVIDERS.items():
            if os.environ.get(p["env"]):
                out.append({"provider": name, "label": f"env-{name}", "model": p["default_model"] or "",
                            "priority": len(out) + 1, "enabled": True, "source": f"env {p['env']}", "env": True})
        if active_only:
            out = [e for e in out if e.get("enabled", True) and e.get("model")]
        return out

    def secret(self, entry):
        if entry.get("env"):
            return os.environ.get(PROVIDERS[entry["provider"]]["env"])
        return self.store.get_account(_account(entry["provider"], entry["label"]))

    # changes ---------------------------------------------------------------------------------------------------
    def add(self, provider, key, model, label=None, priority=None):
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider}")
        taken = {e["label"] for e in self._entries()}
        label = label or f"{provider}-{sum(1 for e in self._entries() if e['provider'] == provider) + 1}"
        base, n = label, 2
        while label in taken:
            label, n = f"{base}-{n}", n + 1
        where = self.store.set_account(_account(provider, label), key)
        entry = {"provider": provider, "label": label, "model": model, "enabled": True,
                 "priority": (priority - 0.5) if priority else len(self._entries()) + 1}
        self._entries().append(entry)
        self._save()
        return dict(entry, where=where)

    def _raw(self, ref):
        e = self.find(ref)
        if not e or e.get("env"):
            raise KeyError(f"tidak ada key tersimpan dengan nama/nomor {ref!r}")
        for raw in self._entries():
            if raw["label"] == e["label"]:
                return raw
        raise KeyError(ref)

    def remove(self, ref):
        raw = self._raw(ref)
        self.store.delete_account(_account(raw["provider"], raw["label"]))
        self.settings["keys"] = [e for e in self._entries() if e is not raw]
        self._save()

    def move(self, ref, position):
        raw = self._raw(ref)
        raw["priority"] = position - 0.5 if position <= raw["priority"] else position + 0.5
        self._save()

    def set_model(self, ref, model):
        self._raw(ref)["model"] = model
        self._save()

    def set_enabled(self, ref, enabled):
        self._raw(ref)["enabled"] = bool(enabled)
        self._save()

    def mark(self, entry, ok, kind=None):
        """Remember the last result of a key (shown in `inaproc keys`)."""
        if entry.get("env"):
            return
        import datetime as _dt
        for raw in self._entries():
            if raw["label"] == entry["label"]:
                now = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
                if ok:
                    raw.update(lastOkAt=now)
                    raw.pop("lastError", None)
                else:
                    raw.update(lastError=kind or "error", lastErrorAt=now)
                self._save()
                return
