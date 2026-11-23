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
APP_COMMANDS = {"setup", "ai", "chat", "ask", "whoami", "login", "doctor", "help", "menu", "logout-ai"}


# ----------------------------------------------------------------------------------------------- helpers
def _version():
    import inaproc
    return inaproc.VERSION


def _store():
    return cfg.KeyStore()


def _key_for(provider):
    return _store().get(provider)


def _ai_settings():
    s = cfg.load_settings()
    if s.get("provider") in cfg.PROVIDERS and s.get("model"):
        return s
    return None


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
        if code != 0 and show_guide(errors, retry=lambda: run_skill(argv, title) == 0):
            return 0
        for line in errors.rstrip().splitlines():
            if line.startswith("error:"):
                print(ui.Style(sys.stderr).red("✗ ") + line[6:].strip(), file=sys.stderr)
            elif line.strip():
                print(ui.Style(sys.stderr).gray(line), file=sys.stderr)
        if code != 0 and guide_for(errors):
            g = guide_for(errors)
            ui.panel(g[1], g[2], kind=g[0], stream=sys.stderr)
    return code


# ----------------------------------------------------------------------------------------------- setup
def _pick_model(provider, models):
    default = cfg.PROVIDERS[provider]["default_model"]
    ids = list(models)
    if provider == "openai":
        ids = sorted([m for m in ids if m.startswith(("gpt-", "o")) and "audio" not in m and "realtime" not in m
                      and "image" not in m and "tts" not in m and "transcribe" not in m and "search" not in m],
                     reverse=True)
    elif provider == "openrouter":
        pri = [m for m in ids if m.startswith(("anthropic/claude", "openai/gpt", "deepseek/", "google/gemini"))]
        ids = sorted(pri, key=lambda m: (not m.startswith("anthropic/"), m))[:15]
    elif provider == "anthropic":
        ids = [m for m in ids if m.startswith("claude")]
    if default and default in models and default in ids:
        ids.remove(default)
    if default and (default in models or not models):
        ids.insert(0, default)
    ids = ids[:15]
    labels = [m + ("  (disarankan)" if m == default else "") for m in ids] + ["Ketik nama model lain…"]
    i = ui.choose("Pilih model", labels, default=0)
    if i == len(ids):
        return ui.ask("Nama model") or (default or "")
    return ids[i]


def cmd_setup(args):
    import inaproc_ai as ai
    st = ui.Style()
    current = _ai_settings() or {}
    names = list(cfg.PROVIDERS)
    if "--provider" in args:
        provider = args[args.index("--provider") + 1]
        if provider not in cfg.PROVIDERS:
            print(f"Provider tidak dikenal: {provider}. Pilihan: {', '.join(names)}", file=sys.stderr)
            return 2
    else:
        ui.panel("Pengaturan AI", ["Mode AI memakai API key Anda sendiri. Biaya pemakaian ditagih oleh provider.",
                                   "Key disimpan di " + ("Keychain macOS" if _store().keychain else
                                                         "~/.inaproc/keys.json (hanya bisa dibaca akun Anda)") +
                                   ", tidak pernah ditampilkan atau dikirim ke tempat lain."])
        labels = []
        for n in names:
            key, _src = _key_for(n)
            labels.append(cfg.PROVIDERS[n]["label"] + (st.green("  ✓ key tersimpan") if key else ""))
        default = names.index(current["provider"]) if current.get("provider") in names else 0
        provider = names[ui.choose("Provider AI mana yang dipakai?", labels, default=default)]
    p = cfg.PROVIDERS[provider]
    existing, src = _key_for(provider)
    if "--key-stdin" in args:
        key = sys.stdin.readline().strip()
    else:
        print(st.gray(f"  Dapatkan key di {p['key_hint']}"))
        prompt = f"API key {p['label']}" + (f" (Enter = pakai yang tersimpan {ui.mask(existing)})" if existing else "")
        key = ui.ask(prompt, secret=True) or existing or ""
    if not key:
        print(st.yellow("Tidak ada key. Batal."))
        return 1
    with ui.Steps() as steps:
        steps.start(f"Memeriksa API key ke {p['label']}")
        try:
            models = ai.list_models(provider, key)
            steps.done(f"diterima · {len(models)} model tersedia")
        except ai.AIError as e:
            steps.fail(str(e))
            return 1
    if "--model" in args:
        model = args[args.index("--model") + 1]
    else:
        model = _pick_model(provider, models)
    where = _store().set(provider, key) if key != existing else src
    cfg.save_settings({"provider": provider, "model": model})
    ui.panel("Siap dipakai", [f"Provider : {p['label']}", f"Model    : {model}",
                              f"API key  : {ui.mask(key)}  ({where})", "",
                              "Coba:  inaproc ai", "       inaproc \"carikan kertas HVS A4 termurah dan bandingkan\""],
             kind="ok")
    return 0


