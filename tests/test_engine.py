import unittest

from tests._util import run_main


class EngineCompatTest(unittest.TestCase):
    def test_scrapling_engine_warns(self):
        code, _, err = run_main(["ops", "--engine", "scrapling"])
        self.assertEqual(code, 0)
        self.assertIn("Scrapling support was removed in v1.1", err)

    def test_impersonate_flag_accepted(self):
        code, _, err = run_main(["ops", "--impersonate", "chrome", "--http3"])
        self.assertEqual(code, 0)
        self.assertIn("--impersonate", err)
        self.assertIn("--http3", err)

    def test_engine_choices(self):
        for e in ("auto", "stdlib"):
            code, _, err = run_main(["ops", "--engine", e])
            self.assertEqual(code, 0)
            self.assertEqual(err, "")

    def test_no_scrapling_code_left(self):
        import inaproc
        self.assertFalse(hasattr(inaproc, "_scrapling_available"))


if __name__ == "__main__":
    unittest.main()


import gzip
import http.server
import json
import threading


ORDER = ["Host", "Connection", "Content-Length", "sec-ch-ua-platform", "User-Agent", "sec-ch-ua", "Content-Type",
         "sec-ch-ua-mobile", "Accept", "Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest", "Referer",
         "Accept-Encoding", "Accept-Language"]


class HeadersTest(unittest.TestCase):
    def test_header_order(self):
        import inaproc
        names = [k for k, _ in inaproc.chrome_headers(10, None)]
        self.assertEqual(names, ORDER)
        names = [k for k, _ in inaproc.chrome_headers(10, "a=1")]
        self.assertEqual(names, ORDER + ["Cookie"])

    def test_header_values(self):
        import inaproc
        h = dict(inaproc.chrome_headers(42, None))
        self.assertEqual(h["Content-Length"], "42")
        self.assertEqual(h["Host"], "katalog.inaproc.id")
        self.assertEqual(h["Accept-Encoding"], "gzip, deflate")
        self.assertEqual(h["Accept-Language"], "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7")
        self.assertEqual(h["Sec-Fetch-Site"], "same-origin")
        self.assertEqual(h["Sec-Fetch-Mode"], "cors")
        self.assertEqual(h["Sec-Fetch-Dest"], "empty")
        self.assertEqual(h["sec-ch-ua-mobile"], "?0")
        self.assertEqual(h["sec-ch-ua-platform"], '"%s"' % inaproc.chrome_platform())

    def test_ua_matches_sec_ch_ua(self):
        import inaproc
        h = dict(inaproc.chrome_headers(1, None))
        self.assertIn(f"Chrome/{inaproc.CHROME_MAJOR}.0.0.0", h["User-Agent"])
        self.assertIn(f'v="{inaproc.CHROME_MAJOR}"', h["sec-ch-ua"])
        plat = inaproc.chrome_platform()
        marker = {"macOS": "Macintosh", "Windows": "Windows NT", "Linux": "X11; Linux"}[plat]
        self.assertIn(marker, h["User-Agent"])

    def test_cookie_store_roundtrip(self):
        import inaproc
        c = inaproc.CookieStore()
        self.assertIsNone(c.header())
        c.update(["a=1; Path=/; HttpOnly", "b=two; Secure"])
        c.update(["a=3"])
        self.assertEqual(c.header(), "a=3; b=two")


class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = self.rfile.read(n)
        _Handler.seen.append((list(self.headers.keys()), body))
        data = gzip.compress(json.dumps({"data": {"ok": True}}).encode())
        self.send_response(200)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", "sid=xyz; Path=/")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class LocalServerTest(unittest.TestCase):
    def setUp(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        _Handler.seen.clear()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_gzip_decoded_and_cookie_sent_back(self):
        import inaproc
        url = "http://127.0.0.1:%d/graphql" % self.srv.server_address[1]
        c = inaproc.Client(min_interval=0.5, endpoint=url)
        try:
            st, text = c._post(b'{"x":1}')
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(text), {"data": {"ok": True}})
            c._post(b'{"x":2}')
        finally:
            c.close()
        first, second = _Handler.seen
        self.assertEqual(first[0][:3], ["Host", "Connection", "Content-Length"])
        self.assertNotIn("Cookie", first[0])
        self.assertEqual(second[0][-1], "Cookie")
