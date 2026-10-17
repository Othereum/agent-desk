"""python -m unittest discover -s tests"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

from agent_desk.core import file_lock, is_write_temp, write_text


class WriteText(unittest.TestCase):
    def test_replaces_without_leaving_temp(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "state.md"
            write_text(path, "a\n")
            write_text(path, "b\n")
            self.assertEqual(path.read_bytes(), b"b\n")
            self.assertEqual(os.listdir(path.parent), ["state.md"])

    def test_is_write_temp(self):
        self.assertTrue(is_write_temp(".state.md.123.tmp"))
        self.assertFalse(is_write_temp("state.md"))


class FileLock(unittest.TestCase):
    def test_breaks_stale_lock(self):
        name = f"test-{os.getpid()}"
        path = Path(tempfile.gettempdir()) / "agent-desk-locks" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, (0, 0))
        with file_lock(name, timeout=1):
            self.assertTrue(path.exists())
        self.assertFalse(path.exists())


class ParallelKnAdd(unittest.TestCase):
    def test_ids_are_unique(self):
        with tempfile.TemporaryDirectory() as d:
            repo, root = Path(d) / "repo", Path(d) / "root"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            env = {**os.environ, "AGENT_DESK_ROOT": str(root)}
            desk = [sys.executable, str(BIN / "desk.py"), "--cwd", str(repo)]
            subprocess.run(desk + ["project-init", "p"], env=env, check=True, capture_output=True)
            procs = [subprocess.Popen(desk + ["kn", "add", "--kind", "finding", "--title", f"t{i}", "--slug", f"s{i}"],
                                      env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                     for i in range(6)]
            for p in procs:
                self.assertEqual(p.wait(), 0, p.stderr.read().decode("utf-8", "replace"))
            ids = [f.name.split("-")[0] for f in (root / "projects" / "p" / "knowledge").glob("K*.md")]
            self.assertEqual(sorted(ids), [f"K{i:04d}" for i in range(1, 7)])


if __name__ == "__main__":
    unittest.main()
