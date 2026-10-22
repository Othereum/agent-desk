"""python -m unittest discover -s tests"""

import json
import os
import subprocess
import sys
import datetime as dt
import tempfile
import unittest
from pathlib import Path

os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # tests may run in a Claude Code session: leave its task alone

DESK = Path(__file__).resolve().parents[1] / "bin" / "desk.py"

STATE = ("---\nid: t1\ntitle: 제목\nstatus: in_progress\nproject: p\nbranches: []\n"
         "updated: 2026-01-01\nverified: 2026-01-01\n---\n## 목표\n\n본문\n")
TODAY = dt.date.today().isoformat()


def touched(text: str) -> str:
    """STATE-based text as the CLI writes it: `updated` set to today."""
    return text.replace("updated: 2026-01-01", f"updated: {TODAY}")
# a knowledge entry of releases before 0.10: kept in the data root, no longer read
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
        (self.root / "desk.toml").write_text("schema_version = 3\n", encoding="utf-8")
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
        body = "## 목표\n\n본문만\n"
        r = self.run_desk("put", str(self.state), "--file", self.scratch_file(body))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("kept", json.loads(r.stdout))
        fm = STATE[:STATE.index("---\n", 4) + 4]
        self.assertEqual(self.state.read_text(encoding="utf-8"), touched(fm) + body)

    def test_old_knowledge_entry_is_a_plain_file(self):
        r = self.run_desk("put", str(self.entry), "--file", self.scratch_file("새 본문\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.entry.read_text(encoding="utf-8"), "새 본문\n")
        self.assertFalse((self.entry.parent / "index.md").exists())

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

class PutVerified(DeskFixture):
    def test_state(self):
        r = self.run_desk("put", str(self.state), "--verified", "--file", self.scratch_file("## 목표\n\n확인함\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        verified = json.loads(r.stdout)["verified"]
        self.assertTrue(verified.startswith(TODAY))
        text = self.state.read_text(encoding="utf-8")
        self.assertIn(f"verified: {verified}", text)
        self.assertIn(f"updated: {TODAY}", text)

    def test_refused_for_other_files(self):
        for target in (self.log, self.entry):
            r = self.run_desk("put", str(target), "--verified", "--file", self.scratch_file("x\n"))
            self.assertEqual(r.returncode, 1, target.name)
            self.assertIn("--verified", r.stderr)


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
        self.assertTrue(any("updated" in w for w in json.loads(r.stdout)["warnings"]))


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

    def test_log_append_points_to_log_command(self):
        r = self.run_desk("append", str(self.log), "--file", self.scratch_file("## 2026-10-18 01:00\n- a\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("adesk log", json.loads(r.stdout)["warnings"][0])


class Log(DeskFixture):
    def log_entry(self, text: str):
        return self.run_desk("--project", "p", "log", "t1", "--file", "-", stdin=text)

    def test_cli_writes_time_heading(self):
        r = self.log_entry("- 알아낸 것\n- 원인")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertNotIn("note", out)
        self.assertEqual(self.log.read_text(encoding="utf-8"),
                         f"# 태스크 로그\n\n## 2026-10-18 00:00\n- 태스크 생성\n\n## {out['time']}\n- 알아낸 것\n- 원인\n")
        self.assertTrue(out["time"].startswith(TODAY))
        self.assertIn(f"updated: {TODAY}", self.state.read_text(encoding="utf-8"))

    def test_guessed_time_heading_is_replaced(self):
        r = self.log_entry("## 2026-10-18 09:99\n\n- a\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIn("note", out)
        self.assertNotIn("09:99", self.log.read_text(encoding="utf-8"))
        self.assertTrue(self.log.read_text(encoding="utf-8").endswith(f"## {out['time']}\n- a\n"))

    def test_heading_only_is_refused(self):
        r = self.log_entry("## 2026-10-18 01:00\n")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("01:00", self.log.read_text(encoding="utf-8"))


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
    def test_title(self):
        r = self.run_desk("--project", "p", "set", "t1", "--title", "새: 제목 #1")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["title"], out["changed"]), ("새: 제목 #1", True))
        text = self.state.read_text(encoding="utf-8")
        self.assertIn('title: "새: 제목 #1"', text)
        self.assertIn(f"updated: {TODAY}", text)
        self.assertIn("제목 변경: 제목 → 새: 제목 #1", self.log.read_text(encoding="utf-8"))
        self.assertIn("새: 제목 #1", (self.root / "projects" / "p" / "index.md").read_text(encoding="utf-8"))

    def test_parent(self):
        parent = self.state.parents[1] / "t0" / "state.md"
        parent.parent.mkdir()
        parent.write_text(STATE.replace("id: t1", "id: t0"), encoding="utf-8", newline="\n")
        r = self.run_desk("--project", "p", "set", "t1", "--parent", "t0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((json.loads(r.stdout)["parent"], json.loads(r.stdout)["changed"]), ("t0", True))
        self.assertIn("parent: t0\n", self.state.read_text(encoding="utf-8"))
        # one level only: t0 now has a child, so it cannot get a parent itself
        r = self.run_desk("--project", "p", "set", "t0", "--parent", "t1")
        self.assertEqual(r.returncode, 1)
        for clear in (("--no-parent",), ("--parent", "")):
            self.run_desk("--project", "p", "set", "t1", "--parent", "t0")
            r = self.run_desk("--project", "p", "set", "t1", *clear)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIsNone(json.loads(r.stdout)["parent"], clear)
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("상위 태스크 지정: t0", log)
        self.assertIn("상위 태스크 해제: t0", log)

    def test_no_change(self):
        r = self.run_desk("--project", "p", "set", "t1", "--title", "제목")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(json.loads(r.stdout)["changed"])
        self.assertEqual(self.state.read_text(encoding="utf-8"), STATE)

    def test_points_to_the_command_for_other_keys(self):
        for opt, cmd in (("--status", "adesk status"), ("--branch", "adesk link")):
            r = self.run_desk("--project", "p", "set", "t1", opt, "x")
            self.assertEqual(r.returncode, 2, opt)
            self.assertIn(cmd, r.stderr)
        self.assertEqual(self.state.read_text(encoding="utf-8"), STATE)

    def test_relate_points_to_set_parent(self):
        r = self.run_desk("--project", "p", "relate", "t1", "--parent", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("set <id> --parent", r.stderr)

    def test_no_edit_alias(self):
        self.assertEqual(self.run_desk("--project", "p", "edit", "t1", "--title", "x").returncode, 2)


class MigrateLinkFields(DeskFixture):
    """Schema 2 → 3: links and design_doc move to the `## 링크` section of the body."""
    def setUp(self):
        super().setUp()
        (self.root / "desk.toml").write_text("schema_version = 2\n", encoding="utf-8")
        old = STATE.replace("branches: []\n", "branches: []\nlinks: [https://x/1, https://x/2]\ndesign_doc: https://x/doc\n"
                                              "related: [t9]\n")
        self.state.write_text(old, encoding="utf-8", newline="\n")
        self.other = self.state.parents[1] / "t2" / "state.md"
        self.other.parent.mkdir()
        self.other.write_text(STATE.replace("id: t1", "id: t2").replace("branches: []\n", "branches: []\nlinks: []\ndesign_doc:\nrelated: []\n")
                              + "\n## 링크\n\n- 이미 있던 링크\n\n## 뒤 섹션\n", encoding="utf-8", newline="\n")

    def test_moves_values_and_drops_fields(self):
        r = self.run_desk("migrate", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c["task"] for c in json.loads(r.stdout)["changed"]], ["t1"])
        self.assertIn("design_doc", self.state.read_text(encoding="utf-8"))
        r = self.run_desk("migrate")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state.read_text(encoding="utf-8"),
                         STATE + "\n## 링크\n\n- 설계 문서: https://x/doc\n- https://x/1\n- https://x/2\n- 연관 태스크: t9\n")
        self.assertEqual(self.other.read_text(encoding="utf-8"),
                         STATE.replace("id: t1", "id: t2") + "\n## 링크\n\n- 이미 있던 링크\n\n## 뒤 섹션\n")
        self.assertIn("schema_version = 3", (self.root / "desk.toml").read_text(encoding="utf-8"))

    def test_appends_to_existing_section(self):
        self.state.write_text(self.state.read_text(encoding="utf-8") + "\n## 링크\n\n- 이미 있던 링크\n\n## 뒤 섹션\n",
                              encoding="utf-8", newline="\n")
        self.assertEqual(self.run_desk("migrate").returncode, 0)
        self.assertTrue(self.state.read_text(encoding="utf-8").endswith(
            "## 링크\n\n- 이미 있던 링크\n- 설계 문서: https://x/doc\n- https://x/1\n- https://x/2\n- 연관 태스크: t9\n\n## 뒤 섹션\n"))


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

    def put(self, text: str) -> list[str]:
        r = self.run_desk("put", str(self.decisions), "--file", self.scratch_file(text))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout).get("warnings", [])

    def test_gap_is_not_a_problem(self):
        # the user deleted D2 (numbers are not reused), or wrote the file that way
        self.append("## D1. a  (2026-10-18, 유효)\n")
        self.assertEqual(self.append("## D3. c  (2026-10-18, 유효)\n"), [])
        self.assertEqual(self.put("# 결정\n\n## D1. a  (2026-10-18, 유효)\n\n## D3. c  (2026-10-18, 유효)\n"), [])

    def test_put_dropping_a_decision(self):
        self.put("# 결정\n\n## D1. a  (2026-10-18, 유효)\n\n## D2. b  (2026-10-18, 유효)\n\n## D3. c  (2026-10-18, 유효)\n")
        warnings = self.put("# 결정\n\n## D1. a  (2026-10-18, 대체됨 → D3)\n\n## D3. c  (2026-10-18, 유효)\n")
        self.assertEqual(len(warnings), 1)
        self.assertIn("사라졌습니다: D2", warnings[0])
        # once written, the gap is the file's state: later writes do not warn again
        self.assertEqual(self.append("## D4. d  (2026-10-18, 유효)\n"), [])

    def test_taken_number_is_refused(self):
        self.append("## D1. a  (2026-10-18, 유효)\n")
        self.append("## D2. b  (2026-10-18, 유효)\n")
        before = self.decisions.read_text(encoding="utf-8")
        r = self.run_desk("append", str(self.decisions), "--file", self.scratch_file("## D2. c  (2026-10-18, 유효)\n"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("D2", r.stderr)
        self.assertIn("다음 번호는 D3", r.stderr)
        self.assertEqual(self.decisions.read_text(encoding="utf-8"), before)

    def test_duplicate_within_appended_text(self):
        warnings = self.append("## D1. a  (2026-10-18, 유효)\n\n## D1. b  (2026-10-18, 유효)\n")
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
        warnings = self.put("# 결정\n\n## D2. b  (2026-10-18, 유효)\n\n## D1. a  (2026-10-18, 유효)\n")
        self.assertEqual(len(warnings), 1)
        self.assertIn("순서대로가 아닙니다", warnings[0])


if __name__ == "__main__":
    unittest.main()
