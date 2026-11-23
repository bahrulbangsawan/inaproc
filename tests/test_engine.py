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
