"""Terminal UI for the standalone `inaproc` CLI: colours, logo, tables, live step list, prompts.

Standard library only. Colour and animation turn off by themselves when the stream is not a terminal, when
NO_COLOR is set, or with INAPROC_PLAIN=1, so piping output into a file stays clean.
"""
import getpass
import itertools
import os
import re
import shutil
import sys
import threading
import time
import unicodedata

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

LOGO = [
    "██╗███╗   ██╗ █████╗ ██████╗ ██████╗  ██████╗  ██████╗",
    "██║████╗  ██║██╔══██╗██╔══██╗██╔══██╗██╔═══██╗██╔════╝",
    "██║██╔██╗ ██║███████║██████╔╝██████╔╝██║   ██║██║     ",
    "██║██║╚██╗██║██╔══██║██╔═══╝ ██╔══██╗██║   ██║██║     ",
    "██║██║ ╚████║██║  ██║██║     ██║  ██║╚██████╔╝╚██████╗",
    "╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝ ╚═════╝  ╚═════╝",
]


def vlen(s):
    """Visible width: ANSI codes take no room, wide (CJK/emoji) characters take two."""
    s = ANSI.sub("", str(s))
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1 for c in s)


def term_width(default=100):
    try:
        return max(40, shutil.get_terminal_size((default, 24)).columns)
    except (OSError, ValueError):
        return default


def color_enabled(stream):
    if os.environ.get("NO_COLOR") or os.environ.get("INAPROC_PLAIN"):
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


class Style:
    CODES = {"bold": "1", "dim": "2", "italic": "3", "under": "4", "red": "31", "green": "32", "yellow": "33",
             "blue": "34", "magenta": "35", "cyan": "36", "white": "97", "gray": "90", "bred": "91", "bgreen": "92"}

    def __init__(self, stream=None):
        self.on = color_enabled(stream if stream is not None else sys.stdout)

    def paint(self, text, *names):
        if not self.on or not names:
            return str(text)
        return "\x1b[" + ";".join(self.CODES[n] for n in names) + "m" + str(text) + "\x1b[0m"

    def __getattr__(self, name):
        if name in Style.CODES:
            return lambda text: self.paint(text, name)
        raise AttributeError(name)


def banner(version, subtitle="Katalog INAPROC & akun penyedia Anda, dari terminal", stream=None):
    stream = stream or sys.stdout
    st = Style(stream)
    shades = ["bred", "bred", "red", "white", "white", "gray"]  # merah-putih
    print("", file=stream)
    for line, shade in zip(LOGO, shades):
        print("  " + st.paint(line, "bold", shade), file=stream)
    print("  " + st.dim(f"{subtitle} · v{version}"), file=stream)
    print("", file=stream)


def _fit(text, width):
    text = str(text)
    if vlen(text) <= width:
        return text + " " * (width - vlen(text))
    out, w = "", 0
    for ch in ANSI.sub("", text):
        cw = vlen(ch)
        if w + cw > width - 1:
            break
        out, w = out + ch, w + cw
    return out + "…" + " " * (width - w - 1)


def table(headers, rows, color=None, max_width=None, stream=None):
    """Rounded box table. Long columns are shortened (with …) so it fits the terminal."""
    st = Style(stream) if color is None else Style(None)
    if color is False:
        st.on = False
    elif color is True:
        st.on = True
    rows = [["-" if c is None or c == "" else str(c).replace("\n", " ") for c in r] for r in rows]
    widths = [max([vlen(h)] + [vlen(r[i]) for r in rows if i < len(r)]) for i, h in enumerate(headers)]
    limit = (max_width or term_width()) - (3 * len(headers) + 1)
    while sum(widths) > limit and max(widths) > 6:
        i = widths.index(max(widths))
        widths[i] -= 1

    def line(l, m, r):
        return st.gray(l + m.join("─" * (w + 2) for w in widths) + r)

    def row(cells, header=False):
        parts = []
        for i, w in enumerate(widths):
            cell = _fit(cells[i] if i < len(cells) else "", w)
            parts.append(" " + (st.paint(cell, "bold", "cyan") if header else cell) + " ")
        bar = st.gray("│")
        return bar + bar.join(parts) + bar

    out = [line("╭", "┬", "╮"), row(headers, True), line("├", "┼", "┤")]
    out += [row(r) for r in rows]
    out.append(line("╰", "┴", "╯"))
    return "\n".join(out)


