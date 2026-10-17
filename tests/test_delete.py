"""python -m unittest discover -s tests"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from agent_desk import cli
from agent_desk.hooks import parent_context
from agent_desk.model import check_branch_free, find_task_by_branch, get_focus_task, get_project


class Delete(unittest.TestCase):
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

    def test_delete_moves_to_trash_and_hides(self):
        tid = self.new("a")
        out = self.ok("delete", tid)
        self.assertTrue(out["deleted"])
        self.assertFalse((self.pdir / "tasks" / tid).exists())
        self.assertTrue((self.pdir / "trash" / tid / "state.md").exists())
        self.assertIn("- 휴지통으로 옮김", (self.pdir / "trash" / tid / "log.md").read_text(encoding="utf-8"))
        self.assertNotIn(tid, self.ids())
        self.assertNotIn(tid, self.ids("--all"))
        deleted = self.ok("list", "--deleted")
        self.assertEqual([r["id"] for r in deleted], [tid])
        self.assertTrue(deleted[0]["deleted"])
        self.assertNotIn(tid, (self.pdir / "index.md").read_text(encoding="utf-8"))

    def test_restore_open_and_closed(self):
        a, b = self.new("a"), self.new("b")
        self.ok("close", b, "done")
        self.ok("delete", a)
        self.ok("delete", b)  # archived tasks can be trashed too
        self.assertEqual(sorted(self.ids("--deleted")), sorted([a, b]))
        self.assertFalse(self.ok("restore", a)["archived"])
        self.assertTrue((self.pdir / "tasks" / a / "state.md").exists())
        self.assertTrue(self.ok("restore", b)["archived"])
        self.assertTrue((self.pdir / "archive" / b / "state.md").exists())
        self.assertEqual(self.ids("--deleted"), [])
        self.assertIn("휴지통에서 되살림", (self.pdir / "tasks" / a / "log.md").read_text(encoding="utf-8"))
        self.assertNotIn("\ntrashed:", (self.pdir / "tasks" / a / "state.md").read_text(encoding="utf-8"))
        self.assertIn("휴지통에 없는", self.fail_with("restore", a))

    def test_purge_needs_trash_first(self):
        tid = self.new("a")
        err = self.fail_with("delete", tid, "--purge")
        self.assertIn(f"adesk delete {tid}", err)
        self.assertTrue((self.pdir / "tasks" / tid).exists())
        self.ok("delete", tid)
        self.assertIn("--purge", self.fail_with("delete", tid))
        out = self.ok("delete", tid, "--purge")
        self.assertEqual(out["status"], "purged")
        self.assertIn("버전 기록", out["note"])
        self.assertFalse((self.pdir / "trash" / tid).exists())
        self.assertIn("찾을 수 없습니다", self.fail_with("delete", tid, "--purge"))
        self.assertIn("찾을 수 없습니다", self.fail_with("restore", tid))

    def test_branch_is_freed(self):
        tid = self.ok("new", "--title", "x", "--branch", "feat")["task"]
        self.ok("delete", tid)
        self.assertIsNone(find_task_by_branch(self.project, "feat"))
        check_branch_free(self.project, "feat")
        other = self.new("other")
        self.ok("link", other, "--branch", "feat")
        # restoring would put two open tasks on one branch
        self.assertIn("feat", self.fail_with("restore", tid))
        self.assertTrue((self.pdir / "trash" / tid).exists())

    def test_delete_clears_focus(self):
        tid = self.ok("new", "--title", "f", "--no-branch", "--slug", "f")["task"]  # on main: focused
        self.assertEqual(get_focus_task(self.project).id, tid)
        self.assertTrue(self.ok("delete", tid)["unfocused"])
        self.assertIsNone(self.ok("where")["focus"])
        self.ok("restore", tid)  # like close and reopen: the focus does not come back
        self.assertIsNone(get_focus_task(self.project))

    def test_id_collisions(self):
        tid = self.new("a")
        self.ok("delete", tid)
        # recreating a deleted task skips the trashed id
        self.assertEqual(self.new("a"), f"{tid}~2")
        shutil.rmtree(self.pdir / "tasks" / f"{tid}~2")
        # a task with the same id appeared while it was in the trash
        shutil.copytree(self.pdir / "trash" / tid, self.pdir / "tasks" / tid)
        self.assertIn("이미 있습니다", self.fail_with("restore", tid))

    def test_other_commands_point_to_restore(self):
        tid, other = self.new("a"), self.new("b")
        self.ok("delete", tid)
        for argv in (["close", tid, "done"], ["reopen", tid], ["status", tid, "on_hold"],
                     ["link", tid, "--branch", "x"], ["relate", tid, "--add", other],
                     ["relate", other, "--add", tid], ["focus", tid]):
            err = self.fail_with(*argv)
            self.assertIn("휴지통", err, argv)
            self.assertIn(f"restore {tid}", err, argv)

    def test_references_are_left_dangling(self):
        parent = self.new("parent")
        child = self.new("child", "--parent", parent)
        self.ok("delete", parent)
        child_task = [t for t in self.ok("list") if t["id"] == child][0]
        self.assertEqual(child_task["parent"], parent)
        self.assertIn(f"상위 태스크 {parent} 를 찾을 수 없음.", parent_context(self.project, self._task(child)))

    def _task(self, tid):
        from agent_desk.model import get_task
        return get_task(self.project, tid)

    def test_doctor_old_trash_and_no_stray_warning(self):
        tid = self.new("a")
        self.ok("delete", tid)
        tdir = self.pdir / "trash" / tid
        (tdir / "state (conflict copy).md").write_text("x", encoding="utf-8")
        kinds = [i["kind"] for i in self.ok("doctor")["issues"]]
        self.assertNotIn("stray-file", kinds)
        self.assertNotIn("trash-old", kinds)
        state = tdir / "state.md"
        text = state.read_text(encoding="utf-8")
        self.assertIn("\ntrashed: ", text)  # the trash time is metadata, not parsed from the log
        state.write_text(re.sub(r"(?m)^trashed: .*$", "trashed: 2020-01-01 00:00", text), encoding="utf-8")
        issues = [i for i in self.ok("doctor")["issues"] if i["kind"] == "trash-old"]
        self.assertEqual([i["id"] for i in issues], [tid])


if __name__ == "__main__":
    unittest.main()
