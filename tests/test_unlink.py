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
from agent_desk.core import DeskError
from agent_desk.model import check_branch_linkable, get_project, tasks_on_branch

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
        return self.ok("new", "--title", slug, "--slug", slug, *extra)["task"]

    @property
    def project(self):
        return get_project(self.root, "p")

    def ids(self, *flags):
        return [r["id"] for r in self.ok("list", *flags)]

    def test_unlink_frees_branch_and_logs(self):
        tid = self.ok("new", "--title", "x", "--slug", "x", "--branch", "feat")["task"]
        self.ok("link", tid, "--branch", "other")
        r = self.ok("unlink", tid, "--branch", "FEAT")
        self.assertEqual((r["branches"], r["changed"]), (["other"], True))
        self.assertEqual(tasks_on_branch(self.project, "feat"), [])
        log = (self.pdir / "tasks" / tid / "log.md").read_text(encoding="utf-8")
        self.assertIn("브랜치 연결 해제: FEAT", log)

    def test_branch_carries_several_tasks(self):
        a = self.ok("new", "--title", "a", "--slug", "a", "--branch", "feat")["task"]
        b = self.new("b")
        self.ok("link", b, "--branch", "feat")
        self.assertEqual(sorted(t.id for t in tasks_on_branch(self.project, "FEAT")), sorted([a, b]))
        self.ok("status", a, "in_progress")
        self.ok("close", b, "done")
        self.ok("reopen", b)
        self.assertEqual(len(tasks_on_branch(self.project, "feat")), 2)

    def test_shared_branch_refused(self):
        self.assertIn("공용 브랜치", self.fail_with("new", "--title", "x", "--slug", "x", "--branch", "main"))
        tid = self.new("a")
        self.assertIn("공용 브랜치", self.fail_with("link", tid, "--branch", "main"))
        with self.assertRaises(DeskError):
            check_branch_linkable(self.project, "Main")

    def test_new_only_creates(self):
        r = self.ok("new", "--title", "x", "--slug", "x", "--branch", "feat")
        self.assertNotIn("focused", r)
        self.assertEqual([x["status"] for x in self.ok("list")], ["todo"])
        self.assertEqual(r["task"].split("-", 1)[1], "x")  # the ID never comes from the branch
        e = io.StringIO()
        with contextlib.redirect_stderr(e), self.assertRaises(SystemExit):
            cli.run(["--cwd", str(self.repo), "new", "--title", "y", "--branch", "feat"])
        self.assertIn("--slug", e.getvalue())

    def test_unlink_not_linked_is_noop(self):
        tid = self.new("a")
        r = self.ok("unlink", tid, "--branch", "nope")
        self.assertFalse(r["changed"])
        self.assertNotIn("해제", (self.pdir / "tasks" / tid / "log.md").read_text(encoding="utf-8"))

    def test_unlink_last_branch_warns(self):
        tid = self.ok("new", "--title", "x", "--slug", "x", "--branch", "feat")["task"]
        r = self.ok("unlink", tid, "--branch", "feat")
        self.assertEqual(r["branches"], [])
        self.assertIn("warning", r)

    def test_unlink_defaults_to_current_branch(self):
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "-b", "cur"], check=True)
        tid = self.ok("new", "--title", "x", "--slug", "x", "--branch", "cur")["task"]
        self.assertEqual(self.ok("unlink", tid)["branches"], [])

    def test_unlink_archived_refused(self):
        tid = self.ok("new", "--title", "x", "--slug", "x", "--branch", "feat")["task"]
        self.ok("close", tid, "done")
        self.assertEqual(self.fail_with("unlink", tid, "--branch", "feat").count("아카이브"), 1)


if __name__ == "__main__":
    unittest.main()
