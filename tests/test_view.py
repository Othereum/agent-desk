"""python -m unittest discover -s tests"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from agent_desk.core import DeskError
from agent_desk.view import desk_names, make_handler, resolve_root, start_server
from http.server import ThreadingHTTPServer
from helpers import child_env, git, init_repo, run, write_task


class Desks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "desk"
        self.app = self.root / "myapp"
        write_task(self.app, "261016-login", "로그인", status="in_progress")
        write_task(self.root / "other", "261016-x", "x", status="done")
        (self.root / "notes").mkdir()
        (self.root / "notes" / "a.md").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_desk_folder_shows_its_siblings_first(self):
        self.assertEqual(resolve_root(str(self.app), Path.cwd()), (self.root, "myapp"))
        self.assertEqual(desk_names(self.root, "myapp"), ["myapp", "other"])

    def test_folder_of_desks(self):
        self.assertEqual(resolve_root(str(self.root), Path.cwd()), (self.root, None))

    def test_folder_without_desks_is_an_error(self):
        with self.assertRaises(DeskError):
            resolve_root(str(self.root / "notes"), Path.cwd())

    def test_empty_current_desk_is_still_listed(self):
        (self.root / "new").mkdir()
        self.assertEqual(desk_names(self.root, "new"), ["new", "myapp", "other"])

    def test_registered_desk_of_the_repository(self):
        env = child_env(Path(self.tmp.name))
        repo = init_repo(env, Path(self.tmp.name) / "repo")
        git(env, repo, "config", "agent-desk.dir", self.app.as_posix())
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(resolve_root(None, repo), (self.root, "myapp"))

    def test_unregistered_repository_is_an_error(self):
        env = child_env(Path(self.tmp.name))
        repo = init_repo(env, Path(self.tmp.name) / "repo")
        r = run(env, "view", "--no-open", cwd=repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("agent-desk.dir", r.stderr)


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "desk"
        self.task = write_task(self.root / "myapp", "261016-login", "로그인", body="## 목표\n이메일 로그인\n")
        (self.task / "decisions.md").write_text("# 결정\n", encoding="utf-8")
        (self.task / "files").mkdir()
        (self.task / "files" / "trace.txt").write_text("evidence", encoding="utf-8")
        (self.root / "secret.txt").write_text("no", encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.root, "myapp", "tok"))
        self.server.last_request = 0
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_desks_and_task(self):
        code, body = self.get("/tok/api/desks")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["current"], "myapp")
        self.assertEqual(data["desks"][0]["tasks"][0]["title"], "로그인")
        code, body = self.get("/tok/api/task?desk=myapp&id=261016-login")
        task = json.loads(body)
        self.assertEqual(task["files"]["state.md"], "## 목표\n이메일 로그인\n")
        self.assertIsNone(task["files"]["log.md"])
        self.assertEqual(task["attachments"], ["trace.txt"])
        self.assertEqual(self.get("/tok/file?desk=myapp&id=261016-login&name=trace.txt"), (200, b"evidence"))

    def test_page_and_live_reads(self):
        code, body = self.get("/tok/")
        self.assertEqual(code, 200)
        self.assertNotIn(b"__POLL_MS__", body)
        (self.task / "log.md").write_text("# log\n", encoding="utf-8")
        task = json.loads(self.get("/tok/api/task?desk=myapp&id=261016-login")[1])
        self.assertEqual(task["files"]["log.md"], "# log\n")

    def test_refuses_wrong_token_and_paths_outside_the_desks(self):
        self.assertEqual(self.get("/other/api/desks")[0], 404)
        self.assertEqual(self.get("/api/desks")[0], 404)
        self.assertEqual(self.get("/tok/api/task?desk=..&id=desk")[0], 404)
        self.assertEqual(self.get("/tok/api/task?desk=myapp&id=..")[0], 404)
        self.assertEqual(self.get("/tok/file?desk=myapp&id=261016-login&name=../state.md")[0], 404)

    def test_requests_keep_the_server_alive(self):
        self.get("/tok/api/desks")
        self.assertGreater(self.server.last_request, 0)


class Start(unittest.TestCase):
    def test_detached_server_reports_its_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            write_task(root / "myapp", "261016-login", "로그인")
            url, proc = start_server(root, "myapp")
            try:
                with urllib.request.urlopen(url + "api/desks") as r:
                    self.assertEqual(json.loads(r.read())["desks"][0]["name"], "myapp")
            finally:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
