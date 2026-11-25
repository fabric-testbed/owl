"""scripts/build_pyz.py produces a runnable single-file owl."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestBuildPyz(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pyz = os.path.join(cls.tmp.name, "owl.pyz")
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_pyz.py"), "-o", cls.pyz],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_pyz(self, *args):
        return subprocess.run([sys.executable, self.pyz, *args], capture_output=True, text=True,
                              cwd=self.tmp.name)  # not the repo, so the archive's copy of owl is used

    def test_contents(self):
        names = zipfile.ZipFile(self.pyz).namelist()
        self.assertIn("__main__.py", names)
        self.assertIn("owl/_build.py", names)
        self.assertIn("owl/daemon.py", names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.startswith("tests/")], names)

    def test_version_names_the_commit(self):
        r = self.run_pyz("--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"^owl \d+\.\d+\.\d+\S* \(git ([0-9a-f]{7,}(\+dirty)?|unknown)\)$")

    @unittest.skipUnless(sys.platform.startswith("linux"), "owl runs only on Linux")
    def test_daemon_check(self):
        cfg = os.path.join(self.tmp.name, "owl.json")
        with open(cfg, "w") as f:
            json.dump({"label": "X", "peers": [{"addr": "127.0.0.1"}]}, f)
        r = self.run_pyz("daemon", "-c", cfg, "--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK (label X", r.stdout)


if __name__ == "__main__":
    unittest.main()
