"""python -m unittest discover -s tests"""

import json
import os
import subprocess
import sys
import datetime as dt
import tempfile
import unittest
from pathlib import Path

DESK = Path(__file__).resolve().parents[1] / "bin" / "desk.py"

STATE = ("---\nid: t1\ntitle: 제목\nstatus: in_progress\nproject: p\nbranches: []\nlinks: []\ndesign_doc:\n"
         "updated: 2026-01-01\nverified: 2026-01-01\n---\n## 목표\n\n본문\n")
TODAY = dt.date.today().isoformat()


def touched(text: str) -> str:
    """STATE-based text as the CLI writes it: `updated` set to today."""
    return text.replace("updated: 2026-01-01", f"updated: {TODAY}")
ENTRY = "---\nid: K0001\ntitle: 항목\nkind: pitfall\npaths: [src/**]\nstatus: valid\n---\n본문\n"


class DeskFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "root"
        self.scratch = base / "scratch"
        self.scratch.mkdir()
        pdir = self.root / "projects" / "p"
        (pdir / "tasks" / "t1").mkdir(parents=True)
        (pdir / "knowledge").mkdir()
        (self.root / "desk.toml").write_text("schema_version = 2\n", encoding="utf-8")
        (pdir / "project.toml").write_text('key = "p"\nremotes = []\npaths = []\n', encoding="utf-8")
        self.state = pdir / "tasks" / "t1" / "state.md"
        self.state.write_text(STATE, encoding="utf-8", newline="\n")
        self.log = pdir / "tasks" / "t1" / "log.md"
        self.log.write_text("# 태스크 로그\n\n## 2026-10-18 00:00\n- 태스크 생성\n", encoding="utf-8", newline="\n")
        self.entry = pdir / "knowledge" / "K0001-x.md"
        self.entry.write_text(ENTRY, encoding="utf-8", newline="\n")

    def tearDown(self):
        self._tmp.cleanup()

    def run_desk(self, *args, stdin: str = ""):
        env = {**os.environ, "AGENT_DESK_ROOT": str(self.root)}
        return subprocess.run([sys.executable, str(DESK), "--cwd", str(self.scratch), *args], input=stdin,
                              capture_output=True, text=True, encoding="utf-8", env=env)

    def scratch_file(self, text: str, name: str = "in.md") -> str:
        path = self.scratch / name
        path.write_bytes(text.encode("utf-8"))
        return str(path)