def _style_for(color, stream):
    st = Style(stream)
    if color is not None:
        st.on = bool(color)  # an explicit choice always wins (False = never any escape codes)
    return st


def kv_table(rows, color=None, stream=None, max_width=None, widths=None):
    """Two-column box for label/value pairs (no header row). Links are never shortened."""
    st = _style_for(color, stream)
    rows = [[str(a), "-" if b in (None, "") else str(b)] for a, b in rows]
    width = max_width or term_width()
    w0 = widths[0] if widths else max(vlen(r[0]) for r in rows)
    w1 = widths[1] if widths else min(max(vlen(r[1]) for r in rows), max(20, width - w0 - 7))
    bar = st.gray("│")
    out = [st.gray("╭" + "─" * (w0 + 2) + "┬" + "─" * (w1 + 2) + "╮")]
    for a, b in rows:
        value = b if b.startswith(("http://", "https://")) and vlen(b) > w1 else _fit(b, w1)
        out.append(bar + " " + st.bold(_fit(a, w0)) + " " + bar + " " + value + " " + bar)
    out.append(st.gray("╰" + "─" * (w0 + 2) + "┴" + "─" * (w1 + 2) + "╯"))
    return "\n".join(out)


def _natural(headers, rows):
    return sum(max([vlen(h)] + [vlen(r[i]) for r in rows if i < len(r)]) for i, h in enumerate(headers)) \
        + 3 * len(headers) + 1


def auto_table(headers, rows, color=None, max_width=None, stream=None):
    """Always a structured table: as is when it fits; turned sideways (fields as rows) for a few wide rows; one
    labelled detail table per row when there are many. Links are never cut."""
    width = max_width or term_width()
    rows = [["-" if c in (None, "") else str(c) for c in r] + ["-"] * (len(headers) - len(r)) for r in rows]
    if headers and all(not str(h).strip() for h in headers):
        return kv_table([r[:2] for r in rows], color=color, stream=stream, max_width=width)
    if not rows or _natural(headers, rows) <= width:
        return table(headers, rows, color=color, max_width=width, stream=stream)
    low = [str(h).strip().lower() for h in headers]
    num_i = next((i for i, h in enumerate(low) if h in ("#", "no", "no.")), None)
    title_i = next((i for i, h in enumerate(low) if i != num_i), 0)
    if len(rows) <= 4:
        side_head = [""] + [(r[num_i] + ". " if num_i is not None else "") + r[title_i] for r in rows]
        side_rows = [[h] + [r[i] for r in rows] for i, h in enumerate(headers) if i not in (num_i, title_i)]
        if _natural(side_head, side_rows) <= width or len(rows) == 1:
            if len(rows) == 1:
                return kv_table([[headers[title_i], rows[0][title_i]]] + side_rows, color=color, stream=stream,
                                max_width=width)
            urls = [r for r in side_rows if any(c.startswith("http") for c in r[1:])]
            body = [r for r in side_rows if r not in urls]
            out = table(side_head, body, color=color, max_width=width, stream=stream)
            for r in urls:
                out += "\n" + "\n".join(f"  {side_head[j]}: {c}" for j, c in enumerate(r[1:], 1))
            return out
    all_pairs = []
    for r in rows:
        pairs = [[headers[i], r[i]] for i in range(len(headers)) if i != num_i]
        if num_i is not None:
            pairs[0][1] = f"{r[num_i]}. {pairs[0][1]}"
        all_pairs.append(pairs)
    w0 = max(vlen(p[0]) for pairs in all_pairs for p in pairs)
    w1 = min(max(vlen(p[1]) for pairs in all_pairs for p in pairs if not p[1].startswith("http")),
             max(20, width - w0 - 7))
    w1 = max(w1, min(width - w0 - 7, max(vlen(p[1]) for pairs in all_pairs for p in pairs)))
    return "\n".join(kv_table(pairs, color=color, stream=stream, max_width=width, widths=(w0, w1))
                     for pairs in all_pairs)


