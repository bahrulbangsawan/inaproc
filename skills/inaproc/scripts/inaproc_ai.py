"""AI mode for the standalone CLI: the user's own API key drives the inaproc commands as tools.

Two adapters over raw HTTPS (the project has a no-dependency rule): the Anthropic Messages API, and the
OpenAI-compatible chat completions API used by OpenAI, OpenRouter and DeepSeek. The model gets two tools:
`inaproc` (run a CLI command) and `apply_plan` (send an approved plan — the CLI itself asks the user y/N in the
terminal, so no model can change products on its own).
"""
import datetime as _dt
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

import inaproc_config as cfg

ANTHROPIC_VERSION = "2026-10-21"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5"}
MAX_TURNS = 24
OUTPUT_LIMIT = 30000
SCRIPT = pathlib.Path(__file__).resolve().parent / "inaproc.py"
BLOCKED = [("penyedia", "apply"), ("penyedia", "record"), ("penyedia", "login"), ("penyedia", "logout"),
           ("update",), ("discover",), ("login",), ("logout",), ("setup",), ("ai",), ("ask",)]

TOOLS = [
    {"name": "inaproc",
     "description": ("Run one inaproc CLI command and get its printed output. `args` are the command and options "
                     "exactly as on the command line, without the program name, e.g. [\"cheapest\", \"kertas hvs a4\", "
                     "\"--top\", \"5\"] or [\"penyedia\", \"products\", \"--search\", \"akrilik\"]. Add \"--json\" "
                     "when you need exact fields. Writes (penyedia product create/update/delete, bulk-update) only "
                     "make a dry-run plan; use apply_plan to send one."),
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["args"],
                      "properties": {"args": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "apply_plan",
     "description": ("Send a dry-run plan made by a penyedia write command. The terminal shows the plan to the user "
                     "and asks them to confirm; nothing is sent unless they say yes. Call it only after you showed "
                     "the dry run in your reply and the user asked to send it."),
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["plan"],
                      "properties": {"plan": {"type": "string", "description": "path printed after `penyedia apply`"}}}},
]


class AIError(Exception):
    exit_code = 1


