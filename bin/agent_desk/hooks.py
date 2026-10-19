"""Claude Code hook entry points: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

from .core import (
    DeskError, OPEN_STATUSES, PYTHON_ALIAS_MESSAGE, SCHEMA_VERSION, desk_root, python_alias, read_text, schema_version,
)
from .frontmatter import parse_frontmatter
from .gitinfo import is_under, native_path, normalize_path, repo_info
from .model import (
    DECISION_LINE, Project, Task, get_session_task, get_task, head_branch, load_tasks,
    match_project, safe_name, session_dir, session_tasks, strip_comments, stray_files, task_list_field, tasks_on_branch,
)


def decision_titles(task: Task) -> list[str]:
    path = task.dir / "decisions.md"
    if not path.exists():
        return []
    return [m.group(1) for m in DECISION_LINE.finditer(strip_comments(read_text(path)))]


RULES_LINE = "태스크 생성·기록·과거 결정 조회는 agent-desk:desk 스킬을 따른다."


def session_start_context(payload: dict, location_change: bool = False) -> str | None:
    """The task chosen in this session (after compact or resume), otherwise the candidates to
    choose from. A subagent shares the session id but does not work on the session's choice."""
    root = desk_root()
    if not (root / "projects").is_dir():
        return None
    info = repo_info(Path(payload.get("cwd") or os.getcwd()))
    project = match_project(root, info)
    if not project:
        return None
    agent = bool(payload.get("agent_id"))
    current = None if agent else get_session_task(project, payload.get("session_id"))
    if current and not location_change:
        return task_context(root, project, current, info.branch, compact=payload.get("source") == "compact")
    return candidates_context(root, project, info.branch, current, agent)


def context_head(root: Path, project: Project, branch: str | None) -> tuple[str, list[str]]:
    """The first-line prefix and the lines shared by both kinds of context."""
    migrate_line = schema_line(root)
    return (f"[agent-desk] 프로젝트 {project.key} · 브랜치 {branch or '(detached)'}",
            ([migrate_line] if migrate_line else []))


def candidates_context(root: Path, project: Project, branch: str | None, current: Task | None, agent: bool) -> str:
    head, common = context_head(root, project, branch)
    now = f"{current.id} ({current.meta.get('title', '')})" if current else "없음"
    # an unreadable task may be this branch's: say so before anything suggests a new task
    lines = [f"{head} · 포커스 {now}"] + broken_task_lines(project) + common
    open_tasks = [t for t in load_tasks(project) if t.status in OPEN_STATUSES]
    branch_tasks = [t for t in tasks_on_branch(project, branch) if not (current and t.id == current.id)] if branch else []
    if len(branch_tasks) == 1:
        lines.append(f"이 브랜치의 태스크: {open_task_line(branch_tasks[0])[2:]}")
    elif branch_tasks:
        lines += ["", "--- 이 브랜치의 태스크 ---"] + [open_task_line(t) for t in branch_tasks] + [""]
    if branch_tasks:
        shown = {t.id for t in branch_tasks} | ({current.id} if current else set())
        others = [t for t in open_tasks if t.id not in shown]
        if others:
            lines.append(f"다른 열린 태스크 {len(others)}건: 필요할 때 `adesk list` 로 확인할 것.")
    elif open_tasks and not current:
        lines += ["", "--- 열린 태스크 ---"] + [open_task_line(t) for t in open_tasks] + [""]
    if agent:
        lines.append("서브에이전트는 이 세션의 포커스를 바꾸지 않는다(`adesk focus` 금지). 기록은 지시받은 태스크 ID로 한다.")
    elif current and branch_tasks:
        target = branch_tasks[0].id if len(branch_tasks) == 1 else "<id>"
        lines.append(f"사용자의 일이 이 브랜치의 태스크로 바뀌었으면 `adesk focus {target}` 로 포커스를 옮길 것.")
    elif not current and (branch_tasks or open_tasks):
        if len(branch_tasks) == 1:
            pick = "사용자의 요청이 이 브랜치의 일이면 묻지 않고 `adesk focus <id>` 로 포커스하고, 다른 태스크를 가리키거나 애매하면 묻는다."
        elif branch_tasks:
            pick = "사용자의 요청에 맞는 이 브랜치의 태스크가 분명하면 `adesk focus <id>` 로 포커스하고, 애매하면 묻는다."
        else:
            pick = "사용자의 요청이 이 중 하나를 분명히 가리키면 `adesk focus <id>` 로 포커스하고, 아니면 묻는다."
        lines.append(f"포커스는 사용자의 요청으로 정한다. {pick} 태스크와 무관한 세션이면 포커스하지 않고, 포커스 전에는 기록하지 않는다.")
    return "\n".join(lines + [RULES_LINE])