def cards(headers, rows, color=None, max_width=None, stream=None):  # kept for callers; now structured tables
    return auto_table(headers, rows, color=color, max_width=max_width, stream=stream)


def panel(title, lines, kind="info", stream=None):
    """A framed message: kind info | ok | warn | error | step."""
    stream = stream or sys.stdout
    st = Style(stream)
    tint = {"info": "cyan", "ok": "green", "warn": "yellow", "error": "red", "step": "magenta"}[kind]
    icon = {"info": "ℹ", "ok": "✓", "warn": "!", "error": "✗", "step": "➜"}[kind]
    width = min(term_width(), 96) - 4
    body = []
    for l in lines:
        body += _wrap(str(l), width - 2) or [""]
    inner = max([vlen(title) + 4] + [vlen(b) + 2 for b in body])
    inner = min(max(inner, 30), width)
    top = st.paint("╭─ ", tint) + st.paint(f"{icon} {title}", "bold", tint) + " " + \
        st.paint("─" * max(0, inner - vlen(title) - 4) + "╮", tint)
    print(top, file=stream)
    for b in body:
        print(st.paint("│ ", tint) + _fit(b, inner - 1) + st.paint(" │", tint), file=stream)
    print(st.paint("╰" + "─" * (inner + 1) + "╯", tint), file=stream)


def _wrap(text, width):
    indent = text[:len(text) - len(text.lstrip(" "))]
    words, lines, cur = text.lstrip(" ").split(" "), [], indent
    for w in words:
        if cur.strip() and vlen(cur) + 1 + vlen(w) > width:
            lines.append(cur)
            cur = indent + w
        else:
            cur = (cur + " " + w) if cur.strip() else cur + w
    if cur:
        lines.append(cur)
    return lines


class Steps:
    """Live list of background steps. In a terminal: a spinner on the current step, a ✓/✗ line when it ends.
    Elsewhere: one plain line per finished step. Always written to stderr so stdout stays data."""

    def __init__(self, stream=None, title=None):
        self.stream = stream or sys.stderr
        self.st = Style(self.stream)
        self.live = self.st.on
        self.label = None
        self.detail = None
        self.t0 = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._drawn = False
        if title:
            print(self.st.paint(title, "bold"), file=self.stream)

    # lifecycle ------------------------------------------------------------
    def __enter__(self):
        global CURRENT
        self._prev, CURRENT = CURRENT, self
        if self.live:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        global CURRENT
        if self.label is not None:
            if exc_type is None:
                self.done()
            else:
                self.fail(str(exc) or exc_type.__name__ if exc_type is not KeyboardInterrupt else "dibatalkan")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        CURRENT = self._prev
        return False

    def _spin(self):
        for frame in itertools.cycle(SPINNER):
            if self._stop.wait(0.08):
                return
            with self._lock:
                if self.label is not None:
                    secs = time.time() - self.t0
                    extra = self.st.gray(" · " + self.detail) if self.detail else ""
                    line = self.label + extra
                    room = term_width() - 10
                    if vlen(line) > room:
                        line = _fit(line, room).rstrip()
                    self._write("\r\x1b[2K" + self.st.cyan(frame) + " " + line + self.st.gray(f"  {secs:.1f}s"))
                    self._drawn = True

    def _write(self, s):
        try:
            self.stream.write(s)
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def _clear(self):
        if self.live and self._drawn:
            self._write("\r\x1b[2K")
            self._drawn = False

    # steps ----------------------------------------------------------------
    def start(self, label):
        with self._lock:
            if self.label is not None:
                self._finish("✓", "green", None)
            self.label, self.t0 = label, time.time()

    def update(self, label):
        with self._lock:
            if self.label is None:
                self.t0 = time.time()
            self.label = label

    def sub(self, text):
        """A small live detail next to the running step (e.g. which call is in flight)."""
        with self._lock:
            self.detail = text

    def done(self, detail=None):
        with self._lock:
            self._finish("✓", "green", detail)

    def fail(self, detail=None):
        with self._lock:
            self._finish("✗", "red", detail)

    def info(self, text):
        with self._lock:
            self._clear()
            self._write(self.st.gray("  " + text) + "\n")

    def _finish(self, mark, tint, detail):
        if self.label is None:
            return
        secs = time.time() - (self.t0 or time.time())
        self._clear()
        tail = (self.st.gray(" — ") + str(detail)) if detail else ""
        self._write(self.st.paint(mark, "bold", tint) + " " + self.label + tail + self.st.gray(f"  {secs:.1f}s") + "\n")
        self.label = None
        self.detail = None

    def pause(self):
        """Clear the spinner line before something else prints."""
        with self._lock:
            self._clear()


