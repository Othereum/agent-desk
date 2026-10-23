"""python -m unittest discover -s tests"""

import tempfile
import unittest
from pathlib import Path

from helpers import child_env, git, init_repo, run, session_start, write_task


class SessionStart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.env = child_env(self.base)
        self.repo = init_repo(self.env, self.base / "myapp", branch="feature/login")
        self.desk = self.base / "desks" / "myapp"
        self.desk.mkdir(parents=True)
        git(self.env, self.repo, "config", "agent-desk.dir", self.desk.as_posix())

    def tearDown(self):
        self.tmp.cleanup()

    def context(self, cwd: Path | None = None) -> str | None:
        r, context = session_start(self.env, cwd or self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")
        return context

    def test_open_tasks_nested_under_parent(self):
        write_task(self.desk, "261016-auth", "인증", status="in_progress")
        write_task(self.desk, "261016-login", "로그인", status="todo", parent="261016-auth",
                   branches="[feature/login]")
        write_task(self.desk, "261016-done", "끝난 일", status="done")
        write_task(self.desk, "261016-orphan", "고아", status="on_hold", parent="261016-done")
        self.assertEqual(self.context().splitlines()[1:-1], [
            "--- 열린 태스크 ---",
            "- 261016-auth (인증) · in_progress",
            "  - 261016-login (로그인) · todo · 이 브랜치",
            "- 261016-orphan (고아) · on_hold · 상위 261016-done",
        ])

    def test_head_names_desk_and_branch(self):
        self.assertEqual(self.context().splitlines()[0],
                         f"[agent-desk] 데스크 {self.desk.as_posix()} · 브랜치 feature/login")
        self.assertIn("열린 태스크 없음.", self.context())

    def test_worktree_shares_registration(self):
        git(self.env, self.repo, "-c", "user.name=t", "-c", "user.email=t@example.com",
            "commit", "-q", "--allow-empty", "-m", "init")
        wt = self.base / "wt"
        git(self.env, self.repo, "worktree", "add", "-q", "-b", "fix/crash", str(wt))
        write_task(self.desk, "261016-crash", "크래시", branches="[fix/crash]")
        self.assertIn("- 261016-crash (크래시) · in_progress · 이 브랜치", self.context(wt))

    def test_unreadable_and_stray_files_warn(self):
        bad = write_task(self.desk, "261016-bad")
        (bad / "state.md").write_text("---\ntitle: [a\n---\n", encoding="utf-8")
        write_task(self.desk, "261016-weird", status="started")
        ok = write_task(self.desk, "261016-ok")
        (ok / "state-PC2.md").write_text("x", encoding="utf-8")
        (self.desk / "261016-empty").mkdir()
        context = self.context()
        self.assertIn("경고: 261016-bad 의 state.md frontmatter를 읽을 수 없다", context)
        self.assertIn("경고: 261016-weird 의 status가", context)
        self.assertIn("경고: 261016-empty 의 state.md가 없다", context)
        self.assertIn("경고: 261016-ok 폴더에 예상하지 않은 파일이 있다(동기화 충돌 사본일 수 있음): state-PC2.md", context)
        self.assertNotIn("- 261016-bad", context)

    def test_missing_desk_folder(self):
        git(self.env, self.repo, "config", "agent-desk.dir", (self.base / "nowhere").as_posix())
        self.assertIn("가리키는 폴더가 없다", self.context())

    def test_legacy_desk_folder_asks_for_migrate(self):
        (self.desk / "tasks").mkdir()
        self.assertIn("`adesk migrate`", self.context())

    def test_unregistered_repo_is_silent(self):
        other = init_repo(self.env, self.base / "other")
        self.assertIsNone(self.context(other))

    def test_unregistered_repo_with_legacy_root_asks_for_migrate(self):
        other = init_repo(self.env, self.base / "other")
        legacy = self.base / "legacy"
        legacy.mkdir()
        (legacy / "desk.toml").write_text("schema_version = 3\n", encoding="utf-8")
        self.assertIn("`adesk migrate`", self.context(other))

    def test_not_a_repository_is_silent(self):
        plain = self.base / "plain"
        plain.mkdir()
        self.assertIsNone(self.context(plain))


class Entry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = child_env(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_event_of_an_older_hooks_json_is_ignored(self):
        r = run(self.env, "hook", "post-tool-use", stdin="{}")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_removed_command_explains(self):
        r = run(self.env, "focus", "261016-login")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Read·Edit·Write로 직접", r.stderr)


if __name__ == "__main__":
    unittest.main()
