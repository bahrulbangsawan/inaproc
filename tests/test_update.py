import io
import json
import os
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from tests._util import SCRIPTS, run_main
import inaproc
import inaproc_update as up


def make_tarball(version, extra=None, prefix="inaproc-main"):
    files = {f"{prefix}/skills/inaproc/version.json": json.dumps({"version": version}),
             f"{prefix}/skills/inaproc/SKILL.md": "---\nname: inaproc\n---\nnew",
             f"{prefix}/skills/inaproc/scripts/inaproc.py": f'VERSION = "{version}"\n',
             f"{prefix}/README.md": "repo readme"}
    files.update(extra or {})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.skill = os.path.join(self.tmp.name, "skills", "inaproc")
        os.makedirs(os.path.join(self.skill, "scripts"))
        open(os.path.join(self.skill, "version.json"), "w").write('{"version": "1.2.0"}')
        open(os.path.join(self.skill, "SKILL.md"), "w").write("old")
        open(os.path.join(self.skill, "scripts", "inaproc.py"), "w").write('VERSION = "1.2.0"\n')
        open(os.path.join(self.skill, "scripts", "queries.discovered.json"), "w").write("{}")
        self.env = mock.patch.dict(os.environ, {"INAPROC_HOME": self.home}, clear=False)
        self.env.start()
        os.environ.pop("INAPROC_AUTO_UPDATE", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def fetcher(self, latest, tarball=None, fail=False):
        calls = []

        def fetch(url, limit):
            calls.append(url)
            if fail:
                raise OSError("offline")
            if url.endswith("version.json"):
                return json.dumps({"version": latest}).encode()
            return tarball if tarball is not None else make_tarball(latest)
        fetch.calls = calls
        return fetch

    def test_version_compare(self):
        self.assertTrue(up.newer("1.10.0", "1.9.0"))
        self.assertFalse(up.newer("1.2.0", "1.2.0"))
        self.assertFalse(up.newer("1.1.9", "1.2.0"))

    def test_installs_newer_version_keeps_backup_and_local_files(self):
        f = self.fetcher("1.3.0")
        msg = up.maybe_update("1.2.0", skill_dir=self.skill, fetch=f)
        self.assertIn("1.2.0 → 1.3.0", msg)
        self.assertEqual(json.load(open(os.path.join(self.skill, "version.json")))["version"], "1.3.0")
        self.assertEqual(open(os.path.join(self.skill, "SKILL.md")).read().splitlines()[-1], "new")
        self.assertTrue(os.path.exists(os.path.join(self.skill, "scripts", "queries.discovered.json")))
        self.assertFalse(os.path.exists(os.path.join(self.skill, "README.md")))
        backups = os.listdir(os.path.join(self.home, "backups"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].startswith("inaproc-1.2.0-"))

    def test_checks_at_most_once_a_day(self):
        f = self.fetcher("1.2.0")
        up.maybe_update("1.2.0", skill_dir=self.skill, fetch=f)
        up.maybe_update("1.2.0", skill_dir=self.skill, fetch=f)
        self.assertEqual(len(f.calls), 1)
        with mock.patch("time.time", return_value=time.time() + 25 * 3600):
            up.maybe_update("1.2.0", skill_dir=self.skill, fetch=f)
        self.assertEqual(len(f.calls), 2)

    def test_never_in_a_git_checkout_or_when_disabled(self):
        f = self.fetcher("9.9.9")
        os.makedirs(os.path.join(self.tmp.name, ".git"))
        self.assertIsNone(up.maybe_update("1.2.0", skill_dir=self.skill, fetch=f))
        os.rmdir(os.path.join(self.tmp.name, ".git"))
        with mock.patch.dict(os.environ, {"INAPROC_AUTO_UPDATE": "0"}):
            self.assertIsNone(up.maybe_update("1.2.0", skill_dir=self.skill, fetch=f))
        self.assertEqual(f.calls, [])

    def test_network_failure_is_silent(self):
        self.assertIsNone(up.maybe_update("1.2.0", skill_dir=self.skill, fetch=self.fetcher("x", fail=True)))

    def test_rejects_path_traversal(self):
        bad = make_tarball("1.3.0", extra={"inaproc-main/../../evil.txt": "x"})
        msg = up.maybe_update("1.2.0", skill_dir=self.skill, fetch=self.fetcher("1.3.0", bad))
        self.assertIsNone(msg)
        self.assertEqual(json.load(open(os.path.join(self.skill, "version.json")))["version"], "1.2.0")
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil.txt")))

    def test_rejects_version_mismatch(self):
        msg = up.maybe_update("1.2.0", skill_dir=self.skill, fetch=self.fetcher("1.3.0", make_tarball("1.2.5")))
        self.assertIsNone(msg)
        self.assertEqual(open(os.path.join(self.skill, "SKILL.md")).read(), "old")


class VersionFileTest(unittest.TestCase):
    def test_version_json_matches_the_script(self):
        v = json.load(open(os.path.join(os.path.dirname(SCRIPTS), "version.json")))["version"]
        self.assertEqual(v, inaproc.VERSION)

    def test_update_command_check_only(self):
        with mock.patch.object(up, "latest_version", return_value="9.9.9"), \
                mock.patch.object(up, "install") as inst:
            code, out, err = run_main(["update", "--check"])
        self.assertEqual(code, 0, err)
        self.assertIn("9.9.9", out)
        inst.assert_not_called()


if __name__ == "__main__":
    unittest.main()
