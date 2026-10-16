"""Claude Code hook entry points: SessionStart, UserPromptSubmit, PostToolUse."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path

from .core import DeskError, OPEN_STATUSES, PLUGIN_ROOT, desk_root, read_text
from .gitinfo import RepoInfo, git, normalize_path, repo_info
from .model import (
    Project, Task, get_focus_task, get_task, head_branch, load_tasks, match_project, projects_dir,
    resolve_session_task, stray_files, task_list_field,
)
from .knowledge import glob_prefix, glob_regex, kn_dir, load_entries, repo_relative
from .doctor import DOCTOR_MARKER, DOCTOR_REMIND_DAYS


def decision_titles(task: Task) -> list[str]:
    path = task.dir / "decisions.md"
    if not path.exists():
        return []
    text = re.sub(r"<!--.*?-->", "", read_text(path), flags=re.S)
    return [m.group(1) for m in re.finditer(r"^## (D\d+\..*)$", text, re.M)]


def session_start_context(payload: dict) -> str | None:
    root = desk_root()
    if not (root / "projects").is_dir():
        return None
    cwd = Path(payload.get("cwd") or os.getcwd())
    info = repo_info(cwd)
    project = match_project(root, info)
    if not project:
        return None
    cli = f'python "{(PLUGIN_ROOT / "bin" / "desk.py").as_posix()}"'
    branch = info.branch
    head = f"[agent-desk] 프로젝트 {project.key} · 브랜치 {branch or '(detached)'}"
    kn_line = knowledge_line(root, project)
    task, source = resolve_session_task(project, branch)
    if not task:
        lines = [f"{head} · 연결된 작업 없음.", f"CLI(아래에서 `desk`로 표기): {cli}"] + ([kn_line] if kn_line else [])
        branchless = [t for t in load_tasks(project) if t.status in OPEN_STATUSES and not t.branches]
        if branchless:
            listed = ", ".join(f"{t.id}({t.meta.get('title', '')})" for t in branchless[:5])
            more = f" 외 {len(branchless) - 5}건" if len(branchless) > 5 else ""
            lines.append(f"진행 중인 브랜치 없는 작업: {listed}{more}. 이어서 할 작업이면 사용자에게 확인하고 "
                         "`desk focus <id>` 로 포커스를 지정할 것.")
        return "\n".join(lines + ["작업 생성 기준과 기록 규칙은 agent-desk:desk 스킬, knowledge 규칙은 agent-desk:kn 스킬을 따른다."])
    design_doc = task.meta.get("design_doc")
    label = "" if source == "branch" else " · 포커스"
    parts = [f"{head} · 작업 {task.id} ({task.status}{label})",
             f"작업 폴더: {task.dir.as_posix()}"] + ([f"설계 문서: {design_doc}"] if design_doc else []) + [
             f"CLI(아래에서 `desk`로 표기): {cli}"] + ([kn_line] if kn_line else [])
    if source == "focus" and branch and not project.is_shared(branch):
        parts.append(f"이 브랜치({branch})에는 연결된 작업이 없어 포커스 작업을 표시한다. 이 브랜치의 일과 관계없을 수 있으니 "
                     "관련이 없으면 사용자에게 확인하고 새 작업을 만들거나 `link` 로 연결할 것.")
    if source == "branch":
        focus = get_focus_task(project)
        if focus and focus.id != task.id and focus.id != (task.meta.get("parent") or ""):
            parts.append(f"포커스 작업은 {focus.id}({focus.meta.get('title', '')})로, 이 브랜치 작업과 별개다. "
                         "포커스가 낡았다면 사용자에게 확인하고 갱신할 것.")
    parts += [
             "",
             "--- state.md ---",
             read_text(task.state_path).rstrip()]
    titles = decision_titles(task)
    parts += ["", "--- 결정 목록 (본문은 decisions.md) ---"] + ([f"- {t}" for t in titles] or ["(없음)"])
    parts += parent_context(project, task)
    related = task_list_field(task, "related")
    if related:
        parts += ["", f"연관 작업: {', '.join(related)} (필요할 때 `desk list --all` 로 위치를 찾아 읽을 것)"]
    strays = stray_files(task)
    if strays:
        parts += ["", f"경고: 작업 폴더에 예상하지 않은 파일이 있습니다(동기화 충돌 사본일 수 있음): {', '.join(strays)}. 사용자에게 알리고 병합 여부를 확인할 것."]
    if payload.get("source") == "compact":
        parts += ["", "컨텍스트가 방금 압축되었다. 압축 전 진행 내용과 결정이 state.md·decisions.md·log.md에 반영되어 있는지 확인하고, 빠진 내용이 있으면 지금 기록할 것."]
    parts += ["", "위 기록은 확인할 출발점이다. 코드와 다를 수 있으니 의존하기 전에 검증하고, 기록 규칙은 agent-desk:desk 스킬, knowledge 규칙은 agent-desk:kn 스킬을 따른다."]
    return "\n".join(parts)


def parent_context(project: Project, task: Task) -> list[str]:
    parent_id = task.meta.get("parent")
    if not parent_id:
        return []
    try:
        parent = get_task(project, parent_id)
    except DeskError:
        return ["", f"상위 작업 {parent_id} 를 찾을 수 없음."]
    titles = decision_titles(parent)
    return (["", f"--- 상위 작업 {parent.id} state.md (폴더: {parent.dir.as_posix()}) ---",
             read_text(parent.state_path).rstrip(),
             "", "--- 상위 작업 결정 목록 (본문은 상위 작업의 decisions.md) ---"] +
            ([f"- {t}" for t in titles] or ["(없음)"]) +
            ["상위 작업은 흐름 전체의 맥락이다. 이 세션의 할 일은 하위 작업 state.md를 따르고, 상위 작업의 유효한 결정을 따른다. "
             "어긋나는 결정이 필요하면 사용자에게 확인하고 상위 작업의 decisions.md에 기록할 것."])


def knowledge_line(root: Path, project: Project) -> str | None:
    pn = sum(1 for e in load_entries(root, project) if e.status != "superseded")
    gn = sum(1 for e in load_entries(root, None) if e.status != "superseded")
    if not pn and not gn:
        return None
    line = (f"knowledge: 프로젝트 {pn}건 · 전역 {gn}건. 시스템을 조사하거나 수정하기 전에 "
            "`desk kn search <검색어>` 또는 `desk kn search --path <파일>` 로 관련 항목을 확인할 것.")
    if pn:
        marker = project.dir / DOCTOR_MARKER
        age = (dt.datetime.now().timestamp() - marker.stat().st_mtime) / 86400 if marker.exists() else None
        if age is None or age >= DOCTOR_REMIND_DAYS:
            since = f"{int(age)}일째" if age is not None else "한 번도"
            line += (f"\ndoctor를 {since} 실행하지 않았다. 사용자에게 `desk doctor` 실행을 한 줄로 제안할 것"
                     "(낡은 knowledge 점검).")
    return line


# Per-session cache for the PostToolUse hook: which knowledge entries were already
# injected, and which project each repository maps to (avoids git calls per tool use).
def session_dir() -> Path:
    # same lookup order as tempfile.gettempdir(), without importing it (pulls in shutil, random)
    tmp = os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP")
    if not tmp:
        import tempfile
        tmp = tempfile.gettempdir()
    return Path(tmp) / "agent-desk-sessions"


SESSION_TTL_DAYS = 7
KN_INJECT_LIMIT = 5


def session_file(session_id: str | None) -> Path:
    return session_dir() / (re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown") + ".json")


def load_session(session_id: str | None) -> dict:
    try:
        return json.loads(session_file(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_session(session_id: str | None, data: dict) -> None:
    try:
        session_dir().mkdir(parents=True, exist_ok=True)
        session_file(session_id).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def prune_sessions() -> None:
    base = session_dir()
    if not base.is_dir():
        return
    cutoff = dt.datetime.now().timestamp() - SESSION_TTL_DAYS * 86400
    for p in base.iterdir():  # session caches, kn indexes, leftover .tmp files
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def find_toplevel(start: Path) -> Path | None:
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def projects_signature(root: Path) -> str:
    """Hash of (name, mtime) of every project.toml; changes when a project is registered or edited."""
    sig = []
    for p in projects_dir(root).glob("*/project.toml"):
        try:
            sig.append((p.parent.name, p.stat().st_mtime_ns))
        except OSError:
            pass
    return hashlib.sha1(repr(sorted(sig)).encode("utf-8")).hexdigest()[:16]


def project_for_toplevel(root: Path, top: Path, cache: dict) -> Project | None:
    # drop entries from the pre-0.2.2 format (bare project key or null)
    for k in [k for k, v in cache.items() if not isinstance(v, dict)]:
        del cache[k]
    # keyed by data root too: the same session may run under a different AGENT_DESK_ROOT
    key = f"{normalize_path(root)} | {normalize_path(top)}"
    sig = projects_signature(root)
    entry = cache.get(key)
    if not isinstance(entry, dict) or entry.get("sig") != sig:
        remotes = [u for name in (git(top, "remote") or "").split() if (u := git(top, "remote", "get-url", name))]
        project = match_project(root, RepoInfo(top, None, remotes))
        entry = cache[key] = {"project": project.key if project else None, "sig": sig}
    if not entry["project"]:
        return None
    pdir = projects_dir(root) / entry["project"]
    try:
        return Project(pdir.name, pdir, tomllib.loads(read_text(pdir / "project.toml")))
    except (OSError, tomllib.TOMLDecodeError):
        return None


KN_HOOK_INDEX_FORMAT = 2  # bump when the cached entry fields change


def kn_hook_index(root: Path, project: Project) -> list[dict]:
    """The fields the PostToolUse hook needs from the project's injectable entries.

    Cached in the temp dir and rebuilt when an entry file is added, removed or modified,
    so a tool call does not parse every entry file."""
    base = kn_dir(root, project)
    files = []
    try:
        with os.scandir(base) as it:
            for d in it:
                if d.name.endswith(".md") and d.name != "index.md":
                    st = d.stat()
                    files.append((d.name, st.st_mtime_ns, st.st_size))
    except OSError:
        return []
    if not files:
        return []
    sig = hashlib.sha1(repr((KN_HOOK_INDEX_FORMAT, sorted(files))).encode("utf-8")).hexdigest()[:16]
    cache = session_dir() / f"kn-{hashlib.sha1(normalize_path(base).encode('utf-8')).hexdigest()[:16]}.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("sig") == sig:
            return data["entries"]
    except (OSError, ValueError, AttributeError):
        pass
    entries = [{"id": e.id, "kind": e.meta.get("kind", ""), "title": e.meta.get("title", ""),
                "status": e.status, "paths": [[g, glob_prefix(g)] for g in e.items("paths")],
                "summary": e.summary(),
                "location": e.meta.get("location", "inline"),
                "stale_reason": e.meta.get("stale_reason") or ""}
               for e in load_entries(root, project) if e.status != "superseded" and e.items("paths")]
    try:  # write then rename, so a concurrent session never reads a half-written file
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"sig": sig, "entries": entries}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache)
    except OSError:
        pass
    return entries


def post_tool_use_context(payload: dict) -> str | None:
    root = desk_root()
    if not (root / "projects").is_dir():
        return None
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path:
        return None
    path = Path(file_path)
    if not path.is_absolute():
        path = Path(payload.get("cwd") or os.getcwd()) / path
    top = find_toplevel(path.parent)
    if not top:
        return None
    sid = payload.get("session_id")
    session = load_session(sid)
    project = project_for_toplevel(root, top, session.setdefault("repos", {}))
    if not project:
        save_session(sid, session)
        return None
    rel = repo_relative(str(path), top)
    # subagent tool calls share the session_id but have their own context: track per agent
    agent = payload.get("agent_id") or "main"
    shown_by_agent = session.get("shown")
    if not isinstance(shown_by_agent, dict):  # pre-0.2.3 format: list for the main agent
        shown_by_agent = {"main": shown_by_agent or []}
    shown = set(shown_by_agent.get(agent, []))
    rel_cf = rel.casefold()
    matched = [e for e in kn_hook_index(root, project) if e["id"] not in shown
               and any(rel_cf.startswith(pre) and glob_regex(g).match(rel) for g, pre in e["paths"])]
    if not matched:
        save_session(sid, session)
        return None
    picked, rest = matched[:KN_INJECT_LIMIT], matched[KN_INJECT_LIMIT:]
    lines = [f"[agent-desk knowledge] {rel} 관련 항목:"]
    for e in picked:
        flags = [e["kind"]] + (["재확인 필요"] if e["status"] == "stale" else [])
        line = f"- {e['id']} ({', '.join(f for f in flags if f)}) {e['title']}"
        if e["summary"]:
            line += f" · {e['summary']}"
        if e["location"] and e["location"] != "inline":
            line += f" · 본문: {e['location']}"
        if e["status"] == "stale" and e["stale_reason"]:
            line += f" · 재확인 사유: {e['stale_reason']}"
        lines.append(line)
    if rest:
        lines.append(f"- 외 {len(rest)}건: `kn search --path {rel}`")
    lines.append(f"항목 파일: {kn_dir(root, project).as_posix()}/<ID>-*.md. "
                 "작업에 영향을 주는 항목은 본문을 읽고 반영할 것 (agent-desk:kn).")
    shown_by_agent[agent] = sorted(shown | {e["id"] for e in picked})
    session["shown"] = shown_by_agent
    save_session(sid, session)
    return "\n".join(lines)


def current_location(payload: dict) -> list | None:
    """[repository toplevel, branch] of the hook's cwd, without running git."""
    top = find_toplevel(Path(payload.get("cwd") or os.getcwd()))
    return [normalize_path(top), head_branch(top)] if top else None


