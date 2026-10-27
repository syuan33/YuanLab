import os
import subprocess
import sys
import textwrap
from pathlib import Path

from tests.fixtures import StrictCase

ROOT = Path(__file__).resolve().parents[1]


class StrictMode(StrictCase):
    def test_leaky_test_really_fails(self):
        """A test that leaks a subprocess must FAIL under StrictCase (plain -W error would only print a warning)."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            Path(d, "leaky_check.py").write_text(textwrap.dedent('''
                import subprocess, unittest
                from tests.fixtures import StrictCase
                class Leaky(StrictCase):
                    def test_leak(self):
                        subprocess.Popen(["sleep", "2"])
                if __name__ == "__main__":
                    unittest.main()
            '''))
            env = dict(os.environ, PYTHONPATH=f"{ROOT}:{ROOT / 'src'}")
            r = subprocess.run([sys.executable, "-X", "dev", "-W", "error", "leaky_check.py"], cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("resource leak", r.stderr)
