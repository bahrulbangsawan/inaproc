import base64
import hashlib
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
import http.server

import tests._util  # noqa: F401  (adds scripts/ to sys.path)
import inaproc_chrome as ic


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_exact(c, n):
    b = b""
    while len(b) < n:
        chunk = c.recv(n - len(b))
        if not chunk:
            raise ConnectionError
        b += chunk
    return b


def _read_frame(c):
    h = _recv_exact(c, 2)
    fin, op = h[0] & 0x80, h[0] & 0x0F
    n = h[1] & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(c, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(c, 8))[0]
    mask = _recv_exact(c, 4)
    data = bytearray(_recv_exact(c, n))
    for i in range(n):
        data[i] ^= mask[i % 4]
    return fin, op, bytes(data)


def _send_frame(c, op, data, fin=True):
    n = len(data)
    hdr = bytes([(0x80 if fin else 0) | op])
    if n < 126:
        hdr += bytes([n])
    elif n < 65536:
        hdr += bytes([126]) + struct.pack(">H", n)
    else:
        hdr += bytes([127]) + struct.pack(">Q", n)
    c.sendall(hdr + data)


class EchoServer(threading.Thread):
    """Minimal RFC 6455 server: echoes each text message back split into 3 continuation frames."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.pinged = False

    def run(self):
        c, _ = self.sock.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        key = [l.split(b":", 1)[1].strip() for l in req.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        _send_frame(c, 0x9, b"hi")  # ping; client must pong (whenever it next reads)
        while True:
            try:
                fin, op, data = _read_frame(c)
            except ConnectionError:
                break
            if op == 0xA:
                self.pinged = self.pinged or data == b"hi"
                continue
            if op == 0x8:
                _send_frame(c, 0x8, b"")
                break
            third = max(1, len(data) // 3)
            parts = [data[:third], data[third:2 * third], data[2 * third:]]
            _send_frame(c, 0x1, parts[0], fin=False)
            _send_frame(c, 0x0, parts[1], fin=False)
            _send_frame(c, 0x0, parts[2], fin=True)
        c.close()


class WebSocketTest(unittest.TestCase):
    def test_ws_echo(self):
        srv = EchoServer()
        srv.start()
        ws = ic.WebSocket()
        ws.connect(f"ws://127.0.0.1:{srv.port}/devtools/browser/x")
        for size in (10, 70 * 1024, 70000):
            msg = ("x" * (size - 1)) + "!"
            ws.send_text(msg)
            self.assertEqual(ws.recv_text(timeout=5), msg)
        ws.close()
        self.assertTrue(srv.pinged)

    def test_ws_refuses_remote_host(self):
        with self.assertRaises(ic.ChromeError):
            ic.WebSocket().connect("ws://example.com:9222/devtools/browser/x")


class FakeWS:
    def __init__(self):
        self.sent = []
        self.inbox = []
        self.cv = threading.Condition()

    def send_text(self, s):
        msg = json.loads(s)
        self.sent.append(msg)
        with self.cv:
            self.inbox.append(json.dumps({"method": "Some.event", "params": {"n": 1}, "sessionId": msg.get("sessionId")}))
            self.inbox.append(json.dumps({"id": msg["id"], "result": {"echo": msg["method"]},
                                          "sessionId": msg.get("sessionId")}))
            self.cv.notify_all()

    def recv_text(self, timeout=None):
        with self.cv:
            if not self.cv.wait_for(lambda: self.inbox, timeout=timeout or 0.2):
                raise TimeoutError
            return self.inbox.pop(0)

    def close(self):
        pass


class CdpDispatchTest(unittest.TestCase):
    def test_cdp_dispatch(self):
        conn = ic.Connection(FakeWS())
        events = []
        conn.on("Some.event", lambda params, sid: events.append((params, sid)))
        self.assertEqual(conn.send("A.b", {}, session="S1"), {"echo": "A.b"})
        self.assertEqual(conn.send("C.d"), {"echo": "C.d"})
        self.assertEqual(events[0], ({"n": 1}, "S1"))
        conn.close()


class FindChromeTest(unittest.TestCase):
    def test_find_chrome_env(self):
        with tempfile.NamedTemporaryFile() as f:
            old = os.environ.get("INAPROC_CHROME")
            os.environ["INAPROC_CHROME"] = f.name
            try:
                self.assertEqual(ic.find_chrome(), f.name)
                os.environ["INAPROC_CHROME"] = "/nope/chrome"
                with self.assertRaises(ic.ChromeError):
                    ic.find_chrome()
            finally:
                if old is None:
                    os.environ.pop("INAPROC_CHROME", None)
                else:
                    os.environ["INAPROC_CHROME"] = old

    def test_profile_dir_mode(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("INAPROC_HOME")
            os.environ["INAPROC_HOME"] = d
            try:
                p = ic.profile_dir("chrome-public")
                self.assertTrue(p.is_dir())
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o700)
            finally:
                if old is None:
                    os.environ.pop("INAPROC_HOME", None)
                else:
                    os.environ["INAPROC_HOME"] = old


def _chrome():
    try:
        return ic.find_chrome()
    except ic.ChromeError:
        return None


class _Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            import time
            time.sleep(1.5)
        body = b"<html><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        got = self.rfile.read(n).decode()
        body = json.dumps({"got": got, "method": "POST"}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@unittest.skipUnless(_chrome(), "no Chrome found")
class ChromeLiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("INAPROC_HOME")
        os.environ["INAPROC_HOME"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("INAPROC_HOME", None)
        else:
            os.environ["INAPROC_HOME"] = self.old
        self.tmp.cleanup()

    def test_launch_and_eval(self):
        with ic.Browser.start("chrome-test", headed=False) as b:
            tab = b.new_tab("about:blank")
            self.assertEqual(tab.evaluate("1+1"), 2)

    def test_wait_load_waits_for_the_real_page(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            with ic.Browser.start("chrome-test", headed=False) as b:
                tab = b.new_tab(base + "/slow")
                self.assertTrue(tab.wait_load())
                self.assertEqual(tab.evaluate("location.href"), base + "/slow")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_reuse_running_instance(self):
        b1 = ic.Browser.start("chrome-test", headed=False, keep_open=True)
        try:
            b2 = ic.Browser.start("chrome-test", headed=False, keep_open=True)
            self.assertEqual(b1.port, b2.port)
            self.assertTrue(b2.reused)
            b2.close()
        finally:
            b1.shutdown()

    def test_page_fetch_local(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            with ic.Browser.start("chrome-test", headed=False) as b:
                tab = b.new_tab(base + "/")
                tab.wait_load()
                r = tab.page_fetch(base + "/api", body='{"a":1}', headers={"content-type": "application/json"})
                self.assertEqual(r["status"], 201)
                self.assertEqual(json.loads(r["text"]), {"got": '{"a":1}', "method": "POST"})
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