def http_json(method, url, headers, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict(headers, **({"content-type": "application/json"} if data else {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        raise AIError(f"tidak bisa menghubungi {url.split('/')[2]}: {getattr(e, 'reason', e)}")


def _headers(provider, key):
    p = cfg.PROVIDERS[provider]
    if p["kind"] == "anthropic":
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    h = {"Authorization": f"Bearer {key}"}
    if provider == "openrouter":
        h.update({"HTTP-Referer": "https://github.com/bahrulbangsawan/inaproc", "X-Title": "inaproc CLI"})
    return h


def _raise_for(status, doc, provider):
    if status == 200:
        return
    msg = ((doc or {}).get("error") or {})
    msg = msg.get("message") if isinstance(msg, dict) else str(msg)
    label = cfg.PROVIDERS[provider]["label"]
    if status in (401, 403):
        raise AIError(f"{label} menolak API key ({msg or status}). Jalankan `inaproc setup` untuk memasukkan key baru.")
    if status == 404:
        raise AIError(f"{label}: model atau alamat tidak ditemukan ({msg}). Pilih model lain lewat `inaproc setup`.")
    if status == 429:
        raise AIError(f"{label}: batas pemakaian tercapai atau saldo habis ({msg}). Coba lagi sebentar lagi.")
    raise AIError(f"{label}: HTTP {status} {msg or ''}".strip())


def list_models(provider, key, http=None):
    http = http or http_json
    p = cfg.PROVIDERS[provider]
    url = p["base_url"] + ("/v1/models" if p["kind"] == "anthropic" else "/models")
    status, doc = http("GET", url, _headers(provider, key))
    _raise_for(status, doc, provider)
    return [m["id"] for m in doc.get("data") or [] if m.get("id")]


def system_prompt(cwd=None):
    skill = pathlib.Path(__file__).resolve().parent.parent / "SKILL.md"
    try:
        text = skill.read_text(encoding="utf-8")
        text = text.split("---", 2)[2] if text.startswith("---") else text
    except OSError:
        text = ""
    notes = f"""
# You are running inside the standalone `inaproc` CLI

- The user types in a terminal. Today is {_dt.date.today().isoformat()}. Working folder: {cwd or os.getcwd()}.
- Run commands with the `inaproc` tool (args exactly as on the command line, without `python3 inaproc.py`).
  Where SKILL.md says `<this-skill-dir>/scripts/inaproc.py <command>`, call the tool with `[<command>, ...]`.
- For file exports ask the format and folder first, as SKILL.md says; "folder ini" means the working folder above.
- For penyedia writes: run the dry run, show what changes in your reply, and only when the user says to send it,
  call `apply_plan` with the plan path. The terminal asks the user to confirm; respect a "no".
- If a command reports a Safari setup problem or that the user is not signed in, explain the steps from the
  error in plain words and stop; the CLI shows a full guide (`inaproc doctor`).
- Reply in the user's language (usually Bahasa Indonesia), short and terminal friendly: lead with the answer,
  then a compact markdown table, then links. Never invent prices or data the tools did not return.
"""
    return text.strip() + "\n" + notes


class ToolRunner:
    """Executes tool calls for the agent. `executor(args) -> (code, stdout, stderr)`; `confirm(plan_doc) -> bool`."""

    def __init__(self, executor=None, confirm=None, on_start=None, on_end=None):
        self.executor = executor or (lambda argv: run_cli(argv))
        self.confirm = confirm
        self.on_start = on_start or (lambda label: None)
        self.on_end = on_end or (lambda label, ok, detail: None)

    def run(self, name, args):
        if name == "inaproc":
            argv = args.get("args") if isinstance(args, dict) else None
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                return "error: `args` must be a non-empty list of strings", True
            words = [a for a in argv if not a.startswith("-")]
            for blocked in BLOCKED:
                if tuple(words[:len(blocked)]) == blocked:
                    hint = "use the apply_plan tool" if blocked == ("penyedia", "apply") else \
                        "the user runs this one themselves"
                    return f"error: `{' '.join(blocked)}` is not available to the assistant ({hint})", True
            label = "inaproc " + " ".join(argv)
            self.on_start(label)
            code, out, err = self.executor(argv)
            text = (out or "") + (("\n[stderr]\n" + err[-4000:]) if err and err.strip() else "")
            if len(text) > OUTPUT_LIMIT:
                text = text[:OUTPUT_LIMIT] + f"\n…[output cut at {OUTPUT_LIMIT} characters]"
            self.on_end(label, code == 0, f"{len((out or '').splitlines())} baris" if code == 0 else (err or "").strip()[-160:])
            return (text or "(no output)") + ("" if code == 0 else f"\n[exit code {code}]"), code != 0
        if name == "apply_plan":
            plan = str((args or {}).get("plan") or "")
            plans_dir = (cfg.home() / "penyedia").resolve()
            try:
                path = pathlib.Path(plan).expanduser().resolve()
            except (OSError, RuntimeError):
                return "error: bad plan path", True
            if path.parent != plans_dir or not path.name.startswith("plan_") or not path.is_file():
                return f"error: plan must be a plan_*.json file in {plans_dir}", True
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return "error: the plan file cannot be read", True
            if not (self.confirm and self.confirm(doc, str(path))):
                return "The user said no in the terminal. Nothing was sent.", False
            label = "inaproc penyedia apply " + path.name
            self.on_start(label)
            code, out, err = self.executor(["penyedia", "apply", str(path)])
            self.on_end(label, code == 0, "terkirim" if code == 0 else (err or "").strip()[-160:])
            return (out or "") + (("\n" + err) if err else ""), code != 0
        return f"error: unknown tool {name}", True


def run_cli(argv, timeout=900):
    env = dict(os.environ, INAPROC_PLAIN="1", INAPROC_AUTO_UPDATE="0")
    try:
        p = subprocess.run([sys.executable, str(SCRIPT)] + list(argv), capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


class Agent:
    def __init__(self, provider, key, model, runner, http=None, system=None, on_think=None, effort=None):
        self.provider, self.key, self.model = provider, key, model
        self.kind = cfg.PROVIDERS[provider]["kind"]
        self.runner, self._http = runner, http
        self.system = system if system is not None else system_prompt()
        self.on_think = on_think or (lambda turn: None)
        self.effort = effort
        self.messages = []
        self.usage = {"input": 0, "output": 0}

    def http(self, *a, **k):
        return (self._http or http_json)(*a, **k)

    def reset(self):
        self.messages = []

    def send(self, text):
        if self.kind == "anthropic":
            return self._send_anthropic(text)
        return self._send_openai(text)

    # Anthropic Messages API ---------------------------------------------------------------------------------
    def _send_anthropic(self, text):
        self.messages.append({"role": "user", "content": text})
        headers = _headers(self.provider, self.key)
        tools = [dict(t) for t in TOOLS]
        for turn in range(MAX_TURNS):
            body = {"model": self.model, "max_tokens": 16000, "system": self.system, "messages": self.messages,
                    "tools": tools}
            if self.effort:
                body["output_config"] = {"effort": self.effort}
            h = dict(headers)
            if self.model in FALLBACK_MODELS:
                body["fallbacks"] = "default"
                h["anthropic-beta"] = FALLBACK_BETA
            self.on_think(turn)
            status, doc = self.http("POST", cfg.PROVIDERS[self.provider]["base_url"] + "/v1/messages", h, body)
            _raise_for(status, doc, self.provider)
            u = doc.get("usage") or {}
            self.usage["input"] += u.get("input_tokens", 0)
            self.usage["output"] += u.get("output_tokens", 0)
            content = doc.get("content") or []
            stop = doc.get("stop_reason")
            self.messages.append({"role": "assistant", "content": content})  # echoed unchanged (thinking kept)
            said = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            if stop == "refusal":
                cat = (doc.get("stop_details") or {}).get("category")
                return (said + "\n\n" if said else "") + f"(Model menolak permintaan ini{f' — kategori {cat}' if cat else ''}.)"
            if stop == "pause_turn":
                continue
            calls = [b for b in content if b.get("type") == "tool_use"]
            if stop == "tool_use" and calls:
                results = []
                for c in calls:
                    out, err = self.runner.run(c.get("name"), c.get("input") or {})
                    results.append({"type": "tool_result", "tool_use_id": c["id"], "content": out, "is_error": err})
                self.messages.append({"role": "user", "content": results})
                continue
            if stop == "max_tokens":
                said += "\n\n(Jawaban terpotong karena terlalu panjang.)"
            return said or "(tidak ada jawaban)"
        return "(Berhenti: terlalu banyak langkah dalam satu permintaan.)"

    # OpenAI-compatible chat completions ------------------------------------------------------------------------
    def _send_openai(self, text):
        if not self.messages:
            self.messages.append({"role": "system", "content": self.system})
        self.messages.append({"role": "user", "content": text})
        tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                   "parameters": t["input_schema"]}} for t in TOOLS]
        url = cfg.PROVIDERS[self.provider]["base_url"] + "/chat/completions"
        for turn in range(MAX_TURNS):
            self.on_think(turn)
            status, doc = self.http("POST", url, _headers(self.provider, self.key),
                                    {"model": self.model, "messages": self.messages, "tools": tools})
            _raise_for(status, doc, self.provider)
            u = doc.get("usage") or {}
            self.usage["input"] += u.get("prompt_tokens", 0)
            self.usage["output"] += u.get("completion_tokens", 0)
            choice = (doc.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            keep = {"role": "assistant", "content": msg.get("content")}
            if msg.get("tool_calls"):
                keep["tool_calls"] = msg["tool_calls"]
            if msg.get("reasoning_content") is not None:
                keep["reasoning_content"] = msg["reasoning_content"]
            self.messages.append(keep)
            calls = msg.get("tool_calls") or []
            if calls:
                for c in calls:
                    fn = c.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                        out, err = self.runner.run(fn.get("name"), args)
                    except ValueError:
                        out = "error: the tool arguments were not valid JSON; send them again"
                    self.messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": out})
                continue
            said = (msg.get("content") or "").strip()
            if choice.get("finish_reason") == "length":
                said += "\n\n(Jawaban terpotong karena terlalu panjang.)"
            return said or "(tidak ada jawaban)"
        return "(Berhenti: terlalu banyak langkah dalam satu permintaan.)"
