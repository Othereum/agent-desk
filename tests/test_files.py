"""adesk files: 태스크 첨부파일(files/) 목록·추가·교체·삭제."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from agent_desk import cli, hooks
from agent_desk.model import get_project, get_task, stray_files

os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # tests may run in a Claude Code session: leave its task alone


class Files(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.repo, self.root, self.src = d / "repo", d / "root", d / "src"
        self.repo.mkdir()
        self.src.mkdir()
        subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(self.repo)], check=True)
        self._env = os.environ.get("AGENT_DESK_ROOT")
        os.environ["AGENT_DESK_ROOT"] = str(self.root)
        self.ok("project-init", "p")
        self.tid = self.ok("new", "--title", "x", "--slug", "x")["task"]
        self.tdir = self.root / "projects" / "p" / "tasks" / self.tid

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

    def source(self, name, data: bytes) -> str:
        path = self.src / name
        path.write_bytes(data)
        return str(path)

    def log(self) -> str:
        return (self.tdir / "log.md").read_text(encoding="utf-8")

    def test_add_keeps_bytes_and_lists(self):
        data = b"\xef\xbb\xbfline\r\n\x00\xff"  # BOM, CRLF and binary stay as given
        r = self.ok("files", self.tid, "add", self.source("repro.py", data), self.source("shot.png", b"png"))
        self.assertEqual((r["added"], r["replaced"]), (["repro.py", "shot.png"], []))
        self.assertEqual((self.tdir / "files" / "repro.py").read_bytes(), data)
        listed = self.ok("files", self.tid)["files"]
        self.assertEqual(listed, [{"name": "repro.py", "size": len(data)}, {"name": "shot.png", "size": 3}])
        self.assertIn("첨부파일 추가: repro.py", self.log())

    def test_existing_name_needs_replace(self):
        self.ok("files", self.tid, "add", self.source("a.txt", b"old"))
        self.assertIn("--replace", self.fail_with("files", self.tid, "add", self.source("a.txt", b"new")))
        self.assertEqual((self.tdir / "files" / "a.txt").read_bytes(), b"old")
        r = self.ok("files", self.tid, "add", self.source("a.txt", b"new"), "--replace")
        self.assertEqual((r["added"], r["replaced"]), ([], ["a.txt"]))
        self.assertEqual((self.tdir / "files" / "a.txt").read_bytes(), b"new")
        self.assertIn("첨부파일 교체: a.txt", self.log())

    def test_size_limit_needs_large(self):
        big = self.source("big.bin", b"0" * 11)
        with mock.patch.object(cli, "FILE_SIZE_LIMIT", 10):
            self.assertIn("--large", self.fail_with("files", self.tid, "add", self.source("a.txt", b"a"), big))
            self.assertFalse((self.tdir / "files").exists())  # nothing written when one is refused
            self.ok("files", self.tid, "add", big, "--large")

    def test_rm_deletes_and_logs(self):
        self.ok("files", self.tid, "add", self.source("a.txt", b"a"), self.source("b.txt", b"b"))
        self.assertIn("없는 첨부파일", self.fail_with("files", self.tid, "rm", "a.txt", "nope.txt"))
        self.assertTrue((self.tdir / "files" / "a.txt").exists())
        self.assertEqual(self.ok("files", self.tid, "delete", "files/a.txt")["removed"], ["a.txt"])
        self.ok("files", self.tid, "remove", "b.txt")
        self.assertFalse((self.tdir / "files").exists())
        self.assertIn("첨부파일 삭제: a.txt", self.log())

    def test_put_refuses_attachment(self):
        self.ok("files", self.tid, "add", self.source("a.md", b"a"))
        rel = f"projects/p/tasks/{self.tid}/files/a.md"
        self.assertIn("--replace", self.fail_with("put", rel, "--file", self.source("b.md", b"b")))

    def test_focus_lists_attachments_and_strays(self):
        self.ok("files", self.tid, "add", self.source("a.txt", b"a"))
        (self.tdir / "other").mkdir()
        project = get_project(self.root, "p")
        task = get_task(project, self.tid)
        self.assertEqual(stray_files(task), ["other/"])
        text = hooks.task_context(self.root, project, task, None, chosen=True)
        self.assertIn("첨부파일(files/): a.txt", text)
        self.assertIn("other/", text)

    def test_edit_of_attachment_points_to_files(self):
        path = self.tdir / "files" / "a.txt"
        reason = hooks.pre_tool_use_decision({"tool_name": "Write", "tool_input": {"file_path": str(path)}})
        self.assertIn("adesk files", reason)
        reason = hooks.pre_tool_use_decision({"tool_name": "Edit", "tool_input": {"file_path": str(self.tdir / "state.md")}})
        self.assertIn("adesk put", reason)


if __name__ == "__main__":
    unittest.main()
