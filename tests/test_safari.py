import json
import subprocess
import unittest

import tests._util  # noqa: F401
import inaproc_safari as sf


class FakeRunner:
    """Stands in for subprocess.run(['osascript', ...]); answers by script content."""

    def __init__(self, answers):
        self.answers = answers  # list of (needle, stdout | Exception-stderr)
        self.calls = []

    def __call__(self, argv, capture_output, text, timeout):
        self.calls.append(argv)
        script = " ".join(argv)
        for needle, out in self.answers:
            if needle in script:
                if isinstance(out, BaseException):
                    return subprocess.CompletedProcess(argv, 1, "", str(out))
                return subprocess.CompletedProcess(argv, 0, out + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "\n", "")


LIST = "11\t1\thttps://www.google.com/\n22\t3\thttps://penyedia.inaproc.id/products?page=1\n"


class SafariTest(unittest.TestCase):
    def test_find_tab_picks_penyedia(self):
        s = sf.Safari(runner=FakeRunner([("every tab", LIST)]))
        tab = s.find_tab("https://penyedia.inaproc.id")
        self.assertEqual((tab.window_id, tab.index), (22, 3))
        self.assertIsNone(s.find_tab("https://nope.example"))

    def test_js_is_passed_as_an_argument_not_spliced_into_the_script(self):
        r = FakeRunner([("every tab", LIST), ("do JavaScript", json.dumps(2))])
        tab = sf.Safari(runner=r).find_tab("https://penyedia.inaproc.id")
        evil = '1+1 " & (do shell script "rm -rf ~") & "'
        tab.evaluate(evil)
        argv = r.calls[-1]
        self.assertTrue(any(evil in a for a in argv[argv.index("-e") + 1:] if not a.startswith(("on run", "tell", "end"))))
        self.assertFalse(any("rm -rf" in a for a in argv if a.startswith(("tell", "set", "return"))))

    def test_evaluate_returns_json_value(self):
        r = FakeRunner([("every tab", LIST), ("do JavaScript", json.dumps({"a": [1, 2]}))])
        tab = sf.Safari(runner=r).find_tab("https://penyedia.inaproc.id")
        self.assertEqual(tab.evaluate("({a:[1,2]})"), {"a": [1, 2]})

    def test_page_fetch_uses_sync_same_origin_xhr(self):
        r = FakeRunner([("every tab", LIST),
                        ("do JavaScript", json.dumps({"status": 200, "headers": {}, "text": '{"data":1}'}))])
        tab = sf.Safari(runner=r).find_tab("https://penyedia.inaproc.id")
        res = tab.page_fetch("https://penyedia.inaproc.id/graphql", body='{"q":1}',
                             headers={"content-type": "application/json"})
        self.assertEqual(res["status"], 200)
        js = r.calls[-1][-3]
        self.assertIn("XMLHttpRequest", js)
        self.assertIn('"POST","https://penyedia.inaproc.id/graphql",false', js.replace(", ", ","))

    def test_permission_error_explains_the_safari_setting(self):
        err = RuntimeError("execution error: Safari got an error: You must enable the 'Allow JavaScript from "
                           "Apple Events' option in Safari's Develop menu to use 'do JavaScript'. (8)")
        r = FakeRunner([("every tab", LIST), ("do JavaScript", err)])
        tab = sf.Safari(runner=r).find_tab("https://penyedia.inaproc.id")
        with self.assertRaises(sf.SafariError) as cm:
            tab.evaluate("1")
        self.assertIn("Allow JavaScript from Apple Events", str(cm.exception))
        self.assertIn("Developer Settings", str(cm.exception))

    def test_wait_load_uses_one_page_call_and_no_tab_listing(self):
        r = FakeRunner([("every tab", LIST),
                        ("do JavaScript", json.dumps(["https://penyedia.inaproc.id/products", "complete"]))])
        tab = sf.Safari(runner=r).find_tab("https://penyedia.inaproc.id")
        before = len(r.calls)
        self.assertTrue(tab.wait_load())
        self.assertEqual(len(r.calls) - before, 1)
        self.assertIn("do JavaScript", " ".join(r.calls[-1]))

    def test_tab_listing_reads_urls_per_window_in_one_event(self):
        self.assertTrue(any("URL of every tab of w" in l for l in sf.LIST_SCRIPT))

    def test_tab_gone_means_closed(self):
        r = FakeRunner([("every tab", LIST)])
        tab = sf.Safari(runner=r).find_tab("https://penyedia.inaproc.id")
        r.answers = [("every tab", "11\t1\thttps://www.google.com/\n")]
        self.assertTrue(tab.closed)


if __name__ == "__main__":
    unittest.main()
