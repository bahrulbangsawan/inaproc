#!/usr/bin/env python3
"""inaproc — the standalone command-line app.

    inaproc                      home screen with a guided menu
    inaproc setup                choose an AI provider, paste the API key, pick a model
    inaproc ai                   chat with the AI assistant (it runs searches, comparisons, RAB, your products…)
    inaproc ask "…"              one question to the AI;  inaproc "carikan kertas termurah" does the same
    inaproc whoami | login | doctor
    inaproc <any skill command>  e.g. inaproc cheapest "kertas hvs a4" --top 5  (drawn as tables)

Standard library only; it wraps scripts/inaproc.py and shares its auto-update.
"""
import contextlib
import io
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import inaproc_config as cfg  # noqa: E402
import inaproc_ui as ui  # noqa: E402

PENYEDIA_URL = "https://penyedia.inaproc.id/products"
AUTOMATION_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
APP_COMMANDS = {"setup", "ai", "chat", "ask", "whoami", "login", "doctor", "help", "menu", "keys", "model", "tui"}


# ----------------------------------------------------------------------------------------------- helpers
def _version():
    import inaproc
    return inaproc.VERSION


def _store():
    return cfg.KeyStore()


def _key_for(provider):
    return _store().get(provider)


def _profiles():
    return cfg.Profiles(_store())


def _ai_settings():
    """The key used first (highest priority, active), or None."""
    act = _profiles().ordered(active_only=True)
    return act[0] if act else None


def _skill_commands():
    import inaproc
    p = inaproc.build_parser()
    for a in p._actions:
        if getattr(a, "choices", None) and isinstance(a.choices, dict):
            return set(a.choices)
    return set()


def _interactive():
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _terminal_name():
    return {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "vscode": "Visual Studio Code",
            "WarpTerminal": "Warp", "ghostty": "Ghostty"}.get(os.environ.get("TERM_PROGRAM", ""),
                                                                "aplikasi terminal Anda (mis. Terminal/iTerm/Sawit)")


def _open(target, app_name=None):
    if platform.system() != "Darwin":
        return False
    cmd = ["open"] + (["-a", app_name] if app_name else []) + ([target] if target else [])
    return subprocess.run(cmd, capture_output=True).returncode == 0


MD_TABLE_SEP = re.compile(r"^\|\s*:?-{2,}")


def _inline(cell):
    """Table cells: drop markdown emphasis/code marks (the box table already sets the look)."""
    return re.sub(r"`([^`]+)`", r"\1", re.sub(r"\*\*(.+?)\*\*", r"\1", cell))


def pretty(text, color=None):
    """Markdown from the skill commands -> terminal: tables become box tables, **bold**/headings styled."""
    out, block = [], []

    def flush():
        if not block:
            return
        rows = [[_inline(c.strip()) for c in l.strip().strip("|").split("|")] for l in block
                if not MD_TABLE_SEP.match(l)]
        if rows:
            out.append(ui.auto_table(rows[0], rows[1:], color=color))
        block.clear()

    for line in text.splitlines():
        if line.startswith("|"):
            block.append(line)
            continue
        flush()
        out.append(ui.render_markdown(line, color=color))
    flush()
    return "\n".join(out)


# ----------------------------------------------------------------------------------------------- Safari guides
def guide_for(error_text):
    """(kind, title, steps, action) for a known penyedia/Safari problem, else None."""
    t = error_text or ""
    if "Allow JavaScript from Apple Events" in t or "Develop" in t and "Safari" in t:
        return ("warn", "Safari perlu diizinkan (sekali saja)", [
            "1. Buka Safari.",
            "2. Menu Safari ▸ Settings… (⌘ ,) ▸ tab Advanced.",
            "3. Centang \"Show features for web developers\".",
            "4. Di menu bar sekarang ada menu Develop ▸ pilih Developer Settings…",
            "5. Centang \"Allow JavaScript from Apple Events\" (Safari meminta password Mac Anda).",
            "6. Kembali ke sini dan jalankan perintah tadi lagi.",
            "Kenapa? inaproc memakai tab penyedia di Safari Anda sendiri, jadi password dan centang Cloudflare "
            "tetap di Safari; pengaturan ini mengizinkan terminal menjalankan perintah di tab itu."], ("app", "Safari"))
    if "Automation" in t or "control Safari" in t or "-1743" in t:
        return ("warn", "Izinkan terminal mengendalikan Safari", [
            "1. Buka System Settings ▸ Privacy & Security ▸ Automation.",
            f"2. Cari {_terminal_name()} dan nyalakan sakelar Safari di bawahnya.",
            "3. Jika tidak ada di daftar: jalankan perintah tadi lagi dan klik OK saat macOS bertanya.",
            "4. Jalankan perintah tadi lagi."], ("url", AUTOMATION_URL))
    if "did not answer in time" in t:
        return ("warn", "Safari belum menjawab — kemungkinan ada dialog izin", [
            f"1. macOS kemungkinan sedang bertanya: \"{_terminal_name()} ingin mengendalikan Safari\". Cari dialog "
            "itu (bisa tertutup jendela lain) dan klik OK.",
            "2. Jika tidak ada dialog: buka System Settings ▸ Privacy & Security ▸ Automation, cari aplikasi "
            "terminal Anda, nyalakan Safari.",
            "3. Pastikan Safari tidak sedang menampilkan dialog/peringatan sendiri.",
            "4. Jalankan perintah tadi lagi."], ("url", AUTOMATION_URL))
    if "not signed in" in t or "signed out" in t or "refused the call" in t:
        return ("info", "Anda belum login di penyedia.inaproc.id", [
            "1. Jalankan:  inaproc login   (Safari terbuka di halaman penyedia).",
            "2. Masuk dengan akun INAPROC Anda dan centang Cloudflare seperti biasa.",
            "3. inaproc menunggu sampai login selesai, lalu Anda bisa lanjut.",
            "Password tidak pernah dibaca atau disimpan oleh inaproc."], ("login", None))
    if "osascript not found" in t or "only works on macOS" in t:
        return ("info", "Mode Safari hanya ada di macOS", [
            "Di Linux/Windows pakai Chrome:  inaproc penyedia … --browser chrome",
            "Login dulu dengan:  inaproc penyedia login --browser chrome"], None)
    return None