CURRENT = None  # the active Steps, so library code can report progress without knowing about the UI


def progress(label):
    """Called from the library (catalogue/penyedia calls): shows `label` as the running step, if a UI is active."""
    s = CURRENT
    if s is not None:
        if s.label is None:
            s.update(label)
        else:
            s.sub(label)


def _input():
    """input() where Ctrl-D (EOF) means "cancel", like Ctrl-C."""
    try:
        return input()
    except EOFError:
        raise KeyboardInterrupt


def _flush_stdin():
    """Drop anything typed ahead, so an earlier keystroke can't answer a later question."""
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:  # noqa: BLE001 — not a terminal / not POSIX
        pass


def ask(prompt, default=None, secret=False, stream=None):
    stream = stream or sys.stdout
    st = Style(stream)
    hint = st.gray(f" [{default}]") if default not in (None, "") else ""
    text = st.paint("? ", "bold", "magenta") + st.bold(prompt) + hint + " "
    if secret:
        try:
            value = getpass.getpass(ANSI.sub("", text) if not st.on else text)
        except EOFError:
            raise KeyboardInterrupt
    else:
        print(text, end="", file=stream, flush=True)
        value = _input()
    value = value.strip()
    return value or (default if default is not None else "")


def choose(prompt, options, default=None, stream=None):
    """Numbered menu; returns the chosen index."""
    stream = stream or sys.stdout
    st = Style(stream)
    print(st.paint("? ", "bold", "magenta") + st.bold(prompt), file=stream)
    for i, opt in enumerate(options, 1):
        mark = st.cyan("›") if default == i - 1 else " "
        print(f"  {mark} {st.cyan(str(i).rjust(2))}  {opt}", file=stream)
    while True:
        hint = f" [{default + 1}]" if default is not None else ""
        print(st.gray(f"  Pilih nomor{hint}: "), end="", file=stream, flush=True)
        raw = _input().strip()
        if not raw and default is not None:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(st.yellow(f"  Ketik angka 1–{len(options)}."), file=stream)


def confirm(prompt, default=False, stream=None, flush=False):
    """y/n question. Only an explicit y / ya / yes counts as yes. flush=True drops typed-ahead input first."""
    stream = stream or sys.stdout
    st = Style(stream)
    if flush:
        _flush_stdin()
    hint = "Y/n" if default else "y/N"
    print(st.paint("? ", "bold", "magenta") + st.bold(prompt) + st.gray(f" [{hint}] "), end="", file=stream, flush=True)
    raw = _input().strip().lower()
    if not raw:
        return default
    return raw in ("y", "ya", "yes")


def mask(secret):
    if not secret:
        return ""
    head = secret[:7] if len(secret) > 14 else secret[:3]
    return f"{head}…{secret[-4:]}"


def render_markdown(text, color=None, stream=None):
    """Light markdown for terminal answers: headings, **bold**, `code`, bullets. Tables pass through."""
    st = _style_for(color, stream)
    out = []
    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            out.append(st.paint(m.group(2), "bold", "cyan"))
            continue
        line = re.sub(r"^(\s*)[-*]\s+", lambda mm: mm.group(1) + "• ", line)
        line = re.sub(r"\*\*(.+?)\*\*", lambda mm: st.bold(mm.group(1)), line)
        line = re.sub(r"`([^`]+)`", lambda mm: st.cyan(mm.group(1)), line)
        out.append(line)
    return "\n".join(out)
