"""python -m unittest discover -s tests"""

import json
import os
import subprocess
import time
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

from agent_desk.core import DeskError, file_lock, is_write_temp, move_dir, root_lock, root_lock_name, write_text

os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # tests may run in a Claude Code session: leave its task alone


class MoveDir(unittest.TestCase):
    """Windows refuses a folder rename while a sync client has a file in it open."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.src = Path(self._tmp.name) / "tasks" / "t1"
        self.src.mkdir(parents=True)
        (self.src / "state.md").write_text("x", encoding="utf-8")
        self.dest = Path(self._tmp.name) / "archive" / "t1"
        self.addCleanup(self._tmp.cleanup)

    def refuse(self, times):
        real, calls = os.rename, []
        def rename(a, b):
            calls.append(a)
            if len(calls) <= times:
                raise PermissionError(5, "access denied")
            real(a, b)
        return mock.patch("agent_desk.core.os.rename", rename), calls

    def test_retries_until_the_folder_is_free(self):
        patch, calls = self.refuse(3)
        with patch:
            move_dir(self.src, self.dest)
        self.assertEqual(len(calls), 4)
        self.assertFalse(self.src.exists())
        self.assertTrue((self.dest / "state.md").exists())

    def test_gives_up_without_leaving_two_copies(self):
        patch, _ = self.refuse(10**6)
        with patch, mock.patch("agent_desk.core.BUSY_WAIT", 0.05):
            with self.assertRaises(DeskError):
                move_dir(self.src, self.dest)
        self.assertTrue((self.src / "state.md").exists())
        self.assertFalse(self.dest.exists())


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


class RootLock(unittest.TestCase):
    def test_name_is_per_data_root(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a", Path(d) / "b"
            self.assertNotEqual(root_lock_name(a), root_lock_name(b))
            self.assertEqual(root_lock_name(a), root_lock_name(Path(d) / "x" / ".." / "a"))
            if os.name == "nt":
                self.assertEqual(root_lock_name(a), root_lock_name(Path(str(a).upper())))


class DeskRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.repo, self.root = d / "repo", d / "root"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.env = {**os.environ, "AGENT_DESK_ROOT": str(self.root)}
        self.desk = [sys.executable, str(BIN / "desk.py"), "--cwd", str(self.repo)]
        self.run_desk("project-init", "p")

    def tearDown(self):
        self._tmp.cleanup()

    def run_desk(self, *args):
        r = subprocess.run(self.desk + list(args), env=self.env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def new_task(self, slug):
        return json.loads(self.run_desk("new", "--title", slug, "--no-branch", "--no-focus", "--slug", slug).stdout)["task"]

    def popen(self, *args):
        return subprocess.Popen(self.desk + list(args), env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def wait_ok(self, procs):
        for p in procs:
            with p:
                self.assertEqual(p.wait(), 0, p.stderr.read().decode("utf-8", "replace"))


class ParallelCommands(DeskRepo):
    def test_different_commands_on_one_task_keep_every_change(self):
        a = self.new_task("a")
        others = [self.new_task(f"b{i}") for i in range(4)]
        procs = [self.popen("relate", a, "--add", b) for b in others]
        procs += [self.popen("link", a, "--branch", f"br{i}") for i in range(4)]
        procs += [self.popen("new", "--title", f"c{i}", "--no-branch", "--no-focus", "--slug", f"c{i}") for i in range(2)]
        self.wait_ok(procs)
        task = json.loads(self.run_desk("list").stdout)[0]
        self.assertEqual(task["id"], a)
        self.assertEqual(sorted(task["branches"]), [f"br{i}" for i in range(4)])
        state = (self.root / "projects" / "p" / "tasks" / a / "state.md").read_text(encoding="utf-8")
        for b in others:
            self.assertIn(b, state.split("related:")[1].splitlines()[0])
        log = (self.root / "projects" / "p" / "tasks" / a / "log.md").read_text(encoding="utf-8")
        self.assertEqual(log.count("연관 태스크 추가"), 4)
        self.assertEqual(log.count("브랜치 연결"), 4)


class LockScope(DeskRepo):
    def test_read_commands_do_not_wait_and_writes_do(self):
        a = self.new_task("a")
        with root_lock(self.root):
            for args in (["where"], ["list"], ["doctor"]):
                start = time.monotonic()
                self.run_desk(*args)
                self.assertLess(time.monotonic() - start, 5, args)
            writer = self.popen("status", a, "on_hold")
            time.sleep(1.5)
            self.assertIsNone(writer.poll(), "a write command ran while the data root lock was held")
        self.wait_ok([writer])
        self.assertEqual(json.loads(self.run_desk("list").stdout)[0]["status"], "on_hold")


if __name__ == "__main__":
    unittest.main()
