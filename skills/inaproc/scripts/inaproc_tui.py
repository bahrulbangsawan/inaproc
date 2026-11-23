"""Full-screen terminal UI for `inaproc` (curses, standard library only).

Layout: a sticky header (logo, AI status, the /menu list), a scrolling conversation area, a live step line and an
input line. Every `/` command maps to a plain `inaproc` command (COMMANDS[].equivalent): AI agents such as Claude
Code use those commands (and --json) directly; the TUI only opens in a real terminal. Commands that ask questions
(/model, /keys, /login, /doctor) run inside the TUI: their questions appear as popups and their output streams into
the conversation area. The mouse wheel scrolls the conversation (it is captured, so it never pages through history).
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


# ------------------------------------------------------------------------------------------------ commands
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
    Cmd("/whoami", "akun penyedia + AI aktif", "inaproc whoami", lambda a: ["whoami"], kind="inapp"),
    Cmd("/keys", "API key + prioritas (tambah/hapus/urutkan)", "inaproc keys [add|move|model|test|on|off|remove]",
        lambda a: ["keys"] + a, kind="inapp"),
    Cmd("/model", "ganti model AI", "inaproc model", lambda a: ["model"] + a, kind="inapp"),
    Cmd("/login", "login penyedia lewat Safari", "inaproc login", lambda a: ["login"] + a, kind="inapp"),
    Cmd("/doctor", "cek kesiapan + langkah perbaikan", "inaproc doctor", lambda a: ["doctor"], kind="inapp"),
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
    """Input line -> (kind, payload). kind: run | inapp | ai | new | clear | help | quit | error."""
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


# ------------------------------------------------------------------------------------------------ conversation
class Body:
    """Conversation lines as (text, kind). kind: plain | user-head | user | ai-head | ai | ai-foot | ok | fail | dim."""

    def __init__(self, limit=6000):
        self.lines, self.limit, self.offset = [], limit, 0
        self._lock = threading.Lock()

    def add(self, text, kind="plain"):
        with self._lock:
            for line in str(text).splitlines() or [""]:
                self.lines.append((ui.ANSI.sub("", line), kind))
            del self.lines[:-self.limit]
            self.offset = 0

    def clear(self):
        with self._lock:
            self.lines, self.offset = [], 0

    def scroll(self, n):
        with self._lock:
            self.offset = max(0, min(self.offset + n, max(0, len(self.lines) - 1)))

    def visible(self, height):
        with self._lock:
            end = len(self.lines) - self.offset
            return list(self.lines[max(0, end - height):end])


def add_user(body, text):
    body.add("")
    body.add(f"Anda  {time.strftime('%H:%M')}", "user-head")
    body.add(text, "user")


def add_answer(body, markdown, footer=None):
    import inaproc_app as app
    body.add("")
    body.add("◆ inaproc AI", "ai-head")
    for line in app.pretty(markdown, color=False).splitlines() or [""]:
        body.add(line, "ai")
    if footer:
        body.add(footer, "ai-foot")


GUTTER = {"ai": "│ ", "user": "▍ ", "ai-foot": "╰ "}


def screen_rows(lines, width, height):
    """The last `height` screen rows as (text, kind); long lines wrap, answers keep their left bar."""
    rows = []
    for item in reversed(lines):
        text, kind = (item, "plain") if isinstance(item, str) else item
        gutter = GUTTER.get(kind, "")
        room = max(10, width - ui.vlen(gutter))
        parts = _wrap(text, room)
        rows[:0] = [(gutter + p if kind != "ai-foot" or i == 0 else "  " + p, kind) for i, p in enumerate(parts)]
        if len(rows) >= height:
            break
    return rows[-height:]


def _wrap(text, room):
    """Wrap between words; a word longer than the line (a link, a table border) is split at the edge."""
    if ui.vlen(text) <= room:
        return [text]
    parts, cur = [], ""
    for word in text.split(" "):
        candidate = (cur + " " + word) if cur else word
        if ui.vlen(candidate) <= room:
            cur = candidate
            continue
        if cur:
            parts.append(cur)
        cur = word
        while ui.vlen(cur) > room:  # very long word: split at the edge
            cut, w = "", 0
            for ch in cur:
                if w + ui.vlen(ch) > room:
                    break
                cut, w = cut + ch, w + ui.vlen(ch)
            parts.append(cut)
            cur = cur[len(cut):]
    parts.append(cur)
    return parts


def wheel_delta(bstate):
    """Mouse wheel: up -> +1 (older lines), down -> -1, anything else 0."""
    import curses
    up = getattr(curses, "BUTTON4_PRESSED", 0x80000)
    down = getattr(curses, "BUTTON5_PRESSED", 0x200000)
    if bstate & up:
        return 1
    if bstate & down:
        return -1
    return 0


# ------------------------------------------------------------------------------------------------ in-TUI prompts
class BodyWriter(io.TextIOBase):
    """A stdout/stderr replacement that streams complete lines into the conversation area."""

    def __init__(self, body, kind="plain"):
        self.body, self.kind, self._buf = body, kind, ""

    def writable(self):
        return True

    def isatty(self):
        return False

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.body.add(line, self._kind_for(line))
        return len(s)

    def _kind_for(self, line):
        t = ui.ANSI.sub("", line).lstrip()
        if t.startswith("✓"):
            return "ok"
        if t.startswith(("✗", "error:")):
            return "fail"
        return self.kind

    def flush(self):
        if self._buf:
            self.body.add(self._buf, self._kind_for(self._buf))
            self._buf = ""


class NullSteps:
    label = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, name):
        return lambda *a, **k: None


class StepsProxy:
    """`with ui.Steps() as s:` inside app code -> the TUI's live step line."""

    def __init__(self, steps):
        self.steps = steps

    def __enter__(self):
        return self.steps

    def __exit__(self, exc_type, exc, tb):
        if self.steps.label is not None:
            (self.steps.done if exc_type is None else self.steps.fail)(None if exc_type is None else str(exc))
        return False


