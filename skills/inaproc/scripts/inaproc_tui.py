"""Full-screen terminal UI for `inaproc` (curses, standard library only).

Layout: a sticky header (logo, AI status, the /menu list), a scrolling body with the results, a live step line
and an input line. `/` commands map one-to-one to plain `inaproc` commands (see COMMANDS[].equivalent), so AI agents
such as Claude Code use those commands (and --json) directly; the TUI itself only opens in a real terminal.
"""
import contextlib
import io
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import inaproc_ui as ui  # noqa: E402


class Cmd:
    def __init__(self, name, desc, equivalent, build, kind="run", aliases=(), usage=None):
        self.name, self.desc, self.equivalent, self.build = name, desc, equivalent, build
        self.kind, self.aliases, self.usage = kind, aliases, usage or name


def _need(args, usage):
    if not args:
        raise ValueError(f"Perlu isi: {usage}")
    return args


def _rest(args):
    return " ".join(args)


COMMANDS = [
    Cmd("/cari", "cari produk katalog", "inaproc search \"kata\"",
        lambda a: ["search", _rest(_need(a, "/cari <kata>"))], usage="/cari <kata>"),
    Cmd("/termurah", "produk termurah yang benar-benar cocok", "inaproc cheapest \"kata\" --top 10",
        lambda a: ["cheapest", _rest(_need(a, "/termurah <kata>")), "--top", "10"], usage="/termurah <kata>"),
    Cmd("/detail", "detail satu produk (link atau penjual/slug)", "inaproc detail <link>",
        lambda a: ["detail"] + _need(a, "/detail <link>")[:1], usage="/detail <link>"),
    Cmd("/bandingkan", "bandingkan 2+ produk", "inaproc compare <link> <link>",
        lambda a: ["compare"] + _need(a, "/bandingkan <link> <link>"), usage="/bandingkan <link> <link>"),
    Cmd("/penjual", "profil & skor penyedia di katalog", "inaproc seller <slug>  /  inaproc sellers \"nama\"",
        lambda a: (["seller", a[0], "--products", "10"] if len(_need(a, "/penjual <slug|nama>")) == 1 and
                   a[0].replace("-", "").isalnum() and a[0] == a[0].lower() else ["sellers", _rest(a)]),
        usage="/penjual <slug|nama>"),
    Cmd("/produk", "produk Anda di penyedia", "inaproc penyedia products [--search kata]",
        lambda a: ["penyedia", "products"] + (["--search", _rest(a)] if a else [])),
    Cmd("/kategori", "kategori yang cocok dengan KBLI Anda (L1/L2/L3)", "inaproc penyedia categories [--search kata]",
        lambda a: ["penyedia", "categories"] + (["--search", _rest(a)] if a else [])),
    Cmd("/pesanan", "pesanan Anda; dengan nomor: detail", "inaproc penyedia orders  /  inaproc penyedia order <no>",
        lambda a: ["penyedia", "order", a[0]] if a else ["penyedia", "orders"]),
    Cmd("/nego", "negosiasi Anda; dengan nomor: riwayat tawaran",
        "inaproc penyedia negotiations  /  inaproc penyedia negotiation <no>",
        lambda a: ["penyedia", "negotiation", a[0]] if a else ["penyedia", "negotiations"]),
    Cmd("/whoami", "akun penyedia + AI aktif", "inaproc whoami", lambda a: ["whoami"], kind="app"),
    Cmd("/keys", "API key + prioritas (tambah/hapus/urutkan)", "inaproc keys [add|move|model|test|on|off|remove]",
        lambda a: ["keys"] + a, kind="interactive"),
    Cmd("/model", "ganti model AI", "inaproc model", lambda a: ["model"] + a, kind="interactive"),
    Cmd("/login", "login penyedia lewat Safari", "inaproc login", lambda a: ["login"] + a, kind="interactive"),
    Cmd("/doctor", "cek kesiapan + langkah perbaikan", "inaproc doctor", lambda a: ["doctor"], kind="interactive"),
    Cmd("/run", "perintah inaproc apa pun", "inaproc <perintah> …",
        lambda a: _need(a, "/run <perintah> …"), usage="/run <perintah> …"),
    Cmd("/baru", "mulai percakapan AI baru", "—", lambda a: [], kind="new"),
    Cmd("/bersih", "bersihkan layar", "—", lambda a: [], kind="clear"),
    Cmd("/bantuan", "daftar semua / perintah", "inaproc help", lambda a: [], kind="help", aliases=("/help",)),
    Cmd("/keluar", "keluar", "—", lambda a: [], kind="quit", aliases=("/exit", "/quit")),
]