def task_context(root: Path, project: Project, task: Task, branch: str | None, compact: bool = False,
                 chosen: bool = False) -> str:
    """The task's records: what SessionStart injects for the session's focus and `adesk focus`
    prints (chosen: the session already has the shared lines, and the user chose it)."""
    head, common = context_head(root, project, branch)
    design_doc = task.meta.get("design_doc")
    parts = [f"{head} · 포커스 {task.id} ({task.status})",
             f"태스크 폴더: {task.dir.as_posix()}"] + ([f"설계 문서: {design_doc}"] if design_doc else []) + (
             [] if chosen else common)
    others = [t for t in load_tasks(project) if t.status in OPEN_STATUSES and t.id != task.id]
    if others:
        children = sum(1 for t in others if t.meta.get("parent") == task.id)
        sub = f"(하위 {children}건)" if children else ""
        parts.append(f"다른 열린 태스크 {len(others)}건{sub}: 필요할 때 `adesk list` 로 확인할 것.")
    if task.status == "todo":
        parts.append("이 태스크는 착수 전(todo)이다. 작업을 시작하면 `adesk status <id> in_progress` 로 바꿀 것.")
    if not chosen and branch:
        on_branch = tasks_on_branch(project, branch)
        if task.id not in (t.id for t in on_branch):
            others = [t for t in on_branch if t.id != (task.meta.get("parent") or "")]
            if others:
                names = ", ".join(f"{t.id}({t.meta.get('title', '')})" for t in others)
                target = others[0].id if len(others) == 1 else "<id>"
                parts.append(f"이 브랜치의 태스크는 {names}로, 포커스와 다르다. "
                             f"사용자의 일이 {'그 태스크' if len(others) == 1 else '그중 하나'}면 `adesk focus {target}` 로 포커스를 옮길 것.")
    parts += [
             "",
             "--- state.md ---",
             read_text(task.state_path).rstrip()]
    titles = decision_titles(task)
    parts += ["", "--- 결정 목록 (본문은 decisions.md) ---"] + ([f"- {t}" for t in titles] or ["(없음)"])
    parts += parent_context(project, task)
    related = task_list_field(task, "related")
    if related:
        parts += ["", f"연관 태스크: {', '.join(related)} (필요할 때 `adesk list --all` 로 위치를 찾아 읽을 것)"]
    strays = stray_files(task)
    if strays:
        parts += ["", f"경고: 태스크 폴더에 예상하지 않은 파일이 있습니다(동기화 충돌 사본일 수 있음): {', '.join(strays)}. 사용자에게 알리고 병합 여부를 확인할 것."]
    broken = broken_task_lines(project)
    if broken:
        parts += [""] + broken
    if compact:
        parts += ["", "컨텍스트가 방금 압축되었다. 압축 전 진행 내용과 결정이 state.md·decisions.md·log.md에 반영되어 있는지 확인하고, 빠진 내용이 있으면 지금 기록할 것."]
    parts += ["", "위 기록은 확인할 출발점이다. 코드와 다를 수 있으니 의존하기 전에 검증한다. 결정을 확정하거나 단계를 끝내면 바로 decisions.md·state.md·log.md에 기록한다. 기록 규칙은 agent-desk:desk 스킬을 따른다."]
    return "\n".join(parts)


def schema_line(root: Path) -> str | None:
    try:
        version = schema_version(root)
    except (OSError, ValueError):
        return None
    if isinstance(version, int) and version < SCHEMA_VERSION:
        return ("데이터가 이전 형식이라 기록을 바꾸는 CLI 명령이 거부된다. 사용자에게 `adesk migrate` 실행을 한 줄로 제안할 것"
                "(`--dry-run` 으로 바뀔 내용을 먼저 보여 줄 수 있음).")
    return None


