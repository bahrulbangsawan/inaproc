import http.server
import json
import os
import tempfile
import threading
import unittest

from tests._util import run_main
import inaproc_chrome as ic

PAGE = b"""<html><body><script>
fetch('/graphql',{method:'POST',headers:{'content-type':'application/json'},
 body:JSON.stringify({operationName:'localOp',query:'query localOp{x}',variables:{a:1}})});
</script></body></html>"""


class _Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b'{"data":{"x":1}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _chrome():
    try:
        return ic.find_chrome()
    except ic.ChromeError:
        return None


class DiscoverTest(unittest.TestCase):
    def test_discover_rejects_other_domains(self):
        code, _, err = run_main(["discover", "https://example.com/"])
        self.assertEqual(code, 1)
        self.assertIn("only opens katalog.inaproc.id", err)

    def test_discover_opens_a_window_by_default(self):
        import inaproc
        p = inaproc.build_parser()
        self.assertFalse(p.parse_args(["discover"]).headless)
        self.assertTrue(p.parse_args(["discover", "--headless"]).headless)
        p.parse_args(["discover", "--headed"])  # v1.0 flag still accepted

    def test_trackers_list(self):
        import inaproc
        self.assertIn("*doubleclick.net*", inaproc.TRACKERS)
        self.assertEqual(len(inaproc.TRACKERS), 5)

    @unittest.skipUnless(_chrome(), "no Chrome found")
    def test_capture_graphql_local(self):
        import inaproc
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        tmp = tempfile.TemporaryDirectory()
        old = os.environ.get("INAPROC_HOME")
        os.environ["INAPROC_HOME"] = tmp.name
        try:
            with ic.Browser.start("chrome-public", headed=False) as b:
                tab = b.new_tab("about:blank")
                ops = inaproc.capture_graphql(tab, ["http://127.0.0.1:%d/" % srv.server_address[1]], wait_s=0)
            self.assertEqual([o["operationName"] for o in ops], ["localOp"])
            self.assertEqual(ops[0]["status"], 200)
            self.assertEqual(ops[0]["query"], "query localOp{x}")
            self.assertEqual(ops[0]["exampleVariables"], {"a": 1})
        finally:
            srv.shutdown()
            srv.server_close()
            if old is None:
                os.environ.pop("INAPROC_HOME", None)
            else:
                os.environ["INAPROC_HOME"] = old
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
