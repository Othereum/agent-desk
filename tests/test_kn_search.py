"""python -m unittest discover -s tests"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

from agent_desk.frontmatter import update_frontmatter
from agent_desk.knowledge import Entry, search_score, word_stem

os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # tests may run in a Claude Code session: leave its task alone


def entry(title="", body="", tags=(), paths=()):
    return Entry("K0001", Path("K0001.md"), {"title": title, "tags": list(tags), "paths": list(paths)},
                 body, "p")


class SearchScore(unittest.TestCase):
    def test_phrase_in_one_argument_matches_words(self):
        e = entry(title="Claude Code 플러그인 업데이트는 계산된 버전 문자열로 판단한다")
        self.assertGreater(search_score(e, ["플러그인 업데이트 버전"]), 0)

    def test_exact_phrase_ranks_first(self):
        exact = entry(body="오류: 세션 캐시 파일을 찾을 수 없습니다")
        words = entry(title="세션 캐시", body="파일을 찾을 때 없습니다")
        q = ["세션 캐시 파일을 찾을 수 없습니다"]
        self.assertGreater(search_score(exact, q), search_score(words, q))

    def test_scattered_minority_of_words_does_not_match(self):
        e = entry(body="knowledge 항목")
        self.assertEqual(search_score(e, ["오류: knowledge 항목이 없습니다 다시 확인"]), 0)

    def test_single_term_substring_kept(self):
        e = entry(body="hook의 session_id는 메인과 같다")
        self.assertEqual(search_score(e, ["session_id"]), 1)
        self.assertEqual(search_score(e, ["없는말"]), 0)

    def test_particles(self):
        self.assertEqual(word_stem("플러그인을"), "플러그인")
        self.assertEqual(word_stem("worktree에서"), "worktree")
        self.assertEqual(word_stem("경로"), "경로")  # a one-character stem is not stripped
        self.assertEqual(word_stem("session_id"), "session_id")
        e = entry(title="플러그인은 버전으로 판단한다")
        self.assertGreater(search_score(e, ["플러그인을 업데이트"]), 0)
        self.assertGreater(search_score(e, ["플러그인을"]), 0)


class FrontmatterRemove(unittest.TestCase):
    def test_none_removes_key(self):
        text = "---\nid: K1\nstale_reason: x\nstatus: stale\n---\nbody\n"
        self.assertEqual(update_frontmatter(text, {"stale_reason": None, "status": "valid", "gone": None}),
                         "---\nid: K1\nstatus: valid\n---\nbody\n")


class KnCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.repo, self.root = d / "repo", d / "root"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.env = {**os.environ, "AGENT_DESK_ROOT": str(self.root)}
        self.run_desk("project-init", "p")
        self.file = Path(json.loads(self.run_desk("kn", "add", "--kind", "finding", "--title", "제목",
                                                  "--slug", "s", "--body", "본문 첫 줄\n둘째 줄"))["file"])

    def tearDown(self):
        self.tmp.cleanup()

    def run_desk(self, *args, ok=True):
        p = subprocess.run([sys.executable, str(BIN / "desk.py"), "--cwd", str(self.repo), *args],
                           env=self.env, capture_output=True, text=True, encoding="utf-8")
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout
        return p

    def test_show(self):
        shown = json.loads(self.run_desk("kn", "show", "k0001"))
        self.assertEqual(shown["id"], "K0001")
        self.assertIn("둘째 줄", shown["body"])
        self.run_desk("kn", "retire", "K0001", "--reason", "틀림")
        shown = json.loads(self.run_desk("kn", "show", "K0001"))
        self.assertEqual((shown["status"], shown["retired_reason"]), ("retired", "틀림"))
        self.assertEqual(self.run_desk("kn", "show", "K0009", ok=False).returncode, 1)

    def test_path_twice_is_an_error(self):
        p = self.run_desk("kn", "search", "--path", "a.py", "--path", "b.py", ok=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("--path", p.stderr)

    def test_verify_writes_no_empty_stale_reason(self):
        self.run_desk("kn", "verify", "K0001")
        self.assertNotIn("stale_reason", self.file.read_text(encoding="utf-8"))
        self.run_desk("kn", "stale", "K0001", "--reason", "코드 변경")
        self.assertIn("stale_reason: 코드 변경", self.file.read_text(encoding="utf-8"))
        self.run_desk("kn", "verify", "K0001")
        self.assertNotIn("stale_reason", self.file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