def open_task_line(task: Task) -> str:
    where = f"브랜치 {', '.join(task.branches)}" if task.branches else "브랜치 없음"
    parent = f" · 상위 {task.meta['parent']}" if task.meta.get("parent") else ""
    return f"- {task.id} ({task.meta.get('title', '')}) · {task.status} · {where}{parent}"


def broken_task_lines(project: Project) -> list[str]:
    broken = [t for t in load_tasks(project) if t.status == "?"]
    return [f"경고: state.md frontmatter를 읽을 수 없어 태스크 목록에서 빠진 태스크가 있다: {t.id} ({t.meta.get('error', '')}). "
            "사용자에게 알리고 고칠 것." for t in broken]


def parent_context(project: Project, task: Task) -> list[str]:
    parent_id = task.meta.get("parent")
    if not parent_id:
        return []
    try:
        parent = get_task(project, parent_id)
    except DeskError:
        return ["", f"상위 태스크 {parent_id} 를 찾을 수 없음."]
    titles = decision_titles(parent)
    return (["", f"--- 상위 태스크 {parent.id} state.md (폴더: {parent.dir.as_posix()}) ---",
             read_text(parent.state_path).rstrip(),
             "", "--- 상위 태스크 결정 목록 (본문은 상위 태스크의 decisions.md) ---"] +
            ([f"- {t}" for t in titles] or ["(없음)"]) +
            ["상위 태스크는 흐름 전체의 맥락이다. 이 세션의 할 일은 하위 태스크 state.md를 따르고, 상위 태스크의 유효한 결정을 따른다. "
             "어긋나는 결정이 필요하면 사용자에게 확인하고 상위 태스크의 decisions.md에 기록할 것."])


# Per-session cache for the hooks (session_dir in model): the location and the session's task
# last announced to the main agent.
SESSION_TTL_DAYS = 7


def session_file(session_id: str | None) -> Path:
    return session_dir() / (safe_name(session_id) + ".json")