def show_guide(error_text, retry=None):
    g = guide_for(error_text)
    if not g:
        return False
    kind, title, steps, action = g
    ui.panel(title, steps, kind=kind)
    if action and _interactive():
        what, target = action
        if what == "app" and ui.confirm("Buka Safari sekarang?", default=True):
            _open(None, "Safari")
        elif what == "url" and ui.confirm("Buka pengaturan Automation sekarang?", default=True):
            _open(target)
        elif what == "login" and ui.confirm("Login sekarang?", default=True):
            return cmd_login([]) == 0 and bool(retry) and retry()
        if retry and what in ("app", "url"):
            ui.ask("Tekan Enter setelah selesai untuk mencoba lagi", default="")
            return retry()
    return False


# ----------------------------------------------------------------------------------------------- run a skill command
def run_skill(argv, title=None):
    """Run an inaproc.py command with a live step list, then print its output as tables."""
    import inaproc
    want_raw = "--json" in argv or not sys.stdout.isatty() and os.environ.get("INAPROC_PRETTY") != "1"
    out, err = io.StringIO(), io.StringIO()
    label = title or ("inaproc " + " ".join(argv))
    code = 1
    with ui.Steps() as steps:
        steps.start(label)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = inaproc.main(argv)
        except SystemExit as e:  # argparse --help / usage errors
            code = e.code if isinstance(e.code, int) else 1
        if code == 0:
            steps.done()
        else:
            steps.fail("gagal" if code != 2 else "perintah tidak lengkap")
    text = out.getvalue()
    if text:
        print(text.rstrip("\n") if want_raw else pretty(text))
    errors = err.getvalue()
    if errors.strip():
        guided = code != 0 and guide_for(errors) is not None
        if guided and show_guide(errors, retry=lambda: run_skill(argv, title) == 0):
            return 0
        for line in errors.rstrip().splitlines():
            if guided and line.startswith("error:"):
                continue  # the guide above already explains it
            if line.startswith("error:"):
                print(ui.Style(sys.stderr).red("✗ ") + line[6:].strip(), file=sys.stderr)
            elif line.strip():
                print(ui.Style(sys.stderr).gray(line), file=sys.stderr)
    return code


# ----------------------------------------------------------------------------------------------- setup
CHAT_EXCLUDE = re.compile(r"(moderation|embedding|audio|realtime|image|tts|transcribe|search|instruct|codex|-pro\b|"
                          r"dall-e|whisper|babbage|davinci|computer-use|deep-research)", re.I)


def _vkey(model_id):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", model_id)]


def model_choices(provider, models):
    """Chat-capable models, the recommended one first."""
    ids = [m for m in models if not CHAT_EXCLUDE.search(m)]
    default = cfg.PROVIDERS[provider]["default_model"]
    if provider == "openai":
        ids = sorted([m for m in ids if re.match(r"^(gpt-\d|gpt-4o|o\d)", m)], key=_vkey, reverse=True)
        plain = [m for m in ids if re.match(r"^gpt-\d+(\.\d+)?$", m)]
        if plain:
            ids.remove(plain[0])
            ids.insert(0, plain[0])
    elif provider == "openrouter":
        opus = sorted([m for m in ids if m.startswith("anthropic/claude-opus")], key=_vkey, reverse=True)
        others = [m for m in ids if m.startswith(("anthropic/claude", "openai/gpt", "deepseek/", "google/gemini"))
                  and m not in opus]
        ids = opus[:1] + sorted(others, key=_vkey, reverse=True)
    elif provider == "anthropic":
        ids = [m for m in ids if m.startswith("claude")]
    if default and default in ids:
        ids.remove(default)
        ids.insert(0, default)
    return ids[:15]


def _pick_model(provider, models):
    ids = model_choices(provider, models)
    if not ids:
        return ui.ask("Nama model (daftar dari provider kosong)") or (cfg.PROVIDERS[provider]["default_model"] or "")
    labels = [m + ("  (disarankan)" if i == 0 else "") for i, m in enumerate(ids)] + ["Ketik nama model lain…"]
    i = ui.choose("Pilih model", labels, default=0)
    if i == len(ids):
        return ui.ask("Nama model") or ids[0]
    return ids[i]


KIND_TEXT = {"auth": "key ditolak", "quota": "saldo/kuota habis", "rate": "batas per menit", "model": "model tidak ada",
             "server": "server provider bermasalah", "network": "jaringan", "context": "percakapan terlalu panjang"}


