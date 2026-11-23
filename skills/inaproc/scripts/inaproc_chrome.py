"""Drive a locally installed Chrome over the DevTools Protocol, standard library only.

Used by `inaproc.py discover` and the `penyedia` commands. Chrome runs on a skill-owned profile
(`~/.inaproc/<profile>`, or under $INAPROC_HOME), because Chrome 136+ refuses remote debugging on
the user's default profile. The WebSocket client only ever connects to the loopback interface.
"""
import base64
import glob
import hashlib
import json
import os
import pathlib
import platform
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ChromeError(Exception):
    pass


# ----------------------------------------------------------------------------- paths
def inaproc_home():
    p = pathlib.Path(os.environ.get("INAPROC_HOME") or (pathlib.Path.home() / ".inaproc")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def private_dir(*parts):
    """A folder under inaproc_home(), created with mode 700."""
    p = inaproc_home().joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def profile_dir(name):
    return private_dir(name)


def _candidates():
    system = platform.system()
    home = pathlib.Path.home()
    out = []
    if system == "Darwin":
        apps = [("Google Chrome", "Google Chrome"), ("Chromium", "Chromium"),
                ("Microsoft Edge", "Microsoft Edge"), ("Brave Browser", "Brave Browser"),
                ("Google Chrome for Testing", "Google Chrome for Testing")]
        for root in ("/Applications", str(home / "Applications")):
            out += [f"{root}/{app}.app/Contents/MacOS/{binary}" for app, binary in apps]
        tail = "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    elif system == "Windows":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        rels = [r"Google\Chrome\Application\chrome.exe", r"Chromium\Application\chrome.exe",
                r"Microsoft\Edge\Application\msedge.exe", r"BraveSoftware\Brave-Browser\Application\brave.exe"]
        out += [os.path.join(r, rel) for r in roots if r for rel in rels]
        tail = "chrome.exe"
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                     "microsoft-edge", "brave-browser"):
            w = shutil.which(name)
            if w:
                out.append(w)
        tail = "chrome"
    # Last resort: Chrome for Testing downloaded by agent-browser or Playwright (newest first).
    for pattern in (str(home / ".agent-browser" / "browsers" / "chrome-*" / "**" / tail),
                    str(home / ".cache" / "ms-playwright" / "chromium-*" / "**" / tail),
                    str(home / "Library" / "Caches" / "ms-playwright" / "chromium-*" / "**" / tail)):
        out += sorted(glob.glob(pattern, recursive=True), reverse=True)
    return out


def find_chrome(explicit=None):
    want = explicit or os.environ.get("INAPROC_CHROME")
    if want:
        if os.path.exists(want):
            return want
        raise ChromeError(f"Chrome not found at {want}")
    for c in _candidates():
        if os.path.exists(c):
            return c
    raise ChromeError("no Chrome, Chromium, Edge or Brave found. Install Google Chrome, or pass "
                      "--chrome-path /path/to/chrome (or set INAPROC_CHROME)")


# ----------------------------------------------------------------------------- WebSocket (RFC 6455)
class WebSocket:
    def __init__(self):
        self.sock = None
        self._send_lock = threading.Lock()

    def connect(self, url, timeout=10):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "ws" or u.hostname not in LOOPBACK:
            raise ChromeError(f"refusing to connect to {u.hostname!r}: DevTools connections are loopback-only")
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ChromeError("WebSocket handshake failed: connection closed")
            resp += chunk
        head, self._buf = resp.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ChromeError(f"WebSocket handshake failed: {lines[0]}")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        got = [l.split(":", 1)[1].strip() for l in lines[1:] if l.lower().startswith("sec-websocket-accept:")]
        if got != [accept]:
            raise ChromeError("WebSocket handshake failed: bad Sec-WebSocket-Accept")
        self.sock.settimeout(None)
        return self

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("WebSocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode, data):
        n = len(data)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data)) if n < 4096 else _mask(data, mask)
        with self._send_lock:
            self.sock.sendall(hdr + mask + masked)

    def send_text(self, s):
        self._send_frame(0x1, s.encode("utf-8"))

    def recv_text(self, timeout=None):
        """Next complete text message. Answers pings; raises ConnectionError on close, TimeoutError on timeout."""
        self.sock.settimeout(timeout)
        try:
            parts = []
            while True:
                try:
                    h = self._recv_exact(2)
                except socket.timeout:
                    raise TimeoutError
                self.sock.settimeout(None)
                fin, op = h[0] & 0x80, h[0] & 0x0F
                n = h[1] & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._recv_exact(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._recv_exact(8))[0]
                if h[1] & 0x80:
                    mask = self._recv_exact(4)
                    data = _mask(self._recv_exact(n), mask)
                else:
                    data = self._recv_exact(n)
                if op == 0x9:
                    self._send_frame(0xA, data)
                    continue
                if op == 0xA:
                    continue
                if op == 0x8:
                    raise ConnectionError("WebSocket closed by peer")
                parts.append(data)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")
        finally:
            if self.sock:
                self.sock.settimeout(None)

    def close(self):
        if self.sock:
            try:
                self._send_frame(0x8, b"")
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


