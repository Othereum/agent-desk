"""Injection regression tests: what each hook actually injects, scene by scene.

Layer 1 (snapshots): every scene builds a throwaway desk (data root, git repository, session
cache) and runs the real hook entry point (`python bin/desk.py hook <event>`) with a hook
payload. Scenes with a distinct shape render their output as readable text and compare it with
tests/snapshots/injection/<scene>.txt; the fixture records are one-liners so the snapshot shows
the lines the hook writes. Variant scenes (todo, child counts, schema...) keep no snapshot:
they check only the lines that differ from their base scene. Layer 2 (invariants): every hook
call must exit 0 with empty stderr and print nothing or one valid hook JSON within the size
budget; the context kinds have required lines.

When an injection change is intended, regenerate the snapshots and review the diff:
    AGENT_DESK_UPDATE_SNAPSHOTS=1 python -m unittest discover -s tests
    git diff tests/snapshots

Isolation: the hook and CLI child processes get AGENT_DESK_ROOT, HOME/USERPROFILE,
TEMP/TMP/TMPDIR (session cache) and the git global config pointed inside a temp dir, so
nothing reads or writes the real data root, ~/.claude or the real session cache.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DESK_PY = REPO / "bin" / "desk.py"
SNAPSHOTS = Path(__file__).resolve().parent / "snapshots" / "injection"
UPDATE = os.environ.get("AGENT_DESK_UPDATE_SNAPSHOTS") == "1"

# Fixture budget for one injection. The fixture records are small, so this catches the
# injected wording itself growing (or a record being injected twice), not real record size.
CONTEXT_MAX_CHARS = 4000
KN_INJECT_LIMIT = 15
TASK_LAST_LINE = "결정을 확정하거나 단계를 끝내면 바로 decisions.md·state.md·log.md에 기록한다."  # K0033


def child_env(base: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("AGENT_DESK_", "CLAUDE", "GIT_"))}
    # drop the Windows app execution alias directory: the python-alias systemMessage depends on
    # the machine. The hook runs sys.executable, so python need not be on PATH.
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                  if "\\microsoft\\windowsapps" not in p.casefold())
    gitconfig = base / "gitconfig"
    gitconfig.write_text("", encoding="utf-8")
    env.update({
        "AGENT_DESK_ROOT": str(base / "desk"),
        "HOME": str(base / "home"), "USERPROFILE": str(base / "home"),
        "TEMP": str(base / "tmp"), "TMP": str(base / "tmp"), "TMPDIR": str(base / "tmp"),
        "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
    })
    return env


class Desk:
    """A throwaway desk: data root with project myapp registered, and its git repository."""

    def __init__(self, base: Path, template: "Desk | None" = None):
        self.base = base
        self.root = base / "desk"
        self.repo = base / "myapp"
        self.project_dir = self.root / "projects" / "myapp"
        self.env = child_env(base)
        if template:  # a copy is much faster than git init + commit + project-init per scene
            for d in ("home", "tmp", "myapp", "desk"):
                shutil.copytree(template.base / d, base / d)
            toml = self.project_dir / "project.toml"
            toml.write_text(toml.read_text(encoding="utf-8").replace(template.base.as_posix(), base.as_posix()),
                            encoding="utf-8")
            return
        for d in ("home", "tmp", "myapp"):
            (base / d).mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "https://example.com/team/myapp.git")
        (self.repo / "README.md").write_text("myapp\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "init")
        self.cli("project-init", "myapp")

    def run(self, args: list, cwd: Path | None = None, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(args, cwd=cwd or self.repo, env=self.env, input=stdin, capture_output=True,
                              text=True, encoding="utf-8", timeout=60)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        r = self.run(["git", *args], cwd)
        if r.returncode:
            raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
        return r.stdout

    def cli(self, *args: str, cwd: Path | None = None, env: dict | None = None):
        r = self.session_cli(*args, cwd=cwd, env=env or {})
        return json.loads(r.stdout) if r.stdout.strip() else None

    def session_cli(self, *args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
        """A CLI call as the Bash tool of session s1 makes it (env=None), or with the given extra env."""
        env = {"CLAUDE_CODE_SESSION_ID": "s1"} if env is None else env
        r = subprocess.run([sys.executable, str(DESK_PY), *args], cwd=cwd or self.repo, env={**self.env, **env},
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        if r.returncode:
            raise AssertionError(f"adesk {' '.join(args)}: {r.stderr}")
        return r

    def focus(self, task_id: str) -> str:
        """`adesk focus` in session s1: the records it prints."""
        return self.session_cli("focus", task_id).stdout

    def new(self, title: str, *args: str) -> str:
        """The state.md body is cut to one line: snapshots show what the hook writes."""
        task_id = self.cli("new", "--title", title, *args)["task"]
        self.fill_state(task_id, goal=f"{title}.")
        return task_id

    def task_dir(self, task_id: str) -> Path:
        return self.project_dir / "tasks" / task_id

    def fill_state(self, task_id: str, **sections: str) -> None:
        """Replace the state.md body with the given sections: goal, now, next."""
        names = {"goal": "목표", "now": "현재 상태", "next": "다음 할 일"}
        path = self.task_dir(task_id) / "state.md"
        text = path.read_text(encoding="utf-8")
        head = text[:text.index("\n---\n", 4) + 5]
        body = "".join(f"## {names[k]}\n{v}\n" for k, v in sections.items())
        path.write_text(head + body, encoding="utf-8")

    def add_decision(self, task_id: str, title: str, body: str = "- 결정: (본문)") -> None:
        path = self.task_dir(task_id) / "decisions.md"
        text = path.read_text(encoding="utf-8")
        n = len(re.findall(r"^## D\d+\.", re.sub(r"<!--.*?-->", "", text, flags=re.S), re.M)) + 1
        path.write_text(text.rstrip("\n") + f"\n\n## D{n}. {title}  (2026-01-01, 유효)\n{body}\n", encoding="utf-8")

    def add_knowledge(self, n: int, title: str, kind: str, paths: list[str], status: str = "valid",
                      location: str = "inline", stale_reason: str = "") -> None:
        """Written directly (not `kn add`) so dates and ids are fixed."""
        kdir = self.project_dir / "knowledge"
        kdir.mkdir(exist_ok=True)
        lines = ["---", f"id: K{n:04d}", f'title: "{title}"', f"kind: {kind}",
                 f"paths: [{', '.join(paths)}]", "tags: []", "sources: []", f"location: {location}",
                 f"status: {status}", "created: 2026-01-01", "verified: 2026-01-01 00:00"]
        if stale_reason:
            lines.append(f'stale_reason: "{stale_reason}"')
        (kdir / f"K{n:04d}-entry.md").write_text("\n".join(lines + ["---", "- 본문", ""]), encoding="utf-8")

    def set_doctor_age(self, days: float | None) -> None:
        marker = self.project_dir / ".last-doctor"
        if days is None:
            marker.unlink(missing_ok=True)
            return
        marker.write_text("", encoding="utf-8")
        t = time.time() - days * 86400
        os.utime(marker, (t, t))

    def checkout(self, *args: str) -> None:
        self.git("checkout", "-q", *args)

    def hook(self, event: str, payload: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
        cwd = cwd or self.repo
        payload = {"session_id": "s1", "cwd": str(cwd), **payload}
        return self.run([sys.executable, str(DESK_PY), "hook", event], cwd, json.dumps(payload, ensure_ascii=False))

    def normalize(self, text: str) -> str:
        today = dt.date.today()
        for p in (self.base.as_posix(), str(self.base)):
            text = text.replace(p, "<TMP>")
        text = re.sub(rf"{today.isoformat()} \d\d:\d\d", "<DATE> <TIME>", text)
        text = text.replace(today.isoformat(), "<DATE>")
        return re.sub(rf"\b{today.strftime('%y%m%d')}-", "<YYMMDD>-", text)


def remove_tree(path: Path) -> None:
    """rmtree that also removes git's read-only object files on Windows."""
    def retry(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)
    shutil.rmtree(path, **({"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}))


_template: Desk | None = None


def new_desk() -> tuple[Path, Desk]:
    global _template
    if _template is None:
        _template = Desk(Path(tempfile.mkdtemp(prefix="adesk-inject-")).resolve())
    base = Path(tempfile.mkdtemp(prefix="adesk-inject-")).resolve()
    return base, Desk(base, _template)


def tearDownModule():
    if _template:
        remove_tree(_template.base)


EVENT_NAMES = {"session-start": "SessionStart", "user-prompt-submit": "UserPromptSubmit",
               "pre-tool-use": "PreToolUse", "post-tool-use": "PostToolUse"}


class InjectionTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.base, self.desk = new_desk()
        self.addCleanup(remove_tree, self.base)
        self.calls: list[str] = []

    # --- layer 2: invariants of every hook call -------------------------------------------

    def call(self, label: str, event: str, payload: dict, cwd: Path | None = None,
             expect_failure: bool = False) -> str | None:
        """Run a hook, check the invariants, record it for the scene snapshot; returns the context."""
        r = self.desk.hook(event, payload, cwd)
        self.assertEqual(r.returncode, 0, f"{label}: exit {r.returncode}\n{r.stderr}")
        if not expect_failure:
            self.assertEqual(r.stderr, "", f"{label}: stderr")
        out = r.stdout.strip()
        rendered = [f"=== {label}"]
        context = None
        if not out:
            self.assertFalse(expect_failure, f"{label}: expected a failure message")
            rendered.append("(출력 없음)")
        else:
            data = json.loads(out)  # exactly one JSON document
            self.assertIsInstance(data, dict)
            self.assertLessEqual(set(data), {"hookSpecificOutput", "systemMessage"}, label)
            if "systemMessage" in data:
                self.assertIsInstance(data["systemMessage"], str)
                rendered.append(f"[systemMessage] {data['systemMessage']}")
            self.assertEqual(expect_failure, "systemMessage" in data and "실패" in data["systemMessage"], label)
            if "hookSpecificOutput" in data and event == "pre-tool-use":
                hso = data["hookSpecificOutput"]
                reason = hso.get("permissionDecisionReason")
                self.assertEqual(hso, {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}, label)
                self.assertTrue(isinstance(reason, str) and reason.strip(), label)
                rendered += ["[deny PreToolUse]", reason]
            elif "hookSpecificOutput" in data:
                hso = data["hookSpecificOutput"]
                expected_event = EVENT_NAMES[event]
                self.assertEqual(hso.get("hookEventName"), expected_event, label)
                context = hso.get("additionalContext")
                self.assertIsInstance(context, str)
                self.assertTrue(context.strip(), f"{label}: empty additionalContext")
                self.assertLessEqual(len(context), CONTEXT_MAX_CHARS, f"{label}: context too long")
                rendered += [f"[additionalContext {expected_event}]", context]
        self.calls.append(self.desk.normalize("\n".join(rendered)))
        return context

    def assert_task_context(self, ctx: str, task_id: str) -> None:
        lines = ctx.splitlines()
        self.assertTrue(lines[0].startswith("[agent-desk] 프로젝트 myapp · 브랜치 "), lines[0])
        self.assertIn(f"· 포커스 {task_id} (", lines[0])
        self.assertTrue(lines[1].startswith("태스크 폴더: "), lines[1])
        self.assertNotIn("포커스 태스크는", ctx)
        self.assertIn("--- state.md ---", lines)
        self.assertIn(f"id: {task_id}", lines)
        self.assertIn("--- 결정 목록 (본문은 decisions.md) ---", lines)
        self.assertEqual(ctx.count("--- state.md ---"), 1)
        self.assertIn(TASK_LAST_LINE, lines[-1])

    def assert_candidates(self, ctx: str, current: str = "없음") -> None:
        """Candidates to choose from: no task records, the session's focus (or none) on the first line."""
        lines = ctx.splitlines()
        self.assertTrue(lines[0].startswith("[agent-desk] 프로젝트 myapp · 브랜치 "), lines[0])
        self.assertTrue(lines[0].endswith(f" · 포커스 {current}"), lines[0])
        self.assertIn("agent-desk:desk 스킬", lines[-1])
        self.assertNotIn("--- state.md ---", lines)

    def assert_knowledge_context(self, ctx: str, rel: str) -> list[str]:
        lines = ctx.splitlines()
        self.assertEqual(lines[0], f"[agent-desk knowledge] {rel} 관련 항목:")
        self.assertTrue(lines[-1].startswith("항목 파일: "), lines[-1])
        items = [l for l in lines[1:-1] if re.match(r"- K\d{4} ", l)]
        self.assertLessEqual(len(items), KN_INJECT_LIMIT)
        self.assertEqual(len(items), len(set(items)), "duplicate entries")
        return [l.split()[1] for l in items]

    # --- layer 1: snapshot, or the difference from a base scene ------------------------------

    def assert_differs(self, base: str, ctx: str, added: list[str], removed: list[str] = ()) -> None:
        """Every line ctx adds to base starts with one of `added` and each prefix matches one;
        the same for the lines it drops and `removed`."""
        base_lines, lines = base.splitlines(), ctx.splitlines()
        for got, prefixes, what in (([l for l in lines if l not in base_lines], added, "추가된 줄"),
                                    ([l for l in base_lines if l not in lines], removed, "빠진 줄")):
            for line in got:
                self.assertTrue(any(line.startswith(p) for p in prefixes), f"{what}: {line!r}")
            for prefix in prefixes:
                self.assertTrue(any(l.startswith(prefix) for l in got), f"{what} 없음: {prefix!r}")

    def snapshot(self, name: str, description: str) -> None:
        text = f"# {description}\n\n" + "\n\n".join(self.calls) + "\n"
        path = SNAPSHOTS / f"{name}.txt"
        if UPDATE:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            return
        if not path.exists():
            self.fail(f"기준 파일 없음: {path}. AGENT_DESK_UPDATE_SNAPSHOTS=1 로 만들고 내용을 검토할 것")
        self.assertEqual(path.read_text(encoding="utf-8"), text,
                         f"{name}: 주입이 기준과 다르다. 의도한 변경이면 AGENT_DESK_UPDATE_SNAPSHOTS=1 로 갱신")

    # --- fixtures shared by scenes ------------------------------------------------------------

    def login_task(self) -> str:
        """README "다음 날 이어서": feature/login with state and decision D1."""
        d = self.desk
        d.checkout("-b", "feature/login")
        tid = d.new("로그인 기능")
        d.fill_state(tid, goal="로그인 기능을 만든다.", now="- 로그인 API 완료.", next="- 토큰 갱신")
        d.add_decision(tid, "세션 캐시는 Redis에 둔다")
        return tid

    # --- scenes -------------------------------------------------------------------------------

    def test_unregistered_repo(self):
        d = self.desk
        other = self.base / "other"
        other.mkdir()
        d.git("init", "-q", "-b", "main", cwd=other)
        self.assertIsNone(self.call("SessionStart startup (미등록 저장소)", "session-start",
                                    {"source": "startup"}, cwd=other))
        self.assertIsNone(self.call("UserPromptSubmit (미등록 저장소)", "user-prompt-submit", {}, cwd=other))
        self.assertIsNone(self.call("PostToolUse Read (미등록 저장소)", "post-tool-use",
                                    {"tool_name": "Read", "tool_input": {"file_path": str(other / "a.py")}},
                                    cwd=other))
        self.snapshot("unregistered_repo", "등록하지 않은 저장소에서는 아무것도 주입하지 않는다 (README 처음 쓰는 저장소)")

    def test_no_task_lists_open_tasks(self):
        d = self.desk
        d.checkout("-b", "feature/login")
        d.new("로그인 기능")
        d.checkout("main")
        d.new("검색 개선", "--no-branch", "--slug", "search-improve", "--todo")
        ctx = self.call("SessionStart startup (main)", "session-start", {"source": "startup"})
        self.assert_candidates(ctx)
        self.assertIn("--- 열린 태스크 ---", ctx)
        self.assertIn("사용자의 요청이 이 중 하나를 분명히 가리키면 `adesk focus <id>` 로 포커스하고, 아니면 묻는다.", ctx)
        self.snapshot("no_task_lists_open_tasks",
                      "브랜치에 연결된 태스크가 없으면 열린 태스크 전체를 후보로 보여 주고 요청으로 고르게 한다 (K0041)")

    def test_branch_task_recommended(self):
        tid = self.login_task()
        self.desk.new("검색 개선", "--no-branch", "--slug", "search-improve", "--todo")
        self.desk.add_knowledge(1, "세션 토큰 캐시는 설정을 다시 읽어도 초기화되지 않는다", "pitfall", ["src/auth/**"])
        ctx = self.call("SessionStart startup", "session-start", {"source": "startup"})
        self.assert_candidates(ctx)
        self.assertIn(f"이 브랜치의 태스크: {tid} (로그인 기능) · in_progress", ctx)
        self.assertIn("다른 열린 태스크 1건:", ctx)
        self.assertIn("사용자의 요청이 이 브랜치의 일이면 묻지 않고 `adesk focus <id>` 로 포커스하고", ctx)
        self.assertIn("doctor를 한 번도 실행하지 않았다", ctx)
        self.snapshot("branch_task_recommended",
                      "브랜치에 연결된 태스크는 기록을 넣지 않고 추천한다 (README 다음 날 이어서, 낡은 지식 점검 제안)")

    def test_focus_then_compact_resume_clear(self):
        d = self.desk
        tid = self.login_task()
        self.call("SessionStart startup", "session-start", {"source": "startup"})
        shown = d.focus(tid)
        self.assert_task_context(shown, tid)
        self.assertIn("- D1. 세션 캐시는 Redis에 둔다  (2026-01-01, 유효)", shown)
        self.assertNotIn("CLI: ", shown)  # the session already has it from SessionStart
        self.calls.append(d.normalize(f"=== adesk focus {tid}\n{shown.rstrip()}"))
        self.assertEqual(d.cli("where", env={"CLAUDE_CODE_SESSION_ID": "s1"})["focus"], tid)
        ctx = self.call("SessionStart compact", "session-start", {"source": "compact"})
        self.assert_task_context(ctx, tid)
        self.assertIn("컨텍스트가 방금 압축되었다.", ctx)  # K0009
        resumed = self.call("SessionStart resume", "session-start", {"source": "resume"})
        self.assert_differs(ctx, resumed, added=[], removed=["컨텍스트가 방금 압축되었다."])
        self.assert_candidates(self.call("SessionStart clear (새 세션 ID)", "session-start",
                                         {"source": "clear", "session_id": "s2"}))
        self.snapshot("focus_then_compact_resume_clear",
                      "요청으로 정한 포커스 태스크의 기록은 focus가 출력하고, 압축·재개 뒤에도 그 세션에 다시 주입한다. "
                      "/clear는 새 세션이라 후보부터 다시 고른다 (D4)")

    def test_focus_todo_starts_it(self):
        d = self.desk
        d.checkout("-b", "feature/search")
        tid = d.new("검색 개선", "--todo")
        ctx = self.call("SessionStart startup", "session-start", {"source": "startup"})
        self.assertIn(f"이 브랜치의 태스크: {tid} (검색 개선) · todo", ctx)
        shown = d.focus(tid)
        self.assertIn(f"· 포커스 {tid} (in_progress)", shown.splitlines()[0])
        self.assertIn("상태 변경: todo → in_progress (포커스)", (d.task_dir(tid) / "log.md").read_text(encoding="utf-8"))

    def test_new_focuses_task_in_session(self):
        d = self.desk
        r = d.cli("new", "--title", "문서 정리", "--no-branch", "--slug", "docs", env={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertTrue(r["focused"])
        self.assert_task_context(self.call("SessionStart resume", "session-start", {"source": "resume"}), r["task"])
        todo = d.cli("new", "--title", "검색 개선", "--no-branch", "--slug", "search", "--todo",
                     env={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertFalse(todo["focused"])  # a task for later is not this session's work
        self.assertFalse(d.cli("new", "--title", "x", "--no-branch", "--slug", "x")["focused"])  # outside a session

    def test_focus_differs_from_branch(self):
        d = self.desk
        d.git("branch", "feature/login")
        login = d.new("로그인 기능", "--branch", "feature/login")
        docs = d.new("문서 정리", "--no-branch", "--slug", "docs")
        self.call("SessionStart startup (main)", "session-start", {"source": "startup"})
        d.focus(docs)
        self.assertIsNone(self.call("UserPromptSubmit (처음 정한 포커스는 알리지 않음)", "user-prompt-submit", {}))
        d.checkout("feature/login")
        ctx = self.call("UserPromptSubmit (feature/login 체크아웃)", "user-prompt-submit", {})
        lines = ctx.splitlines()
        self.assertEqual(lines[0], "[agent-desk] 작업 위치가 바뀌었다 (myapp · main → myapp · feature/login).")
        self.assert_candidates("\n".join(lines[1:]), current=f"{docs} (문서 정리)")
        self.assertIn(f"사용자의 일이 이 브랜치의 태스크로 바뀌었으면 `adesk focus {login}` 로 포커스를 옮길 것.", ctx)
        ctx = self.call("SessionStart resume (feature/login)", "session-start", {"source": "resume"})
        self.assert_task_context(ctx, docs)
        self.assertIn(f"이 브랜치의 태스크는 {login}(로그인 기능)로, 포커스와 다르다.", ctx)
        self.snapshot("focus_differs_from_branch",
                      "포커스와 브랜치의 태스크가 다르면 옮길 방법을 알린다 (D4)")

    def test_focus_change_notice(self):
        d = self.desk
        a = d.new("로그인 기능", "--no-branch", "--slug", "login")
        b = d.new("문서 정리", "--no-branch", "--slug", "docs")
        self.call("SessionStart startup", "session-start", {"source": "startup"})
        d.focus(a)
        self.assertIsNone(self.call("UserPromptSubmit (처음 정한 포커스)", "user-prompt-submit", {}))
        d.focus(b)  # e.g. a subagent sharing the session id
        ctx = self.call("UserPromptSubmit (포커스가 바뀜)", "user-prompt-submit", {})
        self.assertTrue(ctx.startswith(f"[agent-desk] 이 세션의 포커스가 바뀌었다: {a} → {b}."), ctx)
        self.assertIsNone(self.call("UserPromptSubmit (다시)", "user-prompt-submit", {}))
        d.session_cli("focus", "--clear")
        ctx = self.call("UserPromptSubmit (해제)", "user-prompt-submit", {})
        self.assertTrue(ctx.startswith(f"[agent-desk] 이 세션의 포커스가 바뀌었다: {b} → 없음."), ctx)
        self.snapshot("focus_change_notice", "이 세션의 포커스가 바뀌면 다음 프롬프트에서 알린다 (서브에이전트가 바꾼 경우 대비, D3)")

    def test_subtask_with_parent(self):
        d = self.desk
        parent = d.new("1.0 준비", "--no-branch", "--slug", "v1")
        d.fill_state(parent, goal="1.0을 낸다.")
        d.add_decision(parent, "1.0 기준")
        d.new("문서 정리", "--no-branch", "--slug", "docs", "--parent", parent, "--todo")
        d.checkout("-b", "feature/login")
        child = d.new("로그인 기능", "--parent", parent)
        d.focus(child)
        ctx = self.call("SessionStart resume (하위 태스크)", "session-start", {"source": "resume"})
        self.assert_task_context(ctx, child)
        self.assertIn(f"--- 상위 태스크 {parent} state.md", ctx)
        self.assertIn("- D1. 1.0 기준  (2026-01-01, 유효)", ctx)
        self.assertIn("다른 열린 태스크 2건:", ctx)
        self.snapshot("subtask_with_parent", "하위 태스크 세션에 상위 태스크 현황과 결정 목록을 함께 주입한다 (K0026)")

    def test_parent_task_counts_children(self):
        d = self.desk
        parent = d.new("1.0 준비", "--no-branch", "--slug", "v1")
        d.focus(parent)
        base = self.call("SessionStart resume (상위 태스크)", "session-start", {"source": "resume"})
        d.new("문서 정리", "--no-branch", "--slug", "docs", "--parent", parent, "--todo")
        d.new("로그인 기능", "--branch", "feature/login", "--parent", parent)
        ctx = self.call("SessionStart resume (하위 2건)", "session-start", {"source": "resume"})
        self.assert_task_context(ctx, parent)
        self.assert_differs(base, ctx, added=["다른 열린 태스크 2건(하위 2건):"])  # K0041

    def test_location_change_prompt(self):
        tid = self.login_task()
        d = self.desk
        d.checkout("main")
        self.assert_candidates(self.call("SessionStart startup (main)", "session-start", {"source": "startup"}))
        self.assertIsNone(self.call("UserPromptSubmit (같은 위치)", "user-prompt-submit", {}))
        d.checkout("feature/login")
        ctx = self.call("UserPromptSubmit (세션 밖에서 feature/login 체크아웃)", "user-prompt-submit", {})
        self.assertTrue(ctx.startswith("[agent-desk] 작업 위치가 바뀌었다 (myapp · main → myapp · feature/login).\n"))
        self.assert_candidates(ctx.split("\n", 1)[1])
        self.assertIn(f"이 브랜치의 태스크: {tid} (로그인 기능)", ctx)
        self.assertIsNone(self.call("UserPromptSubmit (다시, 같은 위치)", "user-prompt-submit", {}))
        self.snapshot("location_change_prompt", "세션 중 브랜치가 바뀌면 다음 프롬프트에서 새 위치의 후보를 알린다 (K0025)")

    def test_location_change_worktree(self):
        d = self.desk
        tid = d.new("로그인 기능", "--branch", "feature/login")
        wt = self.base / "myapp-wt"
        d.git("worktree", "add", "-q", str(wt), "-b", "feature/login")
        self.call("SessionStart startup (main)", "session-start", {"source": "startup"})
        ctx = self.call("PostToolUse EnterWorktree", "post-tool-use",
                        {"tool_name": "EnterWorktree", "tool_input": {}}, cwd=wt)
        self.assertTrue(ctx.startswith("[agent-desk] 작업 위치가 바뀌었다 (myapp · main → myapp-wt · feature/login).\n"))
        self.assert_candidates(ctx.split("\n", 1)[1])
        self.assertIn(f"이 브랜치의 태스크: {tid} (로그인 기능)", ctx)
        ctx = self.call("PostToolUse ExitWorktree", "post-tool-use",
                        {"tool_name": "ExitWorktree", "tool_input": {}})
        self.assertTrue(ctx.startswith("[agent-desk] 작업 위치가 바뀌었다 (myapp-wt · feature/login → myapp · main)."))
        d.focus(tid)  # the main agent's focus: a subagent does not work on it
        ctx = self.call("PostToolUse EnterWorktree (서브에이전트)", "post-tool-use",
                        {"tool_name": "EnterWorktree", "tool_input": {}, "agent_id": "a1"}, cwd=wt)
        self.assertTrue(ctx.startswith("[agent-desk] 작업 위치가 바뀌었다 (myapp-wt · feature/login).\n"))
        self.assert_candidates(ctx.split("\n", 1)[1])
        self.assertIn("서브에이전트는 이 세션의 포커스를 바꾸지 않는다", ctx)
        self.assertIsNone(self.call("UserPromptSubmit (메인 위치는 그대로)", "user-prompt-submit", {}))
        self.snapshot("location_change_worktree", "worktree 진입·이탈 시 새 위치의 후보를 알린다. "
                                                  "서브에이전트에게는 세션의 포커스를 바꾸지 말라고 알린다 (K0025, G0007, D2)")

    def test_knowledge_file_access(self):
        d = self.desk
        kinds = ["decision", "pitfall", "finding", "howto"]
        for n in range(1, 18):  # 17 live entries on src/auth/**: more than the limit
            d.add_knowledge(n, f"auth 항목 {n}", kinds[n % 4], ["src/auth/**"])
        d.add_knowledge(18, "갱신 요청이 겹치면 토큰이 두 번 발급된다", "pitfall", ["src/auth/token.ts"],
                        status="stale", stale_reason="갱신 코드가 바뀜")
        d.add_knowledge(19, "토큰 갱신 설계", "decision", ["src/auth/token.ts"], location="repo:docs/token.md")
        d.add_knowledge(20, "대체된 항목", "pitfall", ["src/auth/**"], status="superseded")
        d.add_knowledge(21, "결제 웹훅 중복 처리", "pitfall", ["src/payments/**"])
        src = d.repo / "src" / "auth"
        read = lambda name, **kw: {"tool_name": "Read", "tool_input": {"file_path": str(src / name)}, **kw}

        ids = self.assert_knowledge_context(self.call("PostToolUse Read src/auth/token.ts", "post-tool-use",
                                                      read("token.ts")), "src/auth/token.ts")
        self.assertEqual(len(ids), KN_INJECT_LIMIT)
        kinds_of = {f"K{n:04d}": ("pitfall" if n == 18 else "decision" if n == 19 else kinds[n % 4])
                    for n in range(1, 20)}
        order = ["pitfall", "finding", "howto", "decision"]
        ranks = [order.index(kinds_of[i]) for i in ids]
        self.assertEqual(ranks, sorted(ranks), "pitfalls first")
        self.assertNotIn("K0020", ids)  # superseded
        self.assertNotIn("K0021", ids)  # other path
        rest = self.assert_knowledge_context(self.call("PostToolUse Read src/auth/token.ts (다시)", "post-tool-use",
                                                       read("token.ts")), "src/auth/token.ts")
        self.assertEqual(len(set(ids) | set(rest)), 19)
        self.assertIsNone(self.call("PostToolUse Read src/auth/session.ts (모두 보임)", "post-tool-use",
                                    read("session.ts")))
        sub = self.assert_knowledge_context(self.call("PostToolUse Read src/auth/session.ts (서브에이전트)",
                                                      "post-tool-use", read("session.ts", agent_id="a1")),
                                            "src/auth/session.ts")
        self.assertEqual(len(sub), KN_INJECT_LIMIT)
        self.assertIsNone(self.call("PostToolUse Edit README.md (매칭 없음)", "post-tool-use",
                                    {"tool_name": "Edit", "tool_input": {"file_path": str(d.repo / "README.md")}}))
        self.call("SessionStart compact (주입 이력 초기화)", "session-start", {"source": "compact"})
        again = self.assert_knowledge_context(self.call("PostToolUse Read src/auth/session.ts (압축 뒤)",
                                                        "post-tool-use", read("session.ts")), "src/auth/session.ts")
        self.assertEqual(len(again), KN_INJECT_LIMIT)
        self.snapshot("knowledge_file_access",
                      "파일 접근 시 paths가 맞는 지식 제목을 함정부터 15건까지 한 번씩 주입한다 "
                      "(README 함정을 발견하면 지식으로, K0036, K0039)")

    def test_desk_file_edit(self):
        d = self.desk
        tid = d.new("로그인 기능", "--branch", "feature/login")
        state = d.task_dir(tid) / "state.md"
        edit = {"tool_name": "Edit", "tool_input": {"file_path": str(state)}}
        self.call("PreToolUse Edit state.md (차단)", "pre-tool-use", edit)
        self.assertIsNone(self.call("PreToolUse Read state.md", "pre-tool-use",
                                    {"tool_name": "Read", "tool_input": {"file_path": str(state)}}))
        self.assertIsNone(self.call("PreToolUse Edit README.md", "pre-tool-use",
                                    {"tool_name": "Edit", "tool_input": {"file_path": str(d.repo / "README.md")}}))
        self.assertIsNone(self.call("PostToolUse Edit state.md (정상)", "post-tool-use", edit))
        state.write_text(state.read_text(encoding="utf-8").replace("title: 로그인 기능", 'title: "로그인'),
                         encoding="utf-8")
        ctx = self.call("PostToolUse Edit state.md (frontmatter 깨짐)", "post-tool-use", edit)
        self.assertIn("frontmatter를 읽을 수 없다", ctx)
        self.snapshot("desk_file_edit", "데이터 폴더 파일의 Edit/Write를 막고 put·append를 안내한다. "
                                        "기록 파일의 frontmatter가 깨졌으면 알린다 (desk-rewind D1)")

    def test_old_schema(self):
        d = self.desk
        d.new("문서 정리", "--no-branch", "--slug", "docs")
        base = self.call("SessionStart startup", "session-start", {"source": "startup"})
        (d.root / "desk.toml").write_text("schema_version = 1\n", encoding="utf-8")
        ctx = self.call("SessionStart startup (스키마 1)", "session-start", {"source": "startup"})
        self.assertIn("`adesk migrate`", ctx)  # K0042
        self.assert_differs(base, ctx, added=[next(l for l in ctx.splitlines() if "`adesk migrate`" in l)])

    def test_doctor_reminder(self):
        d = self.desk
        d.add_knowledge(1, "항목", "pitfall", ["src/**"])
        d.set_doctor_age(1)
        base = self.call("SessionStart startup (doctor 1일 전)", "session-start", {"source": "startup"})
        self.assertNotIn("doctor를", base)
        d.set_doctor_age(10)
        ctx = self.call("SessionStart startup (doctor 10일 전)", "session-start", {"source": "startup"})
        self.assertIn("doctor를 10일째 실행하지 않았다", ctx)
        self.assert_differs(base, ctx, added=[next(l for l in ctx.splitlines() if "doctor를 10일째" in l)])

    def test_hook_failure(self):
        d = self.desk
        r = d.run([sys.executable, str(DESK_PY), "hook", "session-start"], stdin="{broken")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertEqual(set(data), {"systemMessage"})
        self.assertTrue(data["systemMessage"].startswith("[agent-desk] hook session-start 실패"))
        self.calls.append(d.normalize(f"=== SessionStart (입력 JSON 깨짐)\n[systemMessage] {data['systemMessage']}"))
        self.snapshot("hook_failure", "hook이 실패하면 사용자에게 보이는 systemMessage로 알린다 (주입 누락을 숨기지 않음)")


class IsolationTest(unittest.TestCase):
    def test_child_processes_stay_in_the_temp_dir(self):
        base = Path(tempfile.mkdtemp(prefix="adesk-inject-")).resolve()
        self.addCleanup(remove_tree, base)
        d = Desk(base)
        d.new("문서 정리", "--no-branch", "--slug", "docs")
        d.add_knowledge(1, "항목", "pitfall", ["src/**"])
        d.hook("post-tool-use", {"tool_name": "Read", "tool_input": {"file_path": str(d.repo / "src" / "a.py")}})
        r = d.run([sys.executable, str(DESK_PY), "where"])
        where = json.loads(r.stdout)
        self.assertEqual(Path(where["root"]).resolve(), (base / "desk").resolve())
        self.assertTrue((base / "tmp" / "agent-desk-sessions").is_dir(), "session cache is not isolated")
        r = d.run([sys.executable, "-c", "import pathlib; print(pathlib.Path.home())"])
        self.assertEqual(Path(r.stdout.strip()).resolve(), (base / "home").resolve())


if __name__ == "__main__":
    unittest.main()