def load_session(session_id: str | None) -> dict:
    try:
        return json.loads(session_file(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_session(session_id: str | None, data: dict) -> None:
    # write then rename, so a concurrent hook never reads a half-written file. The JSON holds
    # the location and the announced task, which SessionStart, UserPromptSubmit and a worktree
    # switch write, and those do not run alongside each other.
    try:
        session_dir().mkdir(parents=True, exist_ok=True)
        path = session_file(session_id)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for _ in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: another hook has the file open for a moment
                import time
                time.sleep(0.005)
        tmp.unlink()
    except OSError:
        pass


def prune_sessions() -> None:
    base = session_dir()
    if not base.is_dir():
        return
    cutoff = dt.datetime.now().timestamp() - SESSION_TTL_DAYS * 86400
    for p in base.iterdir():  # session caches, what older releases left, leftover .tmp files
        try:
            if p.is_dir():
                if max(Path(d).stat().st_mtime for d, _, _ in os.walk(p)) < cutoff:
                    import shutil
                    shutil.rmtree(p, ignore_errors=True)
            elif p.stat().st_mtime < cutoff:
                p.unlink()
        except (OSError, ValueError):
            pass


def find_toplevel(start: Path) -> Path | None:
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def desk_file_check(root: Path, path: Path) -> str | None:
    """For an edit of a state.md in the data root: "" when its frontmatter reads fine, the
    error to inject otherwise. None for any other file."""
    if path.name != "state.md" or not is_under(root, path):
        return None
    try:
        parse_frontmatter(read_text(path))
    except (DeskError, OSError) as e:
        return (f"[agent-desk] 방금 수정한 {path.as_posix()} 의 frontmatter를 읽을 수 없다: {e}. "
                "사본을 고쳐 `adesk put` 으로 바로잡을 것(agent-desk:desk 스킬 \"파일별 작성 규칙\").")
    return ""


def tool_file_path(payload: dict) -> Path | None:
    """The absolute file path a file tool call targets, None for a call without one."""
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path or not isinstance(file_path, str):
        return None
    path = Path(native_path(file_path))
    if not path.is_absolute():
        path = Path(payload.get("cwd") or os.getcwd()) / path
    return path


DESK_EDIT_DENIED = ("[agent-desk] 데이터 폴더는 Edit/Write로 고치지 않는다. 파일을 scratchpad에 복사해 고친 뒤 "
                    "`adesk put <경로> --file <사본>`(전체 교체), 덧붙일 때는 `adesk append <경로> --file <파일>`로 반영할 것.")


def pre_tool_use_decision(payload: dict) -> str | None:
    """The reason to deny an Edit/Write/NotebookEdit of a file in the data root, None to allow.

    rewind (Restore code) reverts Edit/Write changes but not the CLI's, so editing the desk with
    the file tools could leave it half reverted. Writes go through `adesk put`/`append` instead."""
    if payload.get("tool_name") not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return None
    path = tool_file_path(payload)
    return DESK_EDIT_DENIED if path and is_under(desk_root(), path) else None


def post_tool_use_context(payload: dict) -> str | None:
    """PreToolUse denies edits of the data root; this still catches one made while that hook failed."""
    root = desk_root()
    if payload.get("tool_name") not in ("Edit", "Write", "MultiEdit") or not (root / "projects").is_dir():
        return None
    path = tool_file_path(payload)
    return (desk_file_check(root, path) or None) if path else None


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
    lines = [f"[agent-desk] 작업 위치가 바뀌었다 ({change})."]
    context = session_start_context(payload, location_change=True)
    lines.append(context if context else "이 위치에는 등록된 프로젝트의 태스크가 없다.")
    return "\n".join(lines)


def task_change_context(payload: dict) -> str | None:
    """A change of the session's task since it was last announced to the main agent. Subagents
    share the session id and are told not to change it; this keeps a change from going unseen.
    The first choice in a session is not announced: the agent made it a moment ago."""
    if payload.get("agent_id"):
        return None
    sid = payload.get("session_id")
    chosen = session_tasks(sid)
    session = load_session(sid)
    announced = session.get("tasks") if isinstance(session.get("tasks"), dict) else {}
    if chosen == announced:
        return None
    changes = [f"{announced[k]} → {chosen.get(k) or '없음'}" for k in sorted(announced)
               if chosen.get(k) != announced[k]]
    session["tasks"] = chosen
    save_session(sid, session)
    if not changes:
        return None
    return (f"[agent-desk] 이 세션의 포커스가 바뀌었다: {', '.join(changes)}. "
            "직접 바꾼 것이 아니면 서브에이전트가 바꾼 것이니 사용자에게 알리고 `adesk focus` 로 바로잡을 것.")


def cmd_hook(name: str) -> None:
    event = name  # becomes the hookEventName of the output
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if name == "session-start":
            sid = payload.get("session_id")
            if payload.get("source") == "startup":
                prune_sessions()
            context, event = session_start_context(payload), "SessionStart"
            if not payload.get("agent_id"):
                session = load_session(sid)
                session["location"] = current_location(payload)
                session["tasks"] = session_tasks(sid)
                save_session(sid, session)
        elif name == "pre-tool-use":
            reason = pre_tool_use_decision(payload)
            if reason:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": reason}}, ensure_ascii=False))
            return
        elif name == "user-prompt-submit":
            parts = [c for c in (task_change_context(payload), location_change_context(payload, tool_switch=False)) if c]
            context, event = "\n\n".join(parts) or None, "UserPromptSubmit"
        elif payload.get("tool_name") in ("EnterWorktree", "ExitWorktree"):
            context, event = location_change_context(payload, tool_switch=True), "PostToolUse"
        else:
            context, event = post_tool_use_context(payload), "PostToolUse"
        output = {}
        if context:
            output["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": context}
        if event == "SessionStart" and payload.get("source") in ("startup", None) and not payload.get("agent_id"):
            alias = python_alias()
            if alias:  # shown to the user, who is the one to change the setting
                output["systemMessage"] = "[agent-desk] " + PYTHON_ALIAS_MESSAGE.format(path=alias)
        if output:
            print(json.dumps(output))
    except Exception as e:  # a hook must never break the session
        from . import hook_failure
        hook_failure(name, e)