def _lookup(name):
    for c in COMMANDS:
        if name == c.name or name in c.aliases:
            return c
    return None


def parse(text):
    """Input line -> (kind, payload). kind: run | app | interactive | ai | new | clear | help | quit | error."""
    text = text.strip()
    if not text.startswith("/"):
        return ("ai", [text])
    import shlex
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    c = _lookup(parts[0].lower())
    if not c:
        near = complete(parts[0][:3])
        return ("error", [f"{parts[0]} tidak dikenal." + (f" Maksud Anda: {', '.join(near)}?" if near else "")
                          + "  Ketik /bantuan."])
    try:
        argv = c.build(parts[1:])
    except ValueError as e:
        return ("error", [f"{e}"])
    return (c.kind, argv)


def complete(prefix):
    if not prefix.startswith("/"):
        return []
    p = prefix.lower()
    return [c.name for c in COMMANDS if c.name.startswith(p)]


class Body:
    def __init__(self, limit=5000):
        self.lines, self.limit, self.offset = [], limit, 0

    def add(self, text):
        for line in str(text).splitlines() or [""]:
            self.lines.append(line)
        del self.lines[:-self.limit]
        self.offset = 0

    def clear(self):
        self.lines, self.offset = [], 0

    def scroll(self, n):
        self.offset = max(0, min(self.offset + n, max(0, len(self.lines) - 1)))

    def visible(self, height):
        end = len(self.lines) - self.offset
        return self.lines[max(0, end - height):end]


class TuiSteps:
    """Same surface as ui.Steps (start/update/sub/done/fail/info/pause/label), drawn by the TUI."""

    def __init__(self, app):
        self.app, self.label, self.detail, self.t0 = app, None, None, None
        self._lock = threading.Lock()

    def __enter__(self):
        self._prev, ui.CURRENT = ui.CURRENT, self
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.label is not None:
            (self.done if exc_type is None else self.fail)(None if exc_type is None else str(exc))
        ui.CURRENT = self._prev
        return False

    def start(self, label):
        with self._lock:
            if self.label is not None:
                self._finish("✓", None)
            self.label, self.detail, self.t0 = label, None, time.time()

    def update(self, label):
        with self._lock:
            if self.label is None:
                self.t0 = time.time()
            self.label = label

    def sub(self, text):
        self.detail = text

    def done(self, detail=None):
        with self._lock:
            self._finish("✓", detail)

    def fail(self, detail=None):
        with self._lock:
            self._finish("✗", detail)

    def info(self, text):
        self.app.body.add("  " + text)

    def pause(self):
        pass

    def _finish(self, mark, detail):
        if self.label is None:
            return
        secs = time.time() - (self.t0 or time.time())
        self.app.body.add(f"{mark} {self.label}" + (f" — {detail}" if detail else "") + f"  {secs:.1f}s")
        self.label, self.detail = None, None


SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def screen_rows(lines, width, height):
    """The last `height` screen rows for these lines, long lines wrapped (links stay complete)."""
    rows = []
    for line in reversed(lines):
        line = ui.ANSI.sub("", line)
        parts, cur, cw = [], "", 0
        for ch in line:
            w = ui.vlen(ch)
            if cw + w > width:
                parts.append(cur)
                cur, cw = "", 0
            cur, cw = cur + ch, cw + w
        parts.append(cur)
        rows[:0] = parts
        if len(rows) >= height:
            break
    return rows[-height:]