def _flag(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 >= len(args):
            raise ValueError(f"{name} perlu nilai")
        return args[i + 1]
    return None


def cmd_keys_add(args):
    import inaproc_ai as ai
    st = ui.Style()
    try:
        provider, model, label = _flag(args, "--provider"), _flag(args, "--model"), _flag(args, "--label")
        priority = _flag(args, "--priority")
    except ValueError as e:
        print(f"{e}, mis.  inaproc keys add --provider anthropic --model claude-opus-5-5", file=sys.stderr)
        return 2
    names = list(cfg.PROVIDERS)
    if provider and provider not in cfg.PROVIDERS:
        print(f"Provider tidak dikenal: {provider}. Pilihan: {', '.join(names)}", file=sys.stderr)
        return 2
    profiles = _profiles()
    if not provider:
        ui.panel("Tambah API key", [
            "Mode AI memakai API key Anda sendiri; biaya ditagih oleh provider.",
            "Anda bisa menyimpan beberapa key (sama atau beda provider). Jika satu key ditolak, saldonya habis atau",
            "provider bermasalah, inaproc otomatis pindah ke key berikutnya sesuai urutan prioritas.",
            "Key disimpan di " + ("Keychain macOS" if _store().keychain else "~/.inaproc/keys.json (hanya akun Anda)") +
            " dan tidak pernah ditampilkan utuh."])
        counts = {n: sum(1 for e in profiles.ordered() if e["provider"] == n and not e.get("env")) for n in names}
        labels = [cfg.PROVIDERS[n]["label"] + (st.green(f"  ✓ {counts[n]} key") if counts[n] else "") for n in names]
        provider = names[ui.choose("Provider AI mana?", labels, default=0)]
    p = cfg.PROVIDERS[provider]
    if "--key-stdin" in args:
        key = sys.stdin.readline().strip()
    else:
        print(st.gray(f"  Dapatkan key di {p['key_hint']}"))
        key = ui.ask(f"API key {p['label']}", secret=True)
    if not key:
        print(st.yellow("Tidak ada key. Batal."))
        return 1
    with ui.Steps() as steps:
        steps.start(f"Memeriksa API key ke {p['label']}")
        try:
            models = ai.verify_key(provider, key)
            steps.done(f"diterima · {len(models)} model tersedia")
        except ai.AIError as e:
            steps.fail(str(e))
            return 1
    if not model:
        model = _pick_model(provider, models)
    if not label and "--key-stdin" not in args:
        label = ui.ask("Nama key (mis. kantor, pribadi, cadangan)",
                       default=f"{provider}-{sum(1 for e in profiles.ordered() if e['provider'] == provider) + 1}")
    entry = profiles.add(provider, key, model, label=label or None,
                         priority=int(priority) if priority and priority.isdigit() else None)
    pos = next(i for i, e in enumerate(profiles.ordered(), 1) if e["label"] == entry["label"])
    ui.panel("Key tersimpan", [f"Nama      : {entry['label']}", f"Provider  : {p['label']}", f"Model     : {model}",
                               f"API key   : {ui.mask(key)}  ({entry['where']})", f"Prioritas : #{pos}", "",
                               "Lihat/urutkan semua key:  inaproc keys", "Ganti model:  inaproc model"], kind="ok")
    env = p["env"]
    if os.environ.get(env):
        print(st.gray(f"  Catatan: {env} juga diset di shell; key itu dipakai paling akhir sebagai cadangan."))
    return 0


def _keys_table(profiles):
    st = ui.Style()
    rows = []
    for i, e in enumerate(profiles.ordered(), 1):
        key = profiles.secret(e)
        if not e.get("enabled", True):
            status = st.gray("nonaktif")
        elif e.get("lastError"):
            status = st.red("✗ " + KIND_TEXT.get(e["lastError"], e["lastError"])) + st.gray(
                "  " + (e.get("lastErrorAt") or "")[5:16].replace("T", " "))
        elif e.get("lastOkAt"):
            status = st.green("✓ ok") + st.gray("  " + e["lastOkAt"][5:16].replace("T", " "))
        else:
            status = st.gray("belum dipakai")
        rows.append([f"#{i}", e["label"], cfg.PROVIDERS[e["provider"]]["label"], e.get("model") or "-",
                     ui.mask(key) if key else st.yellow("hilang"), status, e.get("source")])
    return ui.table(["Prioritas", "Nama", "Provider", "Model", "Key", "Status", "Sumber"], rows)


KEYS_HELP = """inaproc keys                      daftar key + urutan prioritas + status
inaproc keys add                  tambah key (wizard); atau: --provider P --model M --label L --key-stdin [--priority N]
inaproc keys move <nama|#> <N>     pindahkan ke prioritas N
inaproc keys model <nama|#> [M]    ganti model key itu (tanpa M: pilih dari daftar)
inaproc keys test                 uji semua key sekarang
inaproc keys on|off <nama|#>      aktifkan / nonaktifkan
inaproc keys remove <nama|#>      hapus key (juga dari Keychain)"""


def cmd_keys(args):
    import inaproc_ai as ai
    sub, rest = (args[0], args[1:]) if args else ("list", [])
    profiles = _profiles()
    try:
        if sub in ("list", "ls"):
            if not profiles.ordered():
                ui.panel("Belum ada API key", ["Tambahkan:  inaproc keys add"], kind="warn")
                return 0
            print(_keys_table(profiles))
            print(ui.Style().gray("  Dipakai berurutan dari #1; jika gagal (key ditolak, saldo habis, limit, server)"
                                  " otomatis pindah ke berikutnya."))
            return 0
        if sub in ("add", "tambah"):
            return cmd_keys_add(rest)
        if sub in ("remove", "rm", "hapus"):
            profiles.remove(rest[0])
            print(ui.Style().green("✓ ") + f"Key {rest[0]} dihapus.")
            return 0
        if sub in ("move", "pindah", "priority", "prioritas"):
            profiles.move(rest[0], int(rest[1]))
            print(_keys_table(_profiles()))
            return 0
        if sub in ("on", "off"):
            profiles.set_enabled(rest[0], sub == "on")
            print(_keys_table(_profiles()))
            return 0
        if sub == "model":
            return cmd_model(rest)
        if sub in ("test", "uji"):
            with ui.Steps() as steps:
                for e in profiles.ordered():
                    steps.start(f"{e['label']} · {cfg.PROVIDERS[e['provider']]['label']}")
                    try:
                        ai.verify_key(e["provider"], profiles.secret(e) or "")
                        profiles.mark(e, True)
                        steps.done("ok")
                    except ai.AIError as err:
                        profiles.mark(e, False, err.kind)
                        steps.fail(KIND_TEXT.get(err.kind, err.kind))
            print(_keys_table(_profiles()))
            return 0
        if sub in ("-h", "--help", "help"):
            print(KEYS_HELP)
            return 0
    except (KeyError, IndexError, ValueError) as e:
        print(ui.Style(sys.stderr).red("✗ ") + (str(e).strip("'\"") or "perintah tidak lengkap") +
              "\n" + KEYS_HELP, file=sys.stderr)
        return 2
    print(KEYS_HELP)
    return 2


def cmd_model(args):
    """inaproc model [nama|#] [MODEL] — change the model of a key (default: the top-priority key)."""
    import inaproc_ai as ai
    profiles = _profiles()
    if not profiles.ordered():
        ui.panel("Belum ada API key", ["Tambahkan dulu:  inaproc keys add"], kind="warn")
        return 1
    entry = profiles.find(args[0]) if args else None
    if args and not entry:
        print(f"Tidak ada key {args[0]!r}. Lihat:  inaproc keys", file=sys.stderr)
        return 2
    if not entry:
        saved = [e for e in profiles.ordered() if not e.get("env")]
        entry = saved[0] if len(saved) == 1 else saved[ui.choose(
            "Ganti model untuk key mana?", [f"{e['label']} · {cfg.PROVIDERS[e['provider']]['label']} · {e['model']}"
                                             for e in saved], default=0)]
    if len(args) >= 2:
        model = args[1]
    else:
        with ui.Steps() as steps:
            steps.start(f"Mengambil daftar model {cfg.PROVIDERS[entry['provider']]['label']}")
            models = ai.verify_key(entry["provider"], profiles.secret(entry) or "")
            steps.done(f"{len(models)} model")
        ids = model_choices(entry["provider"], models)
        if entry.get("model") in ids:
            ids.remove(entry["model"])
            ids.insert(0, entry["model"])
        labels = [m + ("  (sekarang)" if m == entry.get("model") else "") for m in ids] + ["Ketik nama model lain…"]
        i = ui.choose("Pilih model", labels, default=0)
        model = ui.ask("Nama model") if i == len(ids) else ids[i]
    profiles.set_model(entry["label"], model)
    print(ui.Style().green("✓ ") + f"Key {entry['label']} sekarang memakai {model}.")
    return 0


def cmd_setup(args):
    """First run: add a key. Later: a small menu over keys and models."""
    if any(a.startswith("--") for a in args) or not [e for e in _profiles().ordered() if not e.get("env")]:
        return cmd_keys_add(args)
    print(_keys_table(_profiles()))
    i = ui.choose("Pengaturan AI", ["Tambah API key", "Ganti model", "Ubah urutan prioritas", "Uji semua key",
                                    "Nonaktifkan / aktifkan key", "Hapus key", "Selesai"], default=6)
    if i == 0:
        return cmd_keys_add([])
    if i == 1:
        return cmd_model([])
    if i == 2:
        ref = ui.ask("Key mana (nama atau #)")
        pos = ui.ask("Jadi prioritas nomor", default="1")
        return cmd_keys(["move", ref, pos])
    if i == 3:
        return cmd_keys(["test"])
    if i == 4:
        ref = ui.ask("Key mana (nama atau #)")
        e = _profiles().find(ref)
        return cmd_keys(["off" if e and e.get("enabled", True) else "on", ref])
    if i == 5:
        ref = ui.ask("Key mana yang dihapus (nama atau #)")
        if ui.confirm(f"Hapus key {ref}?", default=False):
            return cmd_keys(["remove", ref])
    return 0


# ----------------------------------------------------------------------------------------------- AI
def _plan_lines(doc):
    """What each write in a plan will do, in words (shown before the user confirms)."""
    lines = []
    for w in doc.get("writes") or []:
        op = w.get("op", "")
        lines.append("• " + str(w.get("summary") or op))
        if op in ("createProduct", "createProductDraft"):
            inp = (w.get("variables") or {}).get("input") or {}
            mode = "DIBUAT dan LANGSUNG DIKIRIM ke kurasi LKPP" if op == "createProduct" else "disimpan sebagai DRAFT"
            lines.append(f"    mode: {mode}")
            for v in (inp.get("variants") or [])[:5]:
                lines.append(f"    varian: harga Rp{v.get('price')} · stok {v.get('stock')}" +
                             (f" · {v.get('sku')}" if v.get("sku") else ""))
            lines.append(f"    kategori: {(inp.get('category') or {}).get('id')} · gambar: {len(inp.get('images') or [])}")
        for path, old, new in (w.get("diff") or [])[:15]:
            lines.append(f"    {path}: {json.dumps(old, ensure_ascii=False)} → {json.dumps(new, ensure_ascii=False)}")
        if len(w.get("diff") or []) > 15:
            lines.append(f"    … {len(w['diff']) - 15} perubahan lain (lihat file rencana)")
    return lines


def _confirm_plan_factory(get_steps):
    def confirm(doc, path):
        s = get_steps()
        if s:
            s.done("menunggu persetujuan Anda")
        writes = doc.get("writes") or []
        lines = [f"Akun     : {doc.get('account')}", f"Jenis    : {doc.get('kind')}  ·  {len(writes)} perubahan", ""]
        body = _plan_lines(doc)
        lines += body[:60] + ([f"… (lihat file rencana untuk sisanya)"] if len(body) > 60 else [])
        lines += ["", f"Rencana: {path}"]
        ui.panel("AI ingin mengirim perubahan ke penyedia.inaproc.id", lines, kind="warn")
        if not _interactive():
            print("Tidak ada terminal interaktif untuk konfirmasi; tidak ada yang dikirim.", file=sys.stderr)
            return False
        return ui.confirm(f"Kirim {len(writes)} perubahan ini sekarang? (ketik y)", default=False, flush=True)
    return confirm


def _make_agent(holder, confirm=None):
    import inaproc_ai as ai
    profiles = _profiles()
    if not profiles.ordered(active_only=True):
        return None, "AI belum diatur. Tambahkan API key:  inaproc keys add"

    def think(turn):
        st = holder.get("steps")
        if st:
            st.start("AI sedang berpikir…" if turn == 0 else "AI menganalisis hasil…")

    def tool_start(label):
        st = holder.get("steps")
        if st:
            st.start("▶ " + label)

    def tool_end(label, ok, detail):
        st = holder.get("steps")
        if st:
            (st.done if ok else st.fail)(detail)

    def switch(frm, to, err):
        st = holder.get("steps")
        why = KIND_TEXT.get(err.kind, err.kind)
        if st:
            if st.label is None:
                st.start(f"Key {frm['label']}")
            st.fail(f"{frm['label']}: {why}")
            st.start(f"Beralih ke key {to['label']} ({cfg.PROVIDERS[to['provider']]['label']} · {to['model']})")
            st.done()

    runner = ai.ToolRunner(confirm=confirm or _confirm_plan_factory(lambda: holder.get("steps")),
                           on_start=tool_start, on_end=tool_end)
    agent = ai.FailoverAgent(profiles, runner=runner, on_think=think, on_switch=switch)
    return agent, None


def _answer(agent, holder, question):
    import inaproc_ai as ai
    with ui.Steps() as steps:
        holder["steps"] = steps
        try:
            answer = agent.send(question)
        except ai.AIError as e:
            steps.fail(str(e))
            return 1
        finally:
            holder["steps"] = None
        if steps.label is not None:
            steps.done()
    print()
    print(pretty(answer))
    u = agent.last_usage
    who = f"{agent.current['label']} · " if getattr(agent, "current", None) else ""
    print(ui.Style().gray(f"\n  {who}{agent.model} · jawaban ini: {u['input']:,} token masuk · "
                          f"{u['output']:,} token keluar").replace(",", "."))
    return 0


def run_ask(question):
    holder = {}
    agent, problem = _make_agent(holder)
    if not agent:
        print(ui.Style(sys.stderr).yellow(problem), file=sys.stderr)
        return 1
    return _answer(agent, holder, question)


AI_HELP = """inaproc ai            obrolan dengan asisten AI (/baru, /model, /keluar)
inaproc ai "…"        satu pertanyaan (sama dengan: inaproc ask "…" atau inaproc "…")
Atur provider, API key dan model dengan:  inaproc setup"""


def cmd_ai(args):
    if args and args[0] in ("-h", "--help"):
        print(AI_HELP)
        return 0
    if args:
        return run_ask(" ".join(args))
    try:
        import readline  # noqa: F401 — arrow keys and history in the chat prompt
    except ImportError:
        pass
    holder = {}
    agent, problem = _make_agent(holder)
    if not agent:
        print(ui.Style().yellow(problem))
        return 1
    st = ui.Style()
    s = _ai_settings()
    others = len(_profiles().ordered(active_only=True)) - 1
    ui.panel("Asisten AI inaproc", [
        f"{s['label']} · {cfg.PROVIDERS[s['provider']]['label']} · {s['model']}" +
        (f"  (+{others} key cadangan)" if others > 0 else ""),
        "Tanyakan apa saja: cari & bandingkan produk, termurah, cek penyedia, RAB pembanding,",
        "produk Anda, kategori sesuai KBLI, ubah harga/stok (selalu minta persetujuan Anda dulu).",
        "Perintah:  /baru  percakapan baru   /model  ganti model   /keluar  selesai"])
    examples = ["carikan kertas HVS A4 80 gram termurah, bandingkan 3 teratas",
                "kategori apa saja yang sesuai KBLI perusahaan saya?",
                "tampilkan produk saya yang stoknya di bawah 50"]
    print(st.gray("  Contoh: " + " · ".join(f"\"{e}\"" for e in examples)))
    while True:
        try:
            print()
            prompt = st.paint("❯ ", "bold", "magenta")
            q = input(re.sub(r"(\x1b\[[0-9;]*m)", "\001\\1\002", prompt)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q in ("/keluar", "/exit", "/quit", "exit", "keluar"):
            break
        if q in ("/baru", "/new", "/clear"):
            agent.reset()
            print(st.gray("  Percakapan baru dimulai."))
            continue
        if q in ("/keys", "/key"):
            cmd_keys([])
            continue
        if q in ("/model", "/setup"):
            cmd_model([]) if q == "/model" else cmd_setup([])
            agent, problem = _make_agent(holder)
            if not agent:
                print(st.yellow(problem))
                return 1
            continue
        if q.startswith("/"):
            import inaproc_tui as tui
            kind, payload = tui.parse(q)
            if kind == "help":
                cmd_help([])
            elif kind == "error":
                print(st.yellow("  " + payload[0]))
            elif kind == "run":
                run_skill(payload)
            elif kind in ("app", "interactive"):
                main(payload)
                agent, problem = _make_agent(holder)
            elif kind == "clear":
                print("\033[2J\033[H", end="")
            elif kind == "quit":
                break
            continue
        try:
            _answer(agent, holder, q)
        except KeyboardInterrupt:
            print(st.yellow("\n  Dibatalkan."))
    print(st.gray("  Sampai jumpa."))
    return 0


# ----------------------------------------------------------------------------------------------- whoami / login / doctor
def _penyedia_info():
    import inaproc_penyedia as ip
    b, tab = ip.open_penyedia(browser=ip.default_browser())
    try:
        return ip.session_info(tab)
    finally:
        b.close()


def cmd_whoami(args):
    st = ui.Style()
    rows = []
    with ui.Steps() as steps:
        steps.start("Memeriksa login penyedia di Safari")
        try:
            info = _penyedia_info()
            steps.done("login" if info.get("signedIn") else "belum login")
        except Exception as e:  # noqa: BLE001 — shown as a guide below
            info = {"signedIn": False, "error": str(e)}
            steps.fail("tidak bisa diperiksa")
    if info.get("signedIn"):
        who = " · ".join(x for x in (info.get("name"), info.get("company")) if x) or "akun Anda"
        rows.append(["Penyedia", st.green("✓ ") + who])
        if info.get("expires"):
            rows.append(["Sesi berlaku s.d.", info["expires"]])
    else:
        rows.append(["Penyedia", st.yellow("belum login") + st.gray("  → inaproc login")])
    profiles = _profiles()
    act = profiles.ordered(active_only=True)
    if act:
        s = act[0]
        key = profiles.secret(s)
        rows.append(["AI", f"{s['label']} · {cfg.PROVIDERS[s['provider']]['label']} · {s['model']}"])
        rows.append(["API key", (ui.mask(key) + st.gray(f"  ({s.get('source')})")) if key else st.yellow("hilang")])
        if len(act) > 1:
            rows.append(["Cadangan", " → ".join(f"{e['label']} ({e['provider']})" for e in act[1:])])
    else:
        rows.append(["AI", st.yellow("belum diatur") + st.gray("  → inaproc keys add")])
    rows.append(["Versi", "inaproc " + _version()])
    print(ui.kv_table(rows))
    if info.get("error"):
        show_guide(info["error"])
    return 0


def cmd_login(args):
    import inaproc_penyedia as ip
    browser = "chrome" if "--browser" in args and "chrome" in args else ip.default_browser()
    if browser == "safari" and platform.system() == "Darwin":
        ui.panel("Login penyedia", ["Safari akan membuka penyedia.inaproc.id.",
                                    "Masuk seperti biasa (centang Cloudflare sendiri). inaproc menunggu di sini.",
                                    "Password tidak pernah dibaca atau disimpan oleh inaproc."], kind="step")
    return run_skill(["penyedia", "login", "--browser", browser] + [a for a in args if a not in ("--browser", "chrome",
                                                                                               "safari")],
                     title="Menunggu Anda login di penyedia.inaproc.id")


def _check(steps, label, fn):
    steps.start(label)
    try:
        ok, detail, fix = fn()
    except Exception as e:  # noqa: BLE001
        ok, detail, fix = False, str(e)[:160], str(e)
    (steps.done if ok else steps.fail)(detail)
    return ok, fix


def cmd_doctor(args):
    import inaproc
    problems = []
    with ui.Steps(title=ui.Style(sys.stderr).bold("Memeriksa kesiapan inaproc")) as steps:
        _check(steps, "Python", lambda: (sys.version_info >= (3, 8), platform.python_version(), None))

        def net():
            c = inaproc.Client(min_interval=0.5)
            try:
                c.gql("ppnList", {})
            finally:
                c.close()
            return True, "katalog.inaproc.id terhubung", None
        ok, fix = _check(steps, "Koneksi ke katalog.inaproc.id", net)
        if not ok:
            problems.append(("Tidak bisa menghubungi katalog.inaproc.id", ["Periksa koneksi internet / proxy kantor.",
                                                                          "Pakai proxy:  inaproc … --proxy http://…"]))
        if platform.system() == "Darwin":
            import inaproc_safari as sf

            def safari():
                s = sf.Safari()
                tab = s.find_tab("https://penyedia.inaproc.id") or s.new_tab(PENYEDIA_URL)
                tab.wait_load(timeout=30)
                tab.evaluate("1+1")
                return True, "Apple Events diizinkan", None
            ok, fix = _check(steps, "Safari: izin JavaScript dari terminal", safari)
            if not ok:
                problems.append(("safari", fix))
            else:
                def login():
                    info = _penyedia_info()
                    who = " · ".join(x for x in (info.get("name"), info.get("company")) if x)
                    return bool(info.get("signedIn")), (who or "belum login"), "not signed in"
                ok, fix = _check(steps, "Login penyedia.inaproc.id", login)
                if not ok:
                    problems.append(("safari", fix or "not signed in"))
        s = _ai_settings()

        def ai_key():
            import inaproc_ai as ai
            if not s:
                return False, "belum diatur", None
            key = _profiles().secret(s)
            if not key:
                return False, "API key tidak ada", None
            ai.verify_key(s["provider"], key)
            n = len(_profiles().ordered(active_only=True))
            return True, (f"{s['label']} · {cfg.PROVIDERS[s['provider']]['label']} · {s['model']} · key {ui.mask(key)}"
                          + (f" · {n - 1} cadangan" if n > 1 else "")), None
        ok, fix = _check(steps, "AI (API key)", ai_key)
        if not ok:
            problems.append(("Mode AI belum siap", ["Jalankan:  inaproc setup", "Pilih provider, tempel API key, "
                                                                               "pilih model."]))
    if not problems:
        ui.panel("Semua siap", ["Coba:  inaproc ai   atau   inaproc"], kind="ok")
        return 0
    for title, body in problems:
        if title == "safari":
            show_guide(body or "Allow JavaScript from Apple Events")
        else:
            ui.panel(title, body, kind="warn")
    return 1


# ----------------------------------------------------------------------------------------------- home + guided menu
COMMAND_HELP = [
    ("inaproc", "TUI: header tetap, menu /, hasil bergulir (di terminal)"),
    ("inaproc menu", "menu bernomor klasik"),
    ("inaproc setup", "atur AI: tambah key, ganti model, prioritas"),
    ("inaproc keys", "semua API key + urutan prioritas (auto-pindah jika gagal)"),
    ("inaproc model", "ganti model AI"),
    ("inaproc ai", "ngobrol dengan asisten AI (cari, bandingkan, analisis, RAB, produk Anda)"),
    ("inaproc \"pertanyaan…\"", "satu pertanyaan langsung ke AI"),
    ("inaproc whoami", "akun penyedia + AI yang aktif"),
    ("inaproc login", "login penyedia lewat Safari"),
    ("inaproc doctor", "cek kesiapan (koneksi, Safari, login, API key)"),
    ("inaproc search \"kata\"", "cari produk katalog"),
    ("inaproc cheapest \"kata\"", "produk termurah yang benar-benar cocok"),
    ("inaproc detail <link>  ·  compare <link> <link>", "detail / bandingkan produk"),
    ("inaproc seller <slug>  ·  sellers \"nama\"", "profil & skor penyedia"),
    ("inaproc penyedia products", "produk Anda"),
    ("inaproc penyedia categories", "kategori yang cocok dengan KBLI Anda"),
    ("inaproc penyedia orders  ·  order <no>", "pesanan Anda (hanya baca)"),
    ("inaproc penyedia negotiations  ·  negotiation <no>", "negosiasi + riwayat tawaran (hanya baca)"),
    ("inaproc penyedia product update|create|delete …", "ubah produk (dry run → apply)"),
    ("inaproc help", "daftar ini;  inaproc <perintah> --help untuk opsinya"),
]


def cmd_help(args):
    print(ui.table(["Perintah", "Fungsi"], COMMAND_HELP))
    import inaproc_tui as tui
    print()
    print(ui.table(["Menu / (TUI & chat)", "Sama dengan perintah biasa (script / AI agent)"],
                   [[c.usage, c.equivalent] for c in tui.COMMANDS if c.equivalent != "—"]))
    return 0


def _ask_save():
    i = ui.choose("Simpan hasilnya?", ["Tampilkan saja", "Excel (.xlsx)", "CSV", "JSON", "YAML"], default=0)
    if i == 0:
        return []
    fmt = ["", "xlsx", "csv", "json", "yaml"][i]
    folder = ui.ask("Simpan di folder", default=os.getcwd())
    return ["--save", fmt, "--out-dir", folder]


def _flow_search(cheapest):
    kw = ui.ask("Cari produk apa?")
    if not kw:
        return 0
    argv = ["cheapest" if cheapest else "search", kw]
    if ui.confirm("Hanya produk dalam negeri (PDN)?", default=False):
        argv += ["--label", "PDN"]
    mx = ui.ask("Harga maksimal (Rupiah, kosongkan jika bebas)", default="")
    if mx.replace(".", "").isdigit():
        argv += ["--max-price", mx.replace(".", "")]
    return run_skill(argv + _ask_save())


def _flow_detail():
    links = ui.ask("Tempel 1 link produk (atau beberapa, pisahkan spasi untuk dibandingkan)").split()
    if not links:
        return 0
    return run_skill((["compare"] if len(links) > 1 else ["detail"]) + links + _ask_save())


def _flow_seller():
    q = ui.ask("Nama atau slug penyedia")
    if not q:
        return 0
    if " " in q or not re.match(r"^[a-z0-9-]+$", q):
        return run_skill(["sellers", q])
    return run_skill(["seller", q, "--products", "10"])


def _flow_my_products():
    argv = ["penyedia", "products", "--per-page", "50"]
    w = ui.ask("Saring nama produk (kosongkan untuk semua)", default="")
    if w:
        argv += ["--search", w]
    return run_skill(argv + _ask_save())


def _flow_categories():
    argv = ["penyedia", "categories"]
    i = ui.choose("Kategori mana?", ["Hanya yang cocok dengan KBLI saya", "Semua kategori + kolom cocok/tidak"],
                  default=0)
    if i == 1:
        argv.append("--all")
    w = ui.ask("Saring kata (kosongkan untuk semua)", default="")
    if w:
        argv += ["--search", w]
    return run_skill(argv + _ask_save())


def _flow_ai_with(hint):
    if not _ai_settings():
        ui.panel("Fitur ini memakai asisten AI", ["Atur dulu:  inaproc setup"], kind="warn")
        return 1
    print(ui.Style().gray("  " + hint))
    return cmd_ai([])


MENU = [
    ("Tanya asisten AI (cari, bandingkan, analisis)", lambda: cmd_ai([])),
    ("Cari produk", lambda: _flow_search(False)),
    ("Produk termurah", lambda: _flow_search(True)),
    ("Detail / bandingkan produk", _flow_detail),
    ("Cek penyedia (seller)", _flow_seller),
    ("Produk saya di penyedia", _flow_my_products),
    ("Kategori sesuai KBLI saya", _flow_categories),
    ("Pesanan saya", lambda: run_skill(["penyedia", "orders"])),
    ("Negosiasi saya", lambda: run_skill(["penyedia", "negotiations"])),
    ("Tambah / ubah / hapus produk (lewat AI, dengan persetujuan)",
     lambda: _flow_ai_with("Contoh: \"ubah harga produk <id> jadi 600 ribu\"")),
    ("Perbandingan RAB (lewat AI)", lambda: _flow_ai_with("Contoh: \"ini RAB saya ./RAB.xlsx, link perusahaan saya …, "
                                                            "carikan pembanding minimal 2\"")),
    ("Akun & status (whoami)", lambda: cmd_whoami([])),
    ("Login penyedia", lambda: cmd_login([])),
    ("Pengaturan AI (setup)", lambda: cmd_setup([])),
    ("Cek kesiapan (doctor)", lambda: cmd_doctor([])),
]


def cmd_home(args):
    ui.banner(_version())
    st = ui.Style()
    s = _ai_settings()
    ai_line = (st.green("● ") + f"AI: {s['label']} · {cfg.PROVIDERS[s['provider']]['label']} · {s['model']}") if s \
        else (st.yellow("● ") + "AI belum diatur" + st.gray("  → inaproc keys add"))
    print("  " + ai_line)
    if not _interactive():
        print()
        return cmd_help([])
    while True:
        print()
        labels = [m[0] for m in MENU] + ["Keluar"]
        i = ui.choose("Apa yang ingin Anda lakukan?", labels, default=0)
        if i == len(MENU):
            return 0
        try:
            MENU[i][1]()
        except KeyboardInterrupt:
            print(st.yellow("\n  Dibatalkan."))


# ----------------------------------------------------------------------------------------------- main
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if not argv:
            if _interactive() and os.environ.get("INAPROC_TUI", "1") != "0":
                import inaproc_tui
                return inaproc_tui.run(_version())
            return cmd_home(argv)
        head, rest = argv[0], argv[1:]
        if head in ("-h", "--help", "help"):
            return cmd_help(rest)
        if head in ("-V", "--version"):
            print("inaproc " + _version())
            return 0
        if head == "menu":
            return cmd_home(rest)
        if head == "tui":
            import inaproc_tui
            if not _interactive():
                print("TUI butuh terminal interaktif. Untuk script / AI agent pakai perintah biasa (inaproc help).",
                      file=sys.stderr)
                return 2
            return inaproc_tui.run(_version())
        if head == "setup":
            return cmd_setup(rest)
        if head in ("keys", "key"):
            return cmd_keys(rest)
        if head == "model":
            return cmd_model(rest)
        if head in ("ai", "chat"):
            return cmd_ai(rest)
        if head == "ask":
            return run_ask(" ".join(rest)) if rest else cmd_ai([])
        if head == "whoami":
            return cmd_whoami(rest)
        if head == "login":
            return cmd_login(rest)
        if head == "doctor":
            return cmd_doctor(rest)
        commands = _skill_commands()
        if head.startswith("-") or head in commands:
            return run_skill(argv)
        import difflib
        near = difflib.get_close_matches(head, sorted(commands | APP_COMMANDS), n=1, cutoff=0.75)
        if near and len(argv) <= 3:
            print(ui.Style().yellow(f"“{head}” bukan perintah inaproc.") + f" Maksud Anda:  inaproc {near[0]} "
                  + " ".join(rest))
            return 2
        # Anything else is a question in plain language.
        if _ai_settings():
            return run_ask(" ".join(argv))
        print(ui.Style().yellow(f"“{head}” bukan perintah inaproc.") + " Untuk bertanya dengan bahasa biasa, atur AI "
              "dulu:  inaproc setup   ·   daftar perintah:  inaproc help")
        return 2
    except KeyboardInterrupt:
        print(ui.Style().yellow("\nDibatalkan."))
        return 130
    except Exception as e:  # noqa: BLE001 — known errors (Keychain, AI, Chrome, Safari) print as one line
        if getattr(e, "exit_code", None) is None:
            raise
        print(ui.Style(sys.stderr).red("✗ ") + str(e), file=sys.stderr)
        return e.exit_code
    finally:
        if argv and argv[0] in ("setup", "ai", "chat", "ask", "whoami", "login", "doctor", "keys", "model"):
            try:
                import inaproc_update
                note = inaproc_update.maybe_update(_version())
                if note:
                    print(ui.Style(sys.stderr).gray(note), file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