def _mask(data, mask):
    n = len(data)
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ m).to_bytes(n, "big") if n else b""


# ----------------------------------------------------------------------------- CDP connection
class Connection:
    """One DevTools WebSocket: numbered commands, responses matched by id, events to callbacks."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._handlers = {}
        self.closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while not self.closed:
            try:
                raw = self.ws.recv_text(timeout=None)
            except TimeoutError:
                continue
            except Exception:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "id" in msg:
                slot = self._pending.get(msg["id"])
                if slot:
                    slot[1] = msg
                    slot[0].set()
            elif "method" in msg:
                for cb in list(self._handlers.get(msg["method"], [])):
                    try:
                        cb(msg.get("params", {}), msg.get("sessionId"))
                    except Exception:
                        pass
        self.closed = True
        for slot in list(self._pending.values()):
            slot[0].set()

    def on(self, method, callback):
        self._handlers.setdefault(method, []).append(callback)
        return callback

    def off(self, method, callback):
        try:
            self._handlers.get(method, []).remove(callback)
        except ValueError:
            pass

    def send(self, method, params=None, session=None, timeout=30):
        with self._lock:
            self._id += 1
            mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        slot = [threading.Event(), None]
        self._pending[mid] = slot
        try:
            if self.closed:
                raise ChromeError("Chrome connection is closed")
            self.ws.send_text(json.dumps(msg))
            if not slot[0].wait(timeout):
                raise ChromeError(f"{method}: no answer from Chrome within {timeout}s")
        finally:
            self._pending.pop(mid, None)
        res = slot[1]
        if res is None:
            raise ChromeError(f"{method}: Chrome closed the connection")
        if "error" in res:
            raise ChromeError(f"{method}: {res['error'].get('message')}")
        return res.get("result", {})

    def close(self):
        self.closed = True
        try:
            self.ws.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------- browser + tabs
def _http_json(port, path, timeout=2):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def _read_port(profile_path):
    f = profile_path / "DevToolsActivePort"
    try:
        return int(f.read_text().splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


class Browser:
    def __init__(self, port, version, proc, keep_open, reused, profile_path):
        self.port = port
        self.proc = proc
        self.keep_open = keep_open
        self.reused = reused
        self.profile_path = profile_path
        self.headless = "Headless" in version.get("User-Agent", "")
        self.conn = Connection(WebSocket().connect(version["webSocketDebuggerUrl"]))
        self.conn.send("Target.setDiscoverTargets", {"discover": True})

    @classmethod
    def start(cls, profile, headed=False, chrome_path=None, proxy=None, keep_open=False, url="about:blank"):
        path = profile_dir(profile)
        port = _read_port(path)
        if port:
            try:
                version = _http_json(port, "/json/version")
                running_headless = "Headless" in version.get("User-Agent", "")
                if headed and running_headless:
                    b = cls(port, version, None, False, True, path)
                    b.shutdown()
                else:
                    return cls(port, version, None, keep_open, True, path)
            except (OSError, ValueError):
                pass
        try:
            (path / "DevToolsActivePort").unlink()
        except OSError:
            pass
        chrome = find_chrome(chrome_path)
        args = [chrome, f"--user-data-dir={path}", "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate,MediaRouter", "--disable-background-networking"]
        if platform.system() == "Darwin":
            args.append("--use-mock-keychain")
        if not headed:
            args.append("--headless=new")
        if proxy:
            args.append(f"--proxy-server={proxy}")
        args.append(url)
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline:
            port = _read_port(path)
            if port:
                try:
                    version = _http_json(port, "/json/version")
                    return cls(port, version, proc, keep_open, False, path)
                except (OSError, ValueError):
                    pass
            if proc.poll() is not None:
                raise ChromeError(f"Chrome exited at start (code {proc.returncode}). Is another Chrome "
                                  f"using {path} without remote debugging? Close it and retry.")
            time.sleep(0.2)
        proc.kill()
        raise ChromeError("Chrome started but its DevTools port never appeared")

    # tabs ---------------------------------------------------------------
    def _attach(self, target_id):
        sid = self.conn.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        return Tab(self, target_id, sid)

    def new_tab(self, url="about:blank"):
        tid = self.conn.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        tab = self._attach(tid)
        if not url.startswith("about:"):
            tab.navigate(url)  # navigate after attaching so wait_load() knows which page to wait for
        return tab

    def targets(self):
        return [t for t in self.conn.send("Target.getTargets")["targetInfos"] if t.get("type") == "page"]

    def tabs(self, url_prefix=""):
        return [self._attach(t["targetId"]) for t in self.targets() if t.get("url", "").startswith(url_prefix)]

    # lifetime -----------------------------------------------------------
    @property
    def alive(self):
        return not self.conn.closed

    def shutdown(self):
        """Close Chrome itself."""
        try:
            self.conn.send("Browser.close", timeout=5)
        except ChromeError:
            pass
        self.conn.close()
        if self.proc:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        else:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    _http_json(self.port, "/json/version", timeout=0.5)
                    time.sleep(0.2)
                except (OSError, ValueError):
                    break

    def close(self):
        """End this command's use of Chrome: quit it if this command launched it, else just disconnect."""
        if self.proc and not self.keep_open:
            self.shutdown()
        else:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Tab:
    def __init__(self, browser, target_id, session_id):
        self.browser = browser
        self.target_id = target_id
        self.session_id = session_id
        self.closed = False
        self._inflight = set()
        self._last_activity = time.time()
        self._net = False
        self._expect = None
        conn = browser.conn
        conn.on("Target.targetDestroyed", self._on_destroyed)
        conn.on("Target.detachedFromTarget", self._on_detached)

    def _on_destroyed(self, params, sid):
        if params.get("targetId") == self.target_id:
            self.closed = True

    def _on_detached(self, params, sid):
        if params.get("sessionId") == self.session_id:
            self.closed = True

    def send(self, method, params=None, timeout=30):
        return self.browser.conn.send(method, params, session=self.session_id, timeout=timeout)

    def on(self, event, callback):
        """Subscribe to an event from this tab only; callback(params)."""
        def cb(params, sid):
            if sid == self.session_id:
                callback(params)
        return self.browser.conn.on(event, cb)

    def evaluate(self, js, await_promise=True, timeout=60):
        r = self.send("Runtime.evaluate", {"expression": js, "awaitPromise": await_promise,
                                           "returnByValue": True, "userGesture": True}, timeout=timeout)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            msg = (d.get("exception") or {}).get("description") or d.get("text")
            raise ChromeError(f"page script failed: {msg}")
        return r.get("result", {}).get("value")

    def navigate(self, url):
        self._expect = None if url.startswith("about:") else url
        self.send("Page.navigate", {"url": url})

    def url(self):
        for t in self.browser.targets():
            if t["targetId"] == self.target_id:
                return t.get("url", "")
        return ""

    def wait_load(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st = self.evaluate("[location.href, document.readyState]", await_promise=False, timeout=5)
                if self._expect and st[0].startswith("about:"):
                    st = None  # the navigation has not committed yet
                if st and st[1] == "complete":
                    self._expect = None
                    return True
            except ChromeError:
                pass
            time.sleep(0.2)
        return False

    def enable_network(self):
        if self._net:
            return
        self._net = True

        def started(p):
            self._inflight.add(p.get("requestId"))
            self._last_activity = time.time()

        def ended(p):
            self._inflight.discard(p.get("requestId"))
            self._last_activity = time.time()

        self.on("Network.requestWillBeSent", started)
        self.on("Network.loadingFinished", ended)
        self.on("Network.loadingFailed", ended)
        self.send("Network.enable", {"maxPostDataSize": 1024 * 1024})

    def block_urls(self, patterns):
        self.enable_network()
        self.send("Network.setBlockedURLs", {"urls": list(patterns)})

    def wait_network_idle(self, quiet_ms=500, timeout=60):
        self.enable_network()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._inflight and (time.time() - self._last_activity) * 1000 >= quiet_ms:
                return True
            time.sleep(0.1)
        return False

    def page_fetch(self, url, method="POST", headers=None, body=None, timeout=60):
        """fetch() from inside this page (its cookies, its origin) -> {status, headers, text}."""
        js = ("(async()=>{const r=await fetch(%s,{method:%s,headers:%s,body:%s,credentials:'include'});"
              "const t=await r.text();const h={};r.headers.forEach((v,k)=>{h[k]=v});"
              "return {status:r.status,headers:h,text:t};})()"
              % (json.dumps(url), json.dumps(method), json.dumps(headers or {}),
                 "null" if body is None else json.dumps(body)))
        return self.evaluate(js, timeout=timeout)

    def close(self):
        try:
            self.browser.conn.send("Target.closeTarget", {"targetId": self.target_id}, timeout=5)
        except ChromeError:
            pass
        self.closed = True
