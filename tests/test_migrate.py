"""python -m unittest discover -s tests"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import child_env, git, init_repo, run, session_start

STATE = """---
id: {id}
title: {title}
status: {status}
project: myapp
branches: []
parent: {parent}
related: [{related}]
created: 2026-10-16
updated: 2026-10-17
verified: 2026-10-17 10:00
---
## 목표

## 링크

- https://example.com/issue/1
"""


class Migrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.env = child_env(self.base)
        self.root = self.base / "legacy"
        self.project = self.root / "projects" / "myapp"
        self.repo = init_repo(self.env, self.base / "myapp")
        (self.root / "desk.toml").parent.mkdir(parents=True)
        (self.root / "desk.toml").write_text("schema_version = 3\n", encoding="utf-8")
        self.project.mkdir(parents=True)
        (self.project / "project.toml").write_text(
            f'key = "myapp"\nremotes = []\npaths = ["{self.repo.as_posix()}"]\n', encoding="utf-8")
        (self.project / "index.md").write_text("# index\n", encoding="utf-8")
        (self.project / "trash").mkdir()
        self.task("tasks", "261016-auth", "인증", "in_progress")
        self.task("tasks", "261016-login", "로그인", "todo", parent="261016-auth", related="261016-old")
        self.task("archive", "261016-old", "옛 일", "done")
        self.old_mtime = 1_700_000_000
        os.utime(self.project / "archive" / "261016-old" / "state.md", (self.old_mtime, self.old_mtime))

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, sub, task_id, title, status, parent="", related=""):
        d = self.project / sub / task_id
        d.mkdir(parents=True)
        (d / "state.md").write_text(STATE.format(id=task_id, title=title, status=status, parent=parent,
                                                 related=related), encoding="utf-8")
        (d / "decisions.md").write_text("# 결정 기록\n", encoding="utf-8")

    def migrate(self, *args):
        r = run(self.env, "migrate", *args)
        return r.returncode, json.loads(r.stdout)

    def test_flattens_cleans_and_registers(self):
        code, out = self.migrate()
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), ["261016-auth", "261016-login", "261016-old"])
        self.assertFalse((self.root / "desk.toml").exists())
        self.assertEqual(git(self.env, self.repo, "config", "--get", "agent-desk.dir"), self.project.as_posix())
        login = (self.project / "261016-login" / "state.md").read_text(encoding="utf-8")
        self.assertTrue(login.startswith("---\ntitle: 로그인\nstatus: todo\nbranches: []\nparent: 261016-auth\n---\n"), login)
        self.assertTrue(login.endswith("## 링크\n\n- https://example.com/issue/1\n- 관련 태스크: 261016-old\n"), login)
        self.assertEqual((self.project / "261016-old" / "state.md").stat().st_mtime, self.old_mtime)
        _, context = session_start(self.env, self.repo)
        self.assertIn("  - 261016-login (로그인) · todo", context)

    def test_dry_run_changes_nothing(self):
        code, out = self.migrate("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("이동 archive/261016-old → 261016-old", out["changed"])
        self.assertTrue((self.root / "desk.toml").exists())
        self.assertTrue((self.project / "tasks" / "261016-auth").is_dir())

    def test_refuses_nonempty_trash_before_any_change(self):
        (self.project / "trash" / "261016-gone").mkdir()
        code, out = self.migrate()
        self.assertEqual(code, 1)
        self.assertIn("휴지통", out["error"])
        self.assertTrue((self.project / "tasks" / "261016-auth").is_dir())

    def test_refuses_other_schema(self):
        (self.root / "desk.toml").write_text("schema_version = 2\n", encoding="utf-8")
        code, out = self.migrate()
        self.assertEqual(code, 1)
        self.assertIn("0.12.x", out["error"])

    def test_nothing_to_migrate(self):
        (self.root / "desk.toml").unlink()
        code, out = self.migrate()
        self.assertEqual((code, out["changed"]), (0, []))


if __name__ == "__main__":
    unittest.main()
