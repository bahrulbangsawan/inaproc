import json
import os
import tempfile
import unittest
from unittest import mock

from tests._util import run_main
import inaproc_penyedia as ip

R = "<redacted>"


class RedactTest(unittest.TestCase):
    def test_redact_nested(self):
        out = ip.redact({"accessToken": "x", "a": {"X-Amz-Signature": "y", "name": "ok", "keyword": "kertas"},
                         "list": [{"recipientKey": "k", "page": 2}], "sessionId": "s", "password": "p"})
        self.assertEqual(out, {"accessToken": R, "a": {"X-Amz-Signature": R, "name": "ok", "keyword": "kertas"},
                               "list": [{"recipientKey": R, "page": 2}], "sessionId": R, "password": R})

    def test_redact_headers(self):
        out = ip.redact_headers({"Authorization": "Bearer x", "cookie": "a=b", "Set-Cookie": "c=d",
                                 "content-type": "application/json", "x-csrf-token": "t"})
        self.assertEqual(out, {"Authorization": R, "cookie": R, "Set-Cookie": R,
                               "content-type": "application/json", "x-csrf-token": R})

    def test_redact_query_params(self):
        self.assertEqual(ip.redact_url("https://h/p?token=a&page=2&X-Goog-Signature=zz"),
                         "https://h/p?token=%3Credacted%3E&page=2&X-Goog-Signature=%3Credacted%3E")
        self.assertEqual(ip.redact_url("https://h/p"), "https://h/p")

    def test_redact_json_string_body(self):
        body = json.dumps({"operationName": "x", "variables": {"jwtToken": "a", "name": "n"}})
        self.assertEqual(json.loads(ip.redact_body(body, "application/json")),
                         {"operationName": "x", "variables": {"jwtToken": R, "name": "n"}})


class FileModeTest(unittest.TestCase):
    def test_recording_file_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.json")
            ip.write_private_json(p, {"a": 1})
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            with open(p) as f:
                self.assertEqual(json.load(f), {"a": 1})


class StatusTest(unittest.TestCase):
    def test_status_exit_code_not_signed_in(self):
        fake_browser = mock.MagicMock()
        with mock.patch.object(ip, "open_penyedia", return_value=(fake_browser, mock.MagicMock())), \
                mock.patch.object(ip, "session_info", return_value={"signedIn": False}):
            code, out, err = run_main(["penyedia", "status"])
        self.assertEqual(code, 3)
        self.assertIn("not signed in", (out + err).lower())
        fake_browser.close.assert_called()

    def test_status_signed_in(self):
        with mock.patch.object(ip, "open_penyedia", return_value=(mock.MagicMock(), mock.MagicMock())), \
                mock.patch.object(ip, "session_info",
                                  return_value={"signedIn": True, "name": "Budi", "company": "PT Contoh",
                                                "expires": "2026-12-01T00:00:00Z"}):
            code, out, _ = run_main(["penyedia", "status"])
        self.assertEqual(code, 0)
        self.assertIn("PT Contoh", out)


class SessionInfoTest(unittest.TestCase):
    def _tab(self, js_result, url="https://penyedia.inaproc.id/products"):
        t = mock.MagicMock()
        t.url.return_value = url
        t.evaluate.return_value = js_result
        return t

    def test_script_error_is_not_signed_in(self):
        info = ip.session_info(self._tab({"signedIn": False, "error": "TypeError: Failed to fetch"}))
        self.assertFalse(info["signedIn"])

    def test_empty_session_is_not_signed_in(self):
        self.assertFalse(ip.session_info(self._tab({"signedIn": False, "endpointStatus": 200}))["signedIn"])

    def test_fallback_only_when_endpoint_missing(self):
        self.assertTrue(ip.session_info(self._tab({"signedIn": False, "endpointStatus": 404}))["signedIn"])
        self.assertFalse(ip.session_info(self._tab({"signedIn": False, "endpointStatus": 404},
                                                   url="https://penyedia.inaproc.id/auth/signin"))["signedIn"])


class RecorderFilterTest(unittest.TestCase):
    def test_keeps_api_and_uploads_drops_assets_and_trackers(self):
        keep = ip.Recorder.wanted
        self.assertTrue(keep({"url": "https://penyedia.inaproc.id/graphql", "method": "POST"}, "Fetch"))
        self.assertTrue(keep({"url": "https://storage.googleapis.com/b/o?X-Goog-Signature=1", "method": "PUT"}, "Other"))
        self.assertTrue(keep({"url": "https://penyedia.inaproc.id/products", "method": "GET"}, "Document"))
        self.assertFalse(keep({"url": "https://penyedia.inaproc.id/_next/static/x.js", "method": "GET"}, "Script"))
        self.assertFalse(keep({"url": "https://www.google-analytics.com/g/collect", "method": "POST"}, "Ping"))


if __name__ == "__main__":
    unittest.main()