# ----------------------------------------------------------------------------------------------- AI
def _confirm_plan_factory(get_steps):
    def confirm(doc, path):
        s = get_steps()
        if s:
            s.done("menunggu persetujuan Anda")
        writes = doc.get("writes") or []
        lines = [f"Akun     : {doc.get('account')}", f"Jenis    : {doc.get('kind')}  ·  {len(writes)} perubahan", ""]
        lines += [f"• {w.get('summary')}" for w in writes[:12]]
        if len(writes) > 12:
            lines.append(f"… dan {len(writes) - 12} lagi (lihat file rencana)")
        lines += ["", f"Rencana: {path}"]
        ui.panel("AI ingin mengirim perubahan ke penyedia.inaproc.id", lines, kind="warn")
        if not _interactive():
            print("Tidak ada terminal interaktif untuk konfirmasi; tidak ada yang dikirim.", file=sys.stderr)
            return False
        return ui.confirm("Kirim perubahan ini sekarang?", default=False)
    return confirm


def _make_agent(holder):
    import inaproc_ai as ai
    s = _ai_settings()
    if not s:
        return None, "AI belum diatur. Jalankan:  inaproc setup"
    key, _src = _key_for(s["provider"])
    if not key:
        return None, f"Tidak ada API key untuk {cfg.PROVIDERS[s['provider']]['label']}. Jalankan:  inaproc setup"
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

    runner = ai.ToolRunner(confirm=_confirm_plan_factory(lambda: holder.get("steps")),
                           on_start=tool_start, on_end=tool_end)
    agent = ai.Agent(s["provider"], key, s["model"], runner=runner, on_think=think)
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
    print(ui.Style().gray(f"\n  {agent.model} · {agent.usage['input']:,} token masuk · "
                          f"{agent.usage['output']:,} token keluar").replace(",", "."))
    return 0


def run_ask(question):
    holder = {}
    agent, problem = _make_agent(holder)
    if not agent:
        print(ui.Style(sys.stderr).yellow(problem), file=sys.stderr)
        return 1
    return _answer(agent, holder, question)


def cmd_ai(args):
    if args:
        return run_ask(" ".join(args))
    holder = {}
    agent, problem = _make_agent(holder)
    if not agent:
        print(ui.Style().yellow(problem))
        return 1
    st = ui.Style()
    s = _ai_settings()
    ui.panel("Asisten AI inaproc", [
        f"{cfg.PROVIDERS[s['provider']]['label']} · {s['model']}",
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
            q = input(st.paint("❯ ", "bold", "magenta")).strip()
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
        if q in ("/model", "/setup"):
            cmd_setup([])
            agent, problem = _make_agent(holder)
            if not agent:
                print(st.yellow(problem))
                return 1
            continue
        if q in ("/bantuan", "/help"):
            print(st.gray("  /baru · /model · /keluar — selebihnya ketik pertanyaan biasa."))
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
    s = _ai_settings()
    if s:
        key, src = _key_for(s["provider"])
        rows.append(["AI", f"{cfg.PROVIDERS[s['provider']]['label']} · {s['model']}"])
        rows.append(["API key", (ui.mask(key) + st.gray(f"  ({src})")) if key else st.yellow("tidak ada → inaproc setup")])
    else:
        rows.append(["AI", st.yellow("belum diatur") + st.gray("  → inaproc setup")])
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
            key, src = _key_for(s["provider"])
            if not key:
                return False, "API key tidak ada", None
            ai.list_models(s["provider"], key)
            return True, f"{cfg.PROVIDERS[s['provider']]['label']} · {s['model']} · key {ui.mask(key)}", None
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
    ("inaproc", "menu interaktif (paling mudah)"),
    ("inaproc setup", "atur AI: provider, API key, model"),
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
    ("inaproc penyedia product update|create|delete …", "ubah produk (dry run → apply)"),
    ("inaproc help", "daftar ini;  inaproc <perintah> --help untuk opsinya"),
]


def cmd_help(args):
    print(ui.table(["Perintah", "Fungsi"], COMMAND_HELP))
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
    ai_line = (st.green("● ") + f"AI: {cfg.PROVIDERS[s['provider']]['label']} · {s['model']}") if s else \
        (st.yellow("● ") + "AI belum diatur" + st.gray("  → inaproc setup"))
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
            return cmd_home(argv)
        head, rest = argv[0], argv[1:]
        if head in ("-h", "--help", "help"):
            return cmd_help(rest)
        if head in ("-V", "--version"):
            print("inaproc " + _version())
            return 0
        if head == "menu":
            return cmd_home(rest)
        if head == "setup":
            return cmd_setup(rest)
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
        if head.startswith("-") or head in _skill_commands():
            return run_skill(argv)
        # Anything else is a question in plain language.
        if _ai_settings() and _key_for(_ai_settings()["provider"])[0]:
            return run_ask(" ".join(argv))
        print(ui.Style().yellow(f"“{head}” bukan perintah inaproc.") + " Untuk bertanya dengan bahasa biasa, atur AI "
              "dulu:  inaproc setup   ·   daftar perintah:  inaproc help")
        return 2
    except KeyboardInterrupt:
        print(ui.Style().yellow("\nDibatalkan."))
        return 130
    finally:
        if argv and argv[0] in ("setup", "ai", "chat", "ask", "whoami", "login", "doctor"):
            try:
                import inaproc_update
                note = inaproc_update.maybe_update(_version())
                if note:
                    print(ui.Style(sys.stderr).gray(note), file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