def location_change_context(payload: dict, tool_switch: bool) -> str | None:
    """Task context for a new location, when the repository or branch differs from the one
    last announced to the main agent. SessionStart does not fire again on a mid-session
    switch: tool_switch is EnterWorktree/ExitWorktree (PostToolUse, cwd already moved),
    otherwise a user prompt (catches a checkout done outside the session)."""
    sid = payload.get("session_id")
    session = load_session(sid)
    loc = current_location(payload)
    if payload.get("agent_id"):
        # a subagent switching worktrees: tell that subagent, keep the main agent's record
        prev = None
    else:
        prev = session.get("location")
        if loc == prev or (prev is None and not tool_switch):
            if prev is None:
                session["location"] = loc  # first prompt of a session that predates this record
                save_session(sid, session)
            return None
        session["location"] = loc
        save_session(sid, session)
    where = lambda l: f"{Path(l[0]).name} · {l[1] or '(detached)'}" if l else "저장소 밖"
    change = f"{where(prev)} → {where(loc)}" if prev else where(loc)
    lines = [f"[agent-desk] 작업 위치가 바뀌었다 ({change}). 이전에 주입된 작업 기록은 더 이상 현재 작업이 아니다."]
    context = session_start_context(payload)
    lines.append(context if context else "이 위치에는 등록된 프로젝트의 작업이 없다.")
    return "\n".join(lines)


def cmd_hook(event: str) -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if event == "session-start":
            sid = payload.get("session_id")
            if payload.get("source") in ("compact", "clear"):
                # injected knowledge may be gone from context; allow re-injection
                session = load_session(sid)
                session.pop("shown", None)
                save_session(sid, session)
            elif payload.get("source") == "startup":
                prune_sessions()
            context, event = session_start_context(payload), "SessionStart"
            if not payload.get("agent_id"):
                session = load_session(sid)
                session["location"] = current_location(payload)
                save_session(sid, session)
        elif event == "user-prompt-submit":
            context, event = location_change_context(payload, tool_switch=False), "UserPromptSubmit"
        elif payload.get("tool_name") in ("EnterWorktree", "ExitWorktree"):
            context, event = location_change_context(payload, tool_switch=True), "PostToolUse"
        else:
            context, event = post_tool_use_context(payload), "PostToolUse"
        if context:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": event, "additionalContext": context}}))
    except Exception as e:  # a hook must never break the session
        print(f"agent-desk hook error: {e}", file=sys.stderr)
