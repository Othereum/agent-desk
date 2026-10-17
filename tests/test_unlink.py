"""adesk unlink: 브랜치 연결 해제."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from agent_desk import cli
from agent_desk.model import check_branch_free, find_task_by_branch, get_project

os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # tests may run in a Claude Code session: leave its task alone


class Unlink(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.repo, self.root = d / "repo", d / "root"
        self.repo.mkdir()
        subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(self.repo)], check=True)
        self._env = os.environ.get("AGENT_DESK_ROOT")
        os.environ["AGENT_DESK_ROOT"] = str(self.root)
        self.ok("project-init", "p")
        self.pdir = self.root / "projects" / "p"

    def tearDown(self):
        if self._env is None:
            os.environ.pop("AGENT_DESK_ROOT", None)
        else:
            os.environ["AGENT_DESK_ROOT"] = self._env
        self._tmp.cleanup()

    def run_cli(self, *argv):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            code = cli.run(["--cwd", str(self.repo), *argv])
        return code, o.getvalue(), e.getvalue()

    def ok(self, *argv):
        code, o, e = self.run_cli(*argv)
        self.assertEqual(code, 0, e)
        return json.loads(o)

    def fail_with(self, *argv) -> str:
        code, _, e = self.run_cli(*argv)
        self.assertEqual(code, 1)
        return e

    def new(self, slug, *extra):
        return self.ok("new", "--title", slug, "--no-branch", "--slug", slug, "--no-focus", *extra)["task"]

    @property
    def project(self):
        return get_project(self.root, "p")

    def ids(self, *flags):
        return [r["id"] for r in self.ok("list", *flags)]

    def test_unlink_frees_branch_and_logs(self):
        tid = self.ok("new", "--title", "x", "--branch", "feat")["task"]
        self.ok("link", tid, "--branch", "other")
        r = self.ok("unlink", tid, "--branch", "FEAT")
        self.assertEqual((r["branches"], r["changed"]), (["other"], True))
        self.assertIsNone(find_task_by_branch(self.project, "feat"))
        check_branch_free(self.project, "feat")
        log = (self.pdir / "tasks" / tid / "log.md").read_text(encoding="utf-8")
        self.assertIn("브랜치 연결 해제: FEAT", log)

    def test_unlink_not_linked_is_noop(self):
        tid = self.new("a")
        r = self.ok("unlink", tid, "--branch", "nope")
        self.assertFalse(r["changed"])
        self.assertNotIn("해제", (self.pdir / "tasks" / tid / "log.md").read_text(encoding="utf-8"))

    def test_unlink_last_branch_warns(self):
        tid = self.ok("new", "--title", "x", "--branch", "feat")["task"]
        r = self.ok("unlink", tid, "--branch", "feat")
        self.assertEqual(r["branches"], [])
        self.assertIn("warning", r)

    def test_unlink_defaults_to_current_branch(self):
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "-b", "cur"], check=True)
        tid = self.ok("new", "--title", "x", "--branch", "cur")["task"]
        self.assertEqual(self.ok("unlink", tid)["branches"], [])

    def test_unlink_archived_refused(self):
        tid = self.ok("new", "--title", "x", "--branch", "feat")["task"]
        self.ok("close", tid, "done")
        self.assertEqual(self.fail_with("unlink", tid, "--branch", "feat").count("아카이브"), 1)


if __name__ == "__main__":
    unittest.main()
