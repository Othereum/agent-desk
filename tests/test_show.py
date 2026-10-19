"""adesk show: 포커스·상태·로그를 건드리지 않고 태스크 기록을 출력."""
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
from agent_desk.model import get_project, get_task

os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # tests may run in a Claude Code session: leave its task alone


class Show(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.repo, self.root = d / "repo", d / "root"
        self.repo.mkdir()
        subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(self.repo)], check=True)
        self._env = os.environ.get("AGENT_DESK_ROOT")
        os.environ["AGENT_DESK_ROOT"] = str(self.root)
        self.ok("project-init", "p")
        self.parent = self.ok("new", "--title", "흐름", "--slug", "flow")["task"]
        self.tid = self.ok("new", "--title", "일", "--slug", "work", "--parent", self.parent)["task"]

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

    def show(self, tid) -> str:
        code, o, e = self.run_cli("show", tid)
        self.assertEqual(code, 0, e)
        return o

    def task(self, tid):
        return get_task(get_project(self.root, "p"), tid)

    def test_prints_records_without_side_effects(self):
        log_before = (self.task(self.tid).dir / "log.md").read_text(encoding="utf-8")
        text = self.show(self.tid)
        self.assertIn(f"태스크 {self.tid} (todo, 열린 태스크) · 읽기 전용", text)
        self.assertIn("--- state.md ---", text)
        self.assertIn("--- 결정 목록 (본문은 decisions.md) ---", text)
        self.assertIn(f"--- 상위 태스크 {self.parent} state.md", text)
        self.assertNotIn("이 세션의 할 일", text)  # directions belong to the focus, not to a read
        self.assertNotIn("포커스 ", text.splitlines()[0].split("·")[1])
        task = self.task(self.tid)
        self.assertEqual(task.status, "todo")
        self.assertEqual((task.dir / "log.md").read_text(encoding="utf-8"), log_before)

    def test_needs_no_session(self):
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", os.environ)
        self.show(self.tid)

    def test_closed_and_trashed_tasks(self):
        self.ok("close", self.tid, "done")
        self.assertIn("(done, 아카이브)", self.show(self.tid))
        self.ok("delete", self.tid)
        self.assertIn("(done, 휴지통)", self.show(self.tid))

    def test_unknown_task(self):
        code, _, e = self.run_cli("show", "nope")
        self.assertEqual(code, 1)
        self.assertIn("태스크를 찾을 수 없습니다", e)

    def test_focus_on_closed_task_points_to_show(self):
        self.ok("close", self.tid, "done")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "test-show"
        try:
            code, _, e = self.run_cli("focus", self.tid)
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertEqual(code, 1)
        self.assertIn(f"adesk show {self.tid}", e)


if __name__ == "__main__":
    unittest.main()