class Put(DeskFixture):
    def test_replaces_body_and_rebuilds_index(self):
        r = self.run_desk("put", str(self.state), "--file", self.scratch_file(STATE.replace("본문", "새 본문")))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state.read_text(encoding="utf-8"), touched(STATE.replace("본문", "새 본문")))
        self.assertNotIn("kept", json.loads(r.stdout))
        self.assertIn(TODAY, (self.root / "projects" / "p" / "index.md").read_text(encoding="utf-8"))

    def test_keeps_whole_frontmatter_and_reports_kept(self):
        copy = (STATE.replace("title: 제목", "title: 바뀐 제목").replace("status: in_progress", "status: done")
                .replace("branches: []", "branches: []\nextra: x").replace("본문", "새 본문"))
        r = self.run_desk("put", str(self.state), "--file", self.scratch_file(copy))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["kept"], {"title": "제목", "status": "in_progress", "extra": None})
        self.assertEqual(self.state.read_text(encoding="utf-8"), touched(STATE.replace("본문", "새 본문")))

    def test_body_only_copy(self):
        for target, body in ((self.state, "## 목표\n\n본문만\n"), (self.entry, "새 본문\n")):
            before = target.read_text(encoding="utf-8")
            r = self.run_desk("put", str(target), "--file", self.scratch_file(body))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("kept", json.loads(r.stdout))
            fm = before[:before.index("---\n", 4) + 4]
            self.assertEqual(target.read_text(encoding="utf-8"), touched(fm) + body)

    def test_unreadable_copy_frontmatter_is_ignored(self):
        bad = "---\ntitle: x\n  nested: y\n---\n새 본문\n"
        r = self.run_desk("put", str(self.state), "--file", self.scratch_file(bad))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIn("무시", out["warnings"][0])
        self.assertEqual(out["kept"]["title"], "제목")
        self.assertEqual(self.state.read_text(encoding="utf-8"), touched(STATE).split("## 목표")[0] + "새 본문\n")

    def test_unclosed_copy_frontmatter_is_refused(self):
        r = self.run_desk("put", str(self.state), "--file", self.scratch_file("---\n본문\n"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("본문만", r.stderr)
        self.assertEqual(self.state.read_text(encoding="utf-8"), STATE)

    def test_repairs_unreadable_file_with_copy_frontmatter(self):
        self.state.write_text("---\nid: t1\n  bad: y\n---\n본문\n", encoding="utf-8", newline="\n")
        r = self.run_desk("put", str(self.state), "--file", self.scratch_file("본문만\n"))
        self.assertEqual(r.returncode, 1)  # nothing to keep and none given
        self.assertIn("frontmatter", r.stderr)
        r = self.run_desk("put", str(self.state), "--file", self.scratch_file(STATE))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state.read_text(encoding="utf-8"), touched(STATE))

    def test_relative_to_root_and_stdin(self):
        r = self.run_desk("put", "projects/p/tasks/t1/state.md", "--file", "-", stdin=STATE + "추가\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.state.read_text(encoding="utf-8").endswith("추가\n"))

    def test_normalizes_case_separators_bom_crlf(self):
        if os.name != "nt":
            self.skipTest("case-insensitive paths")
        target = str(self.state).upper().replace("\\", "/")
        r = self.run_desk("put", target, "--file", self.scratch_file("﻿" + STATE.replace("\n", "\r\n")))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state.read_bytes(), touched(STATE).encode("utf-8"))

    def test_invalid_toml_is_not_written(self):
        target = self.root / "projects" / "p" / "project.toml"
        r = self.run_desk("put", str(target), "--file", self.scratch_file('key = "p\n'))
        self.assertEqual(r.returncode, 1)
        self.assertIn('key = "p"', target.read_text(encoding="utf-8"))

    def test_refuses_outside_root(self):
        outside = Path(self._tmp.name) / "outside.md"
        outside.write_text("x\n", encoding="utf-8")
        for target in (str(outside), "../outside.md"):
            r = self.run_desk("put", target, "--file", self.scratch_file("y\n"))
            self.assertEqual(r.returncode, 1, target)
            self.assertIn("데이터 폴더", r.stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "x\n")

    def test_refuses_missing_index_and_empty(self):
        r = self.run_desk("put", "projects/p/tasks/t1/new.md", "--file", self.scratch_file("x\n"))
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.state.parent / "new.md").exists())
        self.run_desk("index")
        for name in ("index.md",) + (("INDEX.MD",) if os.name == "nt" else ()):
            r = self.run_desk("put", f"projects/p/{name}", "--file", self.scratch_file("x\n"))
            self.assertEqual(r.returncode, 1, name)
            self.assertIn("adesk index", r.stderr)
        r = self.run_desk("put", str(self.state), "--file", "-", stdin="")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.state.read_text(encoding="utf-8"), STATE)


    def test_keeps_keys_cli_changed_after_the_copy(self):
        copy = self.state.read_text(encoding="utf-8").replace("본문", "새 본문")
        r = self.run_desk("--project", "p", "status", "t1", "on_hold")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_desk("put", str(self.state), "--file", self.scratch_file(copy))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["kept"], {"status": "on_hold"})
        text = self.state.read_text(encoding="utf-8")
        self.assertIn("status: on_hold", text)
        self.assertIn("새 본문", text)

    def test_keeps_knowledge_keys_and_drops_ones_the_file_lacks(self):
        copy = ENTRY.replace("status: valid", "status: stale\nstale_reason: 옛 사유").replace("본문", "새 본문")
        r = self.run_desk("--project", "p", "kn", "verify", "K0001")
        self.assertEqual(r.returncode, 0, r.stderr)
        verified = json.loads(r.stdout)["verified"]
        r = self.run_desk("put", str(self.entry), "--file", self.scratch_file(copy))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["kept"], {"status": "valid", "stale_reason": None, "verified": verified})
        text = self.entry.read_text(encoding="utf-8")
        self.assertIn("status: valid", text)
        self.assertIn(f"verified: {verified}", text)
        self.assertNotIn("옛 사유", text)
        self.assertIn("새 본문", text)

    def test_unchanged_keys_report_nothing(self):
        r = self.run_desk("put", str(self.entry), "--file", self.scratch_file(ENTRY.replace("본문", "새 본문")))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("kept", json.loads(r.stdout))
        self.assertNotIn("updated", self.entry.read_text(encoding="utf-8"))  # entries have no `updated` key

    def test_entry_updated_kept_current_when_present(self):
        with_updated = ENTRY.replace("status: valid", "status: valid\nupdated: 2026-01-01")
        self.entry.write_text(with_updated, encoding="utf-8", newline="\n")
        r = self.run_desk("put", str(self.entry), "--file", self.scratch_file("새 본문\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"updated: {TODAY}", self.entry.read_text(encoding="utf-8"))
        self.entry.write_text(with_updated, encoding="utf-8", newline="\n")
        r = self.run_desk("--project", "p", "kn", "edit", "K0001", "--title", "새 제목")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"updated: {TODAY}", self.entry.read_text(encoding="utf-8"))


class PutVerified(DeskFixture):
    def test_state(self):
        r = self.run_desk("put", str(self.state), "--verified", "--file", self.scratch_file("## 목표\n\n확인함\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        verified = json.loads(r.stdout)["verified"]
        self.assertTrue(verified.startswith(TODAY))
        text = self.state.read_text(encoding="utf-8")
        self.assertIn(f"verified: {verified}", text)
        self.assertIn(f"updated: {TODAY}", text)

    def test_knowledge_like_kn_verify(self):
        stale = ENTRY.replace("status: valid", "status: stale\nstale_reason: 코드 바뀜\nverified: 2026-01-01 00:00")
        self.entry.write_text(stale, encoding="utf-8", newline="\n")
        r = self.run_desk("put", str(self.entry), "--verified", "--file", self.scratch_file("고친 본문\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        verified = json.loads(r.stdout)["verified"]
        text = self.entry.read_text(encoding="utf-8")
        self.assertIn("status: valid", text)
        self.assertIn(f"verified: {verified}", text)
        self.assertNotIn("코드 바뀜", text)
        self.assertIn("고친 본문", text)

    def test_refused_for_other_files_and_inactive_entries(self):
        r = self.run_desk("put", str(self.log), "--verified", "--file", self.scratch_file("x\n"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("--verified", r.stderr)
        self.entry.write_text(ENTRY.replace("status: valid", "status: retired"), encoding="utf-8", newline="\n")
        r = self.run_desk("put", str(self.entry), "--verified", "--file", self.scratch_file("x\n"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("retired", r.stderr)


class TaskUpdated(DeskFixture):
    def test_writing_any_task_file_touches_state(self):
        decisions = self.state.parent / "decisions.md"
        decisions.write_text("# 결정 기록\n", encoding="utf-8", newline="\n")
        for cmd, target, text in (("append", self.log, "## 2026-10-18 01:00\n- a\n"),
                                  ("put", decisions, "# 결정 기록\n\n## D1. 제목  (2026-10-18, 유효)\n- 결정: x\n"),
                                  ("append", self.state, "추가\n")):
            self.state.write_text(STATE, encoding="utf-8", newline="\n")
            r = self.run_desk(cmd, str(target), "--file", self.scratch_file(text))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"updated: {TODAY}", self.state.read_text(encoding="utf-8"), f"{cmd} {target.name}")
        self.assertIn(TODAY, (self.root / "projects" / "p" / "index.md").read_text(encoding="utf-8"))

    def test_unreadable_state_is_a_warning(self):
        self.state.write_text("---\nid: t1\n  bad: y\n---\n", encoding="utf-8", newline="\n")
        r = self.run_desk("append", str(self.log), "--file", self.scratch_file("- a\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("updated", json.loads(r.stdout)["warnings"][0])


class Append(DeskFixture):
    def test_heading_starts_after_blank_line(self):
        r = self.run_desk("append", str(self.log), "--file", self.scratch_file("## 2026-10-18 01:00\n- 알아낸 것"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log.read_text(encoding="utf-8"),
                         "# 태스크 로그\n\n## 2026-10-18 00:00\n- 태스크 생성\n\n## 2026-10-18 01:00\n- 알아낸 것\n")

    def test_line_after_unterminated_file(self):
        self.log.write_text("# 로그\n- a", encoding="utf-8", newline="\n")
        r = self.run_desk("append", "projects/p/tasks/t1/log.md", "-f", self.scratch_file("- b\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log.read_text(encoding="utf-8"), "# 로그\n- a\n- b\n")

    def test_validates_frontmatter_files(self):
        self.state.write_text("---\nid: t1\n", encoding="utf-8")  # unclosed: appending cannot fix it
        r = self.run_desk("append", str(self.state), "--file", self.scratch_file("x\n"))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.state.read_text(encoding="utf-8"), "---\nid: t1\n")


class PreToolUseHook(DeskFixture):
    def hook(self, tool: str, file_path: str, cwd: str | None = None) -> dict | None:
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        payload = {"tool_name": tool, "tool_input": {key: file_path}, "cwd": cwd or str(self.scratch),
                   "session_id": "test"}
        r = self.run_desk("hook", "pre-tool-use", stdin=json.dumps(payload))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_denies_file_tools_in_data_root(self):
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            out = self.hook(tool, str(self.state))
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", tool)
            self.assertIn("adesk put", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_denies_normalized_paths(self):
        paths = [str(self.root / "projects" / "p" / "new.md"), "../root/projects/p/tasks/t1/state.md"]
        if os.name == "nt":
            s = str(self.state)
            paths += [s.upper(), s.replace("\\", "/"), "/" + s[0].lower() + s[2:].replace("\\", "/")]
        for p in paths:
            out = self.hook("Edit", p)
            self.assertIsNotNone(out, p)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", p)

    def test_allows_other_paths_and_tools(self):
        self.assertIsNone(self.hook("Write", str(self.scratch / "state.md")))
        self.assertIsNone(self.hook("Edit", str(Path(self._tmp.name) / "root-other" / "x.md")))
        self.assertIsNone(self.hook("Read", str(self.state)))

    def test_failure_says_the_check_did_not_run(self):
        r = self.run_desk("hook", "pre-tool-use", stdin="{not json")
        self.assertEqual(r.returncode, 0, r.stderr)
        message = json.loads(r.stdout.splitlines()[0])["systemMessage"]
        self.assertIn("pre-tool-use", message)
        self.assertIn("편집 차단이 동작하지 않았습니다", message)
        self.assertNotIn("주입", message)
        r = self.run_desk("hook", "session-start", stdin="{not json")
        self.assertIn("주입이 빠졌습니다", json.loads(r.stdout.splitlines()[0])["systemMessage"])


class SetTask(DeskFixture):
    def test_title_design_doc_links(self):
        r = self.run_desk("--project", "p", "set", "t1", "--title", "새: 제목 #1", "--design-doc", "https://x/doc",
                          "--add-link", "https://x/1", "--link", "https://x/2")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["title"], out["links"], out["changed"]), ("새: 제목 #1", ["https://x/1", "https://x/2"], True))
        r = self.run_desk("--project", "p", "edit", "t1", "--remove-link", "https://x/1", "--design-doc", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.state.read_text(encoding="utf-8")
        self.assertIn('title: "새: 제목 #1"', text)
        self.assertIn("links: [https://x/2]\n", text)
        self.assertIn("design_doc:\n", text)
        self.assertIn(f"updated: {TODAY}", text)
        log = self.log.read_text(encoding="utf-8")
        for line in ("제목 변경: 제목 → 새: 제목 #1", "링크 추가: https://x/1", "링크 제거: https://x/1", "설계 문서 해제"):
            self.assertIn(line, log)
        self.assertIn("새: 제목 #1", (self.root / "projects" / "p" / "index.md").read_text(encoding="utf-8"))

    def test_no_change(self):
        r = self.run_desk("--project", "p", "set", "t1", "--title", "제목", "--remove-link", "https://none")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(json.loads(r.stdout)["changed"])
        self.assertEqual(self.state.read_text(encoding="utf-8"), STATE)

    def test_points_to_the_command_for_other_keys(self):
        for opt, cmd in (("--status", "adesk status"), ("--branch", "adesk link"), ("--parent", "adesk relate")):
            r = self.run_desk("--project", "p", "set", "t1", opt, "x")
            self.assertEqual(r.returncode, 2, opt)
            self.assertIn(cmd, r.stderr)
        self.assertEqual(self.state.read_text(encoding="utf-8"), STATE)


class KnEdit(DeskFixture):
    def test_edits_keys(self):
        r = self.run_desk("--project", "p", "kn", "edit", "K0001", "--title", "새 제목", "--paths", "a/**, b/*.py",
                          "--tags", "x,y", "--kind", "finding", "--location", "repo:docs/a.md")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["changed"], ["kind", "location", "paths", "tags", "title"])
        text = self.entry.read_text(encoding="utf-8")
        for line in ("title: 새 제목", "paths: [a/**, b/*.py]", "tags: [x, y]", "kind: finding", "location: repo:docs/a.md"):
            self.assertIn(line + "\n", text)
        self.assertIn("본문", text)
        self.assertIn("새 제목", (self.entry.parent / "index.md").read_text(encoding="utf-8"))

    def test_refusals(self):
        r = self.run_desk("--project", "p", "kn", "edit", "K0001", "--status", "valid")
        self.assertEqual(r.returncode, 2)
        self.assertIn("kn verify", r.stderr)
        r = self.run_desk("--project", "p", "kn", "edit", "K0001", "--kind", "nope")
        self.assertEqual(r.returncode, 2)
        self.entry.write_text("---\nid: K0001\nstatus: deleted\n---\n", encoding="utf-8", newline="\n")
        r = self.run_desk("--project", "p", "kn", "edit", "K0001", "--title", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("삭제", r.stderr)


class DecisionHeadings(DeskFixture):
    def setUp(self):
        super().setUp()
        self.decisions = self.state.parent / "decisions.md"
        self.decisions.write_text("# 결정 기록\n\n<!--\n## D1. 결정 제목  (YYYY-MM-DD, 유효)\n-->\n",
                                  encoding="utf-8", newline="\n")

    def append(self, text: str) -> list[str]:
        r = self.run_desk("append", str(self.decisions), "--file", self.scratch_file(text))
        self.assertEqual(r.returncode, 0, r.stderr)  # warnings never block the write
        self.assertTrue(self.decisions.read_text(encoding="utf-8").endswith(text))
        return json.loads(r.stdout).get("warnings", [])

    def test_well_formed(self):
        self.assertEqual(self.append("## D1. 첫 결정  (2026-10-18, 유효)\n- 결정: a\n"), [])
        self.assertEqual(self.append("## D2. 둘째 결정: x → y  (2026-10-18, 일부 대체 → D3: 범위)\n"), [])
        self.assertEqual(self.append("## D3. 셋째  (2026-10-18, 유효) → K0012\n"), [])

    def test_gap(self):
        self.append("## D1. a  (2026-10-18, 유효)\n")
        warnings = self.append("## D3. c  (2026-10-18, 유효)\n")
        self.assertEqual(len(warnings), 1)
        self.assertIn("빠졌습니다: D2", warnings[0])

    def test_duplicate(self):
        self.append("## D1. a  (2026-10-18, 유효)\n")
        warnings = self.append("## D1. b  (2026-10-18, 유효)\n")
        self.assertEqual(len(warnings), 1)
        self.assertIn("겹칩니다: D1", warnings[0])

    def test_bad_format(self):
        for head in ("## D1 제목  (2026-10-18, 유효)", "## D1. 제목 (2026-10-18, 유효)", "### D1. 제목  (2026-10-18, 유효)",
                     "## D1. 제목  (어제, 유효)", "## D1. 제목"):
            self.decisions.write_text("# 결정 기록\n", encoding="utf-8", newline="\n")
            warnings = self.append(head + "\n")
            self.assertEqual(len(warnings), 1, head)
            self.assertIn("형식", warnings[0])

    def test_put_checks_too(self):
        r = self.run_desk("put", str(self.decisions), "--file", self.scratch_file("# 결정\n\n## D2. b  (2026-10-18, 유효)\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("D1", json.loads(r.stdout)["warnings"][0])


if __name__ == "__main__":
    unittest.main()