class App:
    def __init__(self, scr, version):
        import curses
        self.c, self.scr, self.version = curses, scr, version
        self.body = Body()
        self.input, self.history, self.hpos = "", [], None
        self.busy = False
        self.steps = TuiSteps(self)
        self.modal = None
        self.agent, self.holder = None, {}
        self.status = ""
        self.quit = False
        self._colors()
        self.refresh_status()
        self.body.add("Ketik pertanyaan untuk AI, atau / untuk menu (Tab melengkapi, ↑/↓ riwayat, PgUp/PgDn gulir).")

    def _colors(self):
        c = self.c
        self.attr = {k: c.A_NORMAL for k in ("red", "green", "gray", "cyan", "magenta", "yellow", "bold")}
        self.attr["bold"] = c.A_BOLD
        try:
            c.start_color()
            c.use_default_colors()
            for i, (name, col) in enumerate((("red", c.COLOR_RED), ("green", c.COLOR_GREEN), ("cyan", c.COLOR_CYAN),
                                             ("magenta", c.COLOR_MAGENTA), ("yellow", c.COLOR_YELLOW),
                                             ("gray", c.COLOR_WHITE)), 1):
                c.init_pair(i, col, -1)
                self.attr[name] = c.color_pair(i) | (c.A_DIM if name == "gray" else 0)
        except Exception:  # noqa: BLE001 — monochrome terminal
            pass

    def refresh_status(self):
        import inaproc_app as app
        try:
            act = app._profiles().ordered(active_only=True)
        except Exception:  # noqa: BLE001
            act = []
        if act:
            s = act[0]
            self.status = f"● AI: {s['label']} · {s['provider']} · {s['model']}" + \
                (f"  (+{len(act) - 1} cadangan)" if len(act) > 1 else "")
        else:
            self.status = "● AI belum diatur → /keys add"

    # drawing ----------------------------------------------------------------------------------------------------
    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        out, width = "", 0
        for ch in str(text):
            cw = ui.vlen(ch)
            if width + cw > w - x - (1 if y == h - 1 else 0):
                break
            out, width = out + ch, width + cw
        try:
            self.scr.addstr(y, x, out, attr)
        except self.c.error:
            pass

    def header(self, w):
        y = 0
        if self.scr.getmaxyx()[0] >= 34 and w >= 60:
            for line, col in zip(ui.LOGO, ("red", "red", "red", "bold", "bold", "gray")):
                self.put(y, 2, line, self.attr[col] | self.c.A_BOLD)
                y += 1
        else:
            self.put(y, 2, "INAPROC", self.attr["red"] | self.c.A_BOLD)
            y += 1
        self.put(y, 2, f"Katalog INAPROC & akun penyedia Anda · v{self.version}", self.attr["gray"])
        y += 1
        self.put(y, 2, self.status, self.attr["green" if "belum" not in self.status else "yellow"])
        y += 1
        chips, line = [c.name for c in COMMANDS if c.kind not in ("quit",)], "  "
        for name in chips:
            if ui.vlen(line + name + "  ") > w - 2:
                self.put(y, 0, line, self.attr["cyan"])
                y, line = y + 1, "  "
            line += name + "  "
        self.put(y, 0, line, self.attr["cyan"])
        y += 1
        self.put(y, 0, "─" * w, self.attr["gray"])
        return y + 1

    def style(self, line):
        s = line.lstrip()
        if s.startswith("✓"):
            return self.attr["green"]
        if s.startswith("✗"):
            return self.attr["red"]
        if s.startswith("❯"):
            return self.attr["magenta"] | self.c.A_BOLD
        if s.startswith(("╭", "├", "╰", "│")):
            return 0
        return 0

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        top = self.header(w)
        sugg = complete(self.input.split(" ")[0]) if self.input.startswith("/") and " " not in self.input else []
        bottom_rows = 2 + (min(len(sugg), 6) if sugg else 0)
        height = max(1, h - top - bottom_rows)
        for i, line in enumerate(screen_rows(self.body.visible(height), w, height)):
            self.put(top + i, 0, line, self.style(line))
        if self.body.offset:
            self.put(top, w - 18, f" ↑ {self.body.offset} baris ", self.attr["yellow"])
        y = h - bottom_rows
        for name in sugg[:6]:
            c = next(x for x in COMMANDS if x.name == name)
            self.put(y, 2, f"{c.usage:<26} {c.desc}", self.attr["cyan"])
            y += 1
        if self.steps.label:
            frame = SPIN[int(time.time() * 12) % len(SPIN)]
            secs = time.time() - (self.steps.t0 or time.time())
            detail = f" · {self.steps.detail}" if self.steps.detail else ""
            self.put(h - 2, 0, f"{frame} {self.steps.label}{detail}  {secs:.1f}s", self.attr["cyan"])
        elif self.busy:
            self.put(h - 2, 0, f"{SPIN[int(time.time() * 12) % len(SPIN)]} sedang bekerja…", self.attr["cyan"])
        else:
            self.put(h - 2, 0, "─" * w, self.attr["gray"])
        prompt = "❯ "
        shown = self.input if not self.busy else "(tunggu sampai selesai…)"
        self.put(h - 1, 0, prompt, self.attr["magenta"] | self.c.A_BOLD)
        self.put(h - 1, 2, shown, self.attr["gray"] if self.busy else 0)
        if self.modal:
            self.draw_modal(h, w)
        try:
            self.c.curs_set(0 if self.busy or self.modal else 1)
            self.scr.move(h - 1, min(w - 1, 2 + ui.vlen(self.input)))
        except self.c.error:
            pass
        self.scr.refresh()

    def draw_modal(self, h, w):
        lines = self.modal["lines"] + ["", "Kirim perubahan ini sekarang?  [y] ya   [n] tidak"]
        bw = min(w - 4, max(ui.vlen(l) for l in lines) + 4)
        bh = min(h - 2, len(lines) + 2)
        y0, x0 = max(0, (h - bh) // 2), max(0, (w - bw) // 2)
        attr = self.attr["yellow"] | self.c.A_BOLD
        self.put(y0, x0, "╭" + "─" * (bw - 2) + "╮", attr)
        for i, l in enumerate(lines[:bh - 2]):
            self.put(y0 + 1 + i, x0, "│ " + ui._fit(l, bw - 4) + " │", attr if i == len(lines) - 1 else 0)
        self.put(y0 + bh - 1, x0, "╰" + "─" * (bw - 2) + "╯", attr)

    # running ------------------------------------------------------------------------------------------------------
    def _worker(self, fn):
        def run():
            try:
                with self.steps:
                    fn()
            except Exception as e:  # noqa: BLE001
                self.body.add(f"✗ {e}")
            finally:
                self.busy = False
                self.refresh_status()
        self.busy = True
        threading.Thread(target=run, daemon=True).start()

    def run_skill(self, argv):
        import inaproc
        import inaproc_app as app

        def go():
            out, err = io.StringIO(), io.StringIO()
            self.steps.start("inaproc " + " ".join(argv))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = inaproc.main(argv)
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 1
            (self.steps.done if code == 0 else self.steps.fail)(None if code == 0 else "gagal")
            if out.getvalue().strip():
                self.body.add(app.pretty(out.getvalue(), color=False))
            errors = err.getvalue().strip()
            if errors:
                g = app.guide_for(errors)
                if g and code != 0:
                    self.body.add(f"! {g[1]}")
                    for line in g[2]:
                        self.body.add("  " + line)
                else:
                    for line in errors.splitlines():
                        self.body.add(("✗ " + line[6:].strip()) if line.startswith("error:") else "  " + line)
        self._worker(go)

    def run_app(self, argv):
        import inaproc_app as app

        def go():
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                app.main(argv)
            text = (err.getvalue() + out.getvalue()).strip()
            if text:
                self.body.add(ui.ANSI.sub("", text))
        self._worker(go)

    def run_interactive(self, argv):
        import inaproc_app as app
        self.c.def_prog_mode()
        self.c.endwin()
        print()
        try:
            app.main(argv)
        except KeyboardInterrupt:
            print("\nDibatalkan.")
        try:
            input("\n[Enter] kembali ke inaproc… ")
        except (EOFError, KeyboardInterrupt):
            pass
        self.c.reset_prog_mode()
        self.scr.refresh()
        self.refresh_status()
        self.agent = None

    def confirm(self, doc, path):
        import inaproc_app as app
        done = threading.Event()
        self.modal = {"lines": [f"Akun: {doc.get('account')} · {doc.get('kind')} · "
                                f"{len(doc.get('writes') or [])} perubahan", ""] + app._plan_lines(doc)[:20],
                      "event": done, "answer": False}
        done.wait()
        answer = self.modal["answer"]
        self.modal = None
        self.body.add(("✓ Anda menyetujui; mengirim…" if answer else "✗ Anda menolak; tidak ada yang dikirim."))
        return answer

    def ask_ai(self, text):
        import inaproc_app as app
        import inaproc_ai as ai
        if self.agent is None:
            self.holder = {"steps": self.steps}
            self.agent, problem = app._make_agent(self.holder, confirm=self.confirm)
            if not self.agent:
                self.body.add("✗ " + problem)
                return

        def go():
            try:
                answer = self.agent.send(text)
            except ai.AIError as e:
                self.steps.fail(str(e))
                return
            if self.steps.label is not None:
                self.steps.done()
            self.body.add("")
            self.body.add(app.pretty(answer, color=False))
            u = self.agent.last_usage
            who = f"{self.agent.current['label']} · " if getattr(self.agent, "current", None) else ""
            self.body.add(f"  {who}{self.agent.model} · {u['input']:,} token masuk · {u['output']:,} token keluar"
                          .replace(",", "."))
        self._worker(go)

    def submit(self):
        text = self.input.strip()
        self.input, self.hpos = "", None
        if not text:
            return
        self.history.append(text)
        self.body.add("")
        self.body.add("❯ " + text)
        kind, payload = parse(text)
        if kind == "quit":
            self.quit = True
        elif kind == "clear":
            self.body.clear()
        elif kind == "new":
            self.agent = None
            self.body.add("  Percakapan AI baru dimulai.")
        elif kind == "help":
            self.body.add(ui.table(["Menu", "Fungsi", "Perintah biasa (untuk script / AI agent)"],
                                   [[c.usage, c.desc, c.equivalent] for c in COMMANDS], color=False))
            self.body.add("  Teks biasa (tanpa /) dikirim ke asisten AI.")
        elif kind == "error":
            self.body.add("✗ " + payload[0])
        elif kind == "run":
            self.run_skill(payload)
        elif kind == "app":
            self.run_app(payload)
        elif kind == "interactive":
            self.run_interactive(payload)
        elif kind == "ai":
            self.ask_ai(payload[0])

    # keys ---------------------------------------------------------------------------------------------------------
    def key(self, k):
        c = self.c
        if self.modal:
            if k in ("y", "Y"):
                self.modal["answer"] = True
                self.modal["event"].set()
            elif k in ("n", "N", "\x1b", "\n", "\r"):
                self.modal["answer"] = False
                self.modal["event"].set()
            return
        if k in (c.KEY_PPAGE,):
            self.body.scroll(self.scr.getmaxyx()[0] // 2)
            return
        if k in (c.KEY_NPAGE,):
            self.body.scroll(-(self.scr.getmaxyx()[0] // 2))
            return
        if self.busy:
            return  # nothing typed while working can answer a later question
        if k in ("\n", "\r", c.KEY_ENTER):
            self.submit()
        elif k in (c.KEY_BACKSPACE, "\x7f", "\b"):
            self.input = self.input[:-1]
        elif k == "\t":
            opts = complete(self.input)
            if opts:
                self.input = (opts[0] if len(opts) == 1 else os.path.commonprefix(opts)) + \
                    (" " if len(opts) == 1 else "")
        elif k == c.KEY_UP and self.history:
            self.hpos = len(self.history) - 1 if self.hpos is None else max(0, self.hpos - 1)
            self.input = self.history[self.hpos]
        elif k == c.KEY_DOWN and self.hpos is not None:
            self.hpos += 1
            if self.hpos >= len(self.history):
                self.hpos, self.input = None, ""
            else:
                self.input = self.history[self.hpos]
        elif k == "\x04":  # Ctrl-D
            self.quit = True
        elif k == "\x15":  # Ctrl-U
            self.input = ""
        elif isinstance(k, str) and k.isprintable():
            self.input += k

    def loop(self):
        self.scr.timeout(90)
        self.scr.keypad(True)
        while not self.quit:
            self.draw()
            try:
                k = self.scr.get_wch()
            except self.c.error:
                continue
            except KeyboardInterrupt:
                if self.busy:
                    self.body.add("  (sedang bekerja; tunggu sampai selesai)")
                    continue
                break
            if k == self.c.KEY_RESIZE:
                continue
            self.key(k)


def run(version):
    import curses
    os.environ.setdefault("ESCDELAY", "25")

    def main(scr):
        App(scr, version).loop()
    curses.wrapper(main)
    return 0