class FakeIO:
    """For tests: scripted answers."""

    def __init__(self, choose=(), ask=(), confirm=()):
        self._c, self._a, self._y, self.asked = list(choose), list(ask), list(confirm), []

    def choose(self, prompt, options, default=None, stream=None):
        self.asked.append(("choose", prompt))
        return self._c.pop(0)

    def ask(self, prompt, default=None, secret=False, stream=None):
        self.asked.append(("ask", prompt, secret))
        return self._a.pop(0)

    def confirm(self, prompt, default=False, stream=None, flush=False):
        self.asked.append(("confirm", prompt))
        return self._y.pop(0)


@contextlib.contextmanager
def tui_io(io_impl, steps_factory):
    """Route ui.choose/ask/confirm/Steps (and the app's "is there a terminal?" check) to the TUI."""
    import inaproc_app as app
    saved = (ui.choose, ui.ask, ui.confirm, ui.Steps, app._interactive)
    ui.choose, ui.ask, ui.confirm, ui.Steps = io_impl.choose, io_impl.ask, io_impl.confirm, steps_factory
    app._interactive = lambda: True
    try:
        yield
    finally:
        ui.choose, ui.ask, ui.confirm, ui.Steps, app._interactive = saved


class TuiIO:
    """Questions from a worker thread, answered in a popup by the main (drawing) thread."""

    def __init__(self, app):
        self.app = app

    def _wait(self, modal):
        modal["event"] = threading.Event()
        self.app.modal = modal
        modal["event"].wait()
        self.app.modal = None
        if modal.get("cancel"):
            raise KeyboardInterrupt
        return modal["answer"]

    def choose(self, prompt, options, default=None, stream=None):
        return self._wait({"kind": "choose", "title": ui.ANSI.sub("", prompt),
                           "options": [ui.ANSI.sub("", str(o)) for o in options], "index": default or 0})

    def ask(self, prompt, default=None, secret=False, stream=None):
        value = self._wait({"kind": "ask", "title": ui.ANSI.sub("", prompt), "value": "", "secret": secret,
                            "default": default})
        return value or (default if default is not None else "")

    def confirm(self, prompt, default=False, stream=None, flush=False):
        return self._wait({"kind": "confirm", "title": ui.ANSI.sub("", prompt), "lines": [], "default": default})


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
        self.app.body.add("  " + text, "dim")

    def pause(self):
        pass

    def _finish(self, mark, detail):
        if self.label is None:
            return
        secs = time.time() - (self.t0 or time.time())
        self.app.body.add(f"  {mark} {self.label}" + (f" — {detail}" if detail else "") + f"  {secs:.1f}s",
                          "ok" if mark == "✓" else "fail")
        self.label, self.detail = None, None


SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ------------------------------------------------------------------------------------------------ app
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
        self.body.add("Tanya apa saja ke asisten AI, atau ketik / untuk menu.  Tab melengkapi · ↑/↓ riwayat · "
                      "roda mouse / PgUp / PgDn menggulir.", "dim")

    def _colors(self):
        c = self.c
        self.attr = {k: c.A_NORMAL for k in ("red", "green", "gray", "cyan", "magenta", "yellow", "bold", "blue")}
        self.attr["bold"] = c.A_BOLD
        try:
            c.start_color()
            c.use_default_colors()
            for i, (name, col) in enumerate((("red", c.COLOR_RED), ("green", c.COLOR_GREEN), ("cyan", c.COLOR_CYAN),
                                             ("magenta", c.COLOR_MAGENTA), ("yellow", c.COLOR_YELLOW),
                                             ("gray", c.COLOR_WHITE), ("blue", c.COLOR_BLUE)), 1):
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
            self.put(y, 10, f"v{self.version}", self.attr["gray"])
            y += 1
        self.put(y, 2, self.status, self.attr["green" if "belum" not in self.status else "yellow"])
        y += 1
        line = "  "
        for c in COMMANDS:
            if c.kind == "quit":
                continue
            if ui.vlen(line + c.name + "  ") > w - 2:
                self.put(y, 0, line, self.attr["cyan"])
                y, line = y + 1, "  "
            line += c.name + "  "
        self.put(y, 0, line, self.attr["cyan"])
        y += 1
        self.put(y, 0, "─" * w, self.attr["gray"])
        return y + 1

    def style(self, kind):
        a, c = self.attr, self.c
        return {"user-head": a["magenta"] | c.A_BOLD, "user": a["magenta"], "ai-head": a["cyan"] | c.A_BOLD,
                "ai": 0, "ai-foot": a["gray"], "ok": a["green"], "fail": a["red"], "dim": a["gray"],
                "warn": a["yellow"]}.get(kind, 0)

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        top = self.header(w)
        sugg = complete(self.input.split(" ")[0]) if self.input.startswith("/") and " " not in self.input else []
        bottom_rows = 2 + (min(len(sugg), 7) if sugg else 0)
        height = max(1, h - top - bottom_rows)
        for i, (row, kind) in enumerate(screen_rows(self.body.visible(height), w, height)):
            if kind == "ai":
                self.put(top + i, 0, row[:2], self.attr["cyan"])
                self.put(top + i, 2, row[2:], 0)
            else:
                self.put(top + i, 0, row, self.style(kind))
        if self.body.offset:
            self.put(top, max(0, w - 30), f" ↑ {self.body.offset} baris di atas · End ", self.attr["yellow"])
        y = h - bottom_rows
        for name in sugg[:7]:
            c = next(x for x in COMMANDS if x.name == name)
            self.put(y, 2, f"{c.usage:<28} {c.desc}", self.attr["cyan"])
            y += 1
        if self.steps.label and not self.modal:
            frame = SPIN[int(time.time() * 12) % len(SPIN)]
            secs = time.time() - (self.steps.t0 or time.time())
            detail = f" · {self.steps.detail}" if self.steps.detail else ""
            self.put(h - 2, 0, f"{frame} {self.steps.label}{detail}  {secs:.1f}s", self.attr["cyan"])
        elif self.busy and not self.modal:
            self.put(h - 2, 0, f"{SPIN[int(time.time() * 12) % len(SPIN)]} sedang bekerja…", self.attr["cyan"])
        else:
            self.put(h - 2, 0, "─" * w, self.attr["gray"])
        self.put(h - 1, 0, "❯ ", self.attr["magenta"] | self.c.A_BOLD)
        if self.busy:
            self.put(h - 1, 2, "(tunggu sampai selesai…)", self.attr["gray"])
        elif not self.input:
            self.put(h - 1, 2, "Tanya apa saja, atau ketik / untuk menu", self.attr["gray"])
        else:
            self.put(h - 1, 2, self.input, 0)
        if self.modal:
            self.draw_modal(h, w)
        try:
            self.c.curs_set(0 if self.busy or self.modal else 1)
            self.scr.move(h - 1, min(w - 1, 2 + ui.vlen(self.input)))
        except self.c.error:
            pass
        self.scr.refresh()

    def draw_modal(self, h, w):
        m, a = self.modal, self.attr
        title = m.get("title") or ""
        if m["kind"] == "choose":
            body = [("› " if i == m["index"] else "  ") + f"{i + 1}. {o}" for i, o in enumerate(m["options"])]
            hint = "↑/↓ pilih · Enter oke · angka langsung · Esc batal"
        elif m["kind"] == "ask":
            shown = ("•" * len(m["value"])) if m.get("secret") else m["value"]
            body = ["", "  " + shown + "▏"] + ([f"  (Enter = {m['default']})"] if m.get("default") else [])
            hint = "Enter oke · Esc batal" + (" · isian disembunyikan" if m.get("secret") else "")
        else:
            body = m.get("lines") or []
            hint = "[y] ya   [n] tidak   Esc batal"
        bw = min(w - 4, max(48, max(ui.vlen(l) for l in [title, hint] + body) + 4))
        room = max(3, h - 6)
        if m["kind"] == "choose" and len(body) > room:  # keep the highlighted option in view
            start = min(max(0, m["index"] - room // 2), len(body) - room)
            body = body[start:start + room]
        visible = [title, ""] + body[:room] + ["", hint]
        bh = len(visible) + 2
        y0, x0 = max(0, (h - bh) // 2), max(0, (w - bw) // 2)
        border = a["yellow"] | self.c.A_BOLD
        self.put(y0, x0, "╭" + "─" * (bw - 2) + "╮", border)
        for i, l in enumerate(visible):
            attr = self.c.A_BOLD if i == 0 else (a["gray"] if i == len(visible) - 1 else 0)
            if m["kind"] == "choose" and l.startswith("› "):
                attr = a["cyan"] | self.c.A_BOLD
            self.put(y0 + 1 + i, x0, "│ " + ui._fit(l, bw - 4) + " │", attr)
            self.put(y0 + 1 + i, x0, "│", border)
            self.put(y0 + 1 + i, x0 + bw - 1, "│", border)
        self.put(y0 + bh - 1, x0, "╰" + "─" * (bw - 2) + "╯", border)

    # running ------------------------------------------------------------------------------------------------------
    def _worker(self, fn):
        def run():
            try:
                with self.steps:
                    fn()
            except KeyboardInterrupt:
                self.body.add("  Dibatalkan.", "warn")
            except Exception as e:  # noqa: BLE001
                self.body.add(f"✗ {e}", "fail")
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
                    self.body.add(f"! {g[1]}", "warn")
                    for line in g[2]:
                        self.body.add("  " + line, "warn")
                else:
                    for line in errors.splitlines():
                        self.body.add(("✗ " + line[6:].strip()) if line.startswith("error:") else "  " + line,
                                      "fail" if line.startswith("error:") else "dim")
        self._worker(go)

    def run_inapp(self, argv):
        """App commands that may ask questions (/model, /keys, /login, /doctor, /whoami), without leaving the TUI."""
        import inaproc_app as app

        def go():
            writer = BodyWriter(self.body)
            with tui_io(TuiIO(self), steps_factory=lambda *a, **k: StepsProxy(self.steps)), \
                    contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    app.main(argv)
                finally:
                    writer.flush()
            if argv and argv[0] in ("keys", "model", "setup"):
                self.agent = None  # pick up new keys / models on the next question
        self._worker(go)

    def confirm_plan(self, doc, path):
        import inaproc_app as app
        lines = [f"Akun: {doc.get('account')} · {doc.get('kind')} · {len(doc.get('writes') or [])} perubahan",
                 ""] + app._plan_lines(doc)[:20]
        answer = TuiIO(self)._wait({"kind": "confirm", "title": "AI ingin mengirim perubahan ke penyedia.inaproc.id",
                                    "lines": lines})
        self.body.add("  ✓ Anda menyetujui; mengirim…" if answer else "  ✗ Anda menolak; tidak ada yang dikirim.",
                      "ok" if answer else "fail")
        return answer

    def ask_ai(self, text):
        import inaproc_app as app
        import inaproc_ai as ai
        if self.agent is None:
            self.holder = {"steps": self.steps}
            self.agent, problem = app._make_agent(self.holder, confirm=self.confirm_plan)
            if not self.agent:
                self.body.add("✗ " + problem, "fail")
                return

        def go():
            t0 = time.time()
            try:
                answer = self.agent.send(text)
            except ai.AIError as e:
                self.steps.fail(str(e))
                return
            if self.steps.label is not None:
                self.steps.done()
            u = self.agent.last_usage
            who = self.agent.current["label"] + " · " if getattr(self.agent, "current", None) else ""
            add_answer(self.body, answer, footer=f"{who}{self.agent.model} · {time.time() - t0:.1f} dtk · "
                                                 f"{u['input']:,} token masuk · {u['output']:,} keluar"
                       .replace(",", "."))
        self._worker(go)

    def submit(self):
        text = self.input.strip()
        self.input, self.hpos = "", None
        if not text:
            return
        if not self.history or self.history[-1] != text:
            self.history.append(text)
        kind, payload = parse(text)
        if kind == "ai":
            add_user(self.body, text)
            self.ask_ai(payload[0])
            return
        self.body.add("")
        self.body.add("❯ " + text, "user-head")
        if kind == "quit":
            self.quit = True
        elif kind == "clear":
            self.body.clear()
        elif kind == "new":
            self.agent = None
            self.body.add("  Percakapan AI baru dimulai.", "dim")
        elif kind == "help":
            self.body.add(ui.table(["Menu", "Fungsi", "Perintah biasa (untuk script / AI agent)"],
                                   [[c.usage, c.desc, c.equivalent] for c in COMMANDS], color=False))
            self.body.add("  Teks biasa (tanpa /) dikirim ke asisten AI.", "dim")
        elif kind == "error":
            self.body.add("✗ " + payload[0], "fail")
        elif kind == "run":
            self.run_skill(payload)
        elif kind == "inapp":
            self.run_inapp(payload)

    # keys ---------------------------------------------------------------------------------------------------------
    def modal_key(self, k):
        c, m = self.c, self.modal
        if k == "\x1b":
            m["cancel"] = True
            m["event"].set()
            return
        if m["kind"] == "confirm":
            if k in ("y", "Y"):
                m["answer"] = True
                m["event"].set()
            elif k in ("n", "N", "\n", "\r"):
                m["answer"] = False
                m["event"].set()
        elif m["kind"] == "choose":
            n = len(m["options"])
            if k == c.KEY_UP:
                m["index"] = (m["index"] - 1) % n
            elif k == c.KEY_DOWN:
                m["index"] = (m["index"] + 1) % n
            elif isinstance(k, str) and k.isdigit() and 1 <= int(k) <= min(n, 9):
                m["answer"] = int(k) - 1
                m["event"].set()
            elif k in ("\n", "\r", c.KEY_ENTER):
                m["answer"] = m["index"]
                m["event"].set()
        elif m["kind"] == "ask":
            if k in ("\n", "\r", c.KEY_ENTER):
                m["answer"] = m["value"]
                m["event"].set()
            elif k in (c.KEY_BACKSPACE, "\x7f", "\b"):
                m["value"] = m["value"][:-1]
            elif k == "\x15":
                m["value"] = ""
            elif isinstance(k, str) and k.isprintable():
                m["value"] += k

    def key(self, k):
        c = self.c
        if self.modal:
            self.modal_key(k)
            return
        if k == c.KEY_MOUSE:
            try:
                _, _, _, _, bstate = c.getmouse()
            except c.error:
                return
            self.body.scroll(3 * wheel_delta(bstate))
            return
        if k == c.KEY_PPAGE:
            self.body.scroll(self.scr.getmaxyx()[0] // 2)
            return
        if k == c.KEY_NPAGE:
            self.body.scroll(-(self.scr.getmaxyx()[0] // 2))
            return
        if k == c.KEY_END:
            self.body.offset = 0
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
                self.input = (opts[0] + " ") if len(opts) == 1 else os.path.commonprefix(opts)
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
        c = self.c
        self.scr.timeout(90)
        self.scr.keypad(True)
        try:  # capture the wheel so it scrolls the conversation instead of sending ↑/↓ (history)
            c.mousemask(c.ALL_MOUSE_EVENTS | c.REPORT_MOUSE_POSITION)
            c.mouseinterval(0)
        except Exception:  # noqa: BLE001
            pass
        while not self.quit:
            self.draw()
            try:
                k = self.scr.get_wch()
            except c.error:
                continue
            except KeyboardInterrupt:
                if self.modal:
                    self.modal["cancel"] = True
                    self.modal["event"].set()
                    continue
                if self.busy:
                    self.body.add("  (sedang bekerja; tunggu sampai selesai)", "dim")
                    continue
                break
            if k == c.KEY_RESIZE:
                continue
            self.key(k)


def run(version):
    import curses
    os.environ.setdefault("ESCDELAY", "25")

    def main(scr):
        App(scr, version).loop()
    curses.wrapper(main)
    return 0
