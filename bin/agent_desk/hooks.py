"""Claude Code hook entry points: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path

from .core import (
    DeskError, OPEN_STATUSES, PYTHON_ALIAS_MESSAGE, SCHEMA_VERSION, desk_root, python_alias, read_text, schema_version,
)
from .frontmatter import parse_frontmatter
from .gitinfo import RepoInfo, git, is_under, native_path, normalize_path, repo_info
from .model import (
    DECISION_LINE, Project, Task, find_task_by_branch, get_session_task, get_task, head_branch, load_tasks,
    match_project, projects_dir, safe_name, session_dir, session_tasks, strip_comments, stray_files, task_list_field,
)
from .knowledge import glob_prefix, glob_regex, inject_order, kn_dir, load_entries, repo_relative
from .doctor import DOCTOR_MARKER, DOCTOR_REMIND_DAYS


def decision_titles(task: Task) -> list[str]:
    path = task.dir / "decisions.md"
    if not path.exists():
        return []
    return [m.group(1) for m in DECISION_LINE.finditer(strip_comments(read_text(path)))]


RULES_LINE = "태스크 생성 기준과 기록 규칙은 agent-desk:desk 스킬, knowledge 규칙은 agent-desk:kn 스킬을 따른다."


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
    """The first-line prefix and the CLI and knowledge lines shared by both kinds of context."""
    kn_line = knowledge_line(root, project)
    if migrate_line := schema_line(root):
        kn_line = f"{kn_line}\n{migrate_line}" if kn_line else migrate_line
    return (f"[agent-desk] 프로젝트 {project.key} · 브랜치 {branch or '(detached)'}",
            ([kn_line] if kn_line else []))


def candidates_context(root: Path, project: Project, branch: str | None, current: Task | None, agent: bool) -> str:
    head, common = context_head(root, project, branch)
    now = f"{current.id} ({current.meta.get('title', '')})" if current else "없음"
    # an unreadable task may be this branch's: say so before anything suggests a new task
    lines = [f"{head} · 포커스 {now}"] + broken_task_lines(project) + common
    open_tasks = [t for t in load_tasks(project) if t.status in OPEN_STATUSES]
    branch_task = find_task_by_branch(project, branch) if branch else None
    if branch_task and current and branch_task.id == current.id:
        branch_task = None
    if branch_task:
        lines.append(f"이 브랜치의 태스크: {open_task_line(branch_task)[2:]}")
        others = [t for t in open_tasks if t.id != branch_task.id and not (current and t.id == current.id)]
        if others:
            lines.append(f"다른 열린 태스크 {len(others)}건: 필요할 때 `adesk list` 로 확인할 것.")
    elif open_tasks and not current:
        lines += ["", "--- 열린 태스크 ---"] + [open_task_line(t) for t in open_tasks] + [""]
    if agent:
        lines.append("서브에이전트는 이 세션의 포커스를 바꾸지 않는다(`adesk focus` 금지). 기록은 지시받은 태스크 ID로 한다.")
    elif current and branch_task:
        lines.append(f"사용자의 일이 이 브랜치의 태스크로 바뀌었으면 `adesk focus {branch_task.id}` 로 포커스를 옮길 것.")
    elif not current and (branch_task or open_tasks):
        pick = ("사용자의 요청이 이 브랜치의 일이면 묻지 않고 `adesk focus <id>` 로 포커스하고, 다른 태스크를 가리키거나 애매하면 묻는다."
                if branch_task else "사용자의 요청이 이 중 하나를 분명히 가리키면 `adesk focus <id>` 로 포커스하고, 아니면 묻는다.")
        lines.append(f"포커스는 사용자의 요청으로 정한다. {pick} 태스크와 무관한 세션이면 포커스하지 않고, 포커스 전에는 기록하지 않는다.")
    return "\n".join(lines + [RULES_LINE])


def task_context(root: Path, project: Project, task: Task, branch: str | None, compact: bool = False,
                 chosen: bool = False) -> str:
    """The task's records: what SessionStart injects for the session's focus and `adesk focus`
    prints (chosen: the session already has the CLI and knowledge lines, and the user chose it)."""
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
        other = find_task_by_branch(project, branch)
        if other and other.id != task.id and other.id != (task.meta.get("parent") or ""):
            parts.append(f"이 브랜치의 태스크는 {other.id}({other.meta.get('title', '')})로, 포커스와 다르다. "
                         f"사용자의 일이 그 태스크면 `adesk focus {other.id}` 로 포커스를 옮길 것.")
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
    parts += ["", "위 기록은 확인할 출발점이다. 코드와 다를 수 있으니 의존하기 전에 검증한다. 결정을 확정하거나 단계를 끝내면 바로 decisions.md·state.md·log.md에 기록한다. 기록 규칙은 agent-desk:desk 스킬, knowledge 규칙은 agent-desk:kn 스킬을 따른다."]
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


def knowledge_line(root: Path, project: Project) -> str | None:
    pn = sum(1 for e in load_entries(root, project) if e.live)
    gn = sum(1 for e in load_entries(root, None) if e.live)
    if not pn and not gn:
        return None
    line = (f"knowledge: 프로젝트 {pn}건 · 전역 {gn}건. 시스템을 조사하거나 수정하기 전에 "
            "`adesk kn search <검색어...>` 또는 `adesk kn search --path <파일>` 로 관련 항목을 확인할 것.")
    if pn:
        marker = project.dir / DOCTOR_MARKER
        age = (dt.datetime.now().timestamp() - marker.stat().st_mtime) / 86400 if marker.exists() else None
        if age is None or age >= DOCTOR_REMIND_DAYS:
            since = f"{int(age)}일째" if age is not None else "한 번도"
            line += (f"\ndoctor를 {since} 실행하지 않았다. 사용자에게 `adesk doctor` 실행을 한 줄로 제안할 것"
                     "(낡은 knowledge 점검).")
    return line


# Per-session cache for the hooks (session_dir in model): which knowledge entries were already
# injected, which project each repository maps to (avoids git calls per tool use), the location
# and the session's task last announced to the main agent.
SESSION_TTL_DAYS = 7
KN_INJECT_LIMIT = 15  # titles only, so this is about the size of five entries with summaries


def session_file(session_id: str | None) -> Path:
    return session_dir() / (safe_name(session_id) + ".json")


def shown_dir(session_id: str | None) -> Path:
    """Entries already injected: one empty file per entry under shown/<agent>/.

    Hooks of parallel tool calls run concurrently, so `shown` is not kept in the session JSON
    (a load-modify-save there loses what another hook saved in between). Creating a file with
    O_EXCL is atomic: the hook that creates it injects the entry, any other skips it."""
    return session_dir() / safe_name(session_id) / "shown"


def claim_shown(base: Path, entry_id: str) -> bool:
    """True when this hook is the first to show the entry to the agent, recording it."""
    try:
        os.close(os.open(base / entry_id, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False
    except OSError:  # cannot record: inject anyway, the worst case is a repeat
        return True


def reset_shown(session_id: str | None) -> None:
    """After compact or clear the injected entries may be gone from context: allow them again.
    Renamed first, so a concurrent hook sees either the old record or none, never half of it."""
    base = shown_dir(session_id)
    old = base.with_name(f"shown.{os.getpid()}.old")
    try:
        base.rename(old)
    except OSError:
        return
    import shutil
    shutil.rmtree(old, ignore_errors=True)


def load_session(session_id: str | None) -> dict:
    try:
        return json.loads(session_file(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_session(session_id: str | None, data: dict) -> None:
    # write then rename, so a concurrent hook never reads a half-written file. The JSON holds
    # only the repos cache (a lost update costs one git call) and the location, which only
    # SessionStart and UserPromptSubmit write, and those do not run alongside tool hooks.
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
    for p in base.iterdir():  # session caches and shown dirs, kn indexes, leftover .tmp files
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


KN_HOOK_INDEX_FORMAT = 3  # bump when the cached entry fields change


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
                "location": e.meta.get("location", "inline"),
                "stale_reason": e.meta.get("stale_reason") or ""}
               for e in load_entries(root, project) if e.live and e.items("paths")]
    entries.sort(key=lambda e: inject_order(e["kind"], e["id"]))
    try:  # write then rename, so a concurrent session never reads a half-written file
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"sig": sig, "entries": entries}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache)
    except OSError:
        pass
    return entries


def desk_file_check(root: Path, path: Path) -> str | None:
    """For an edit of a record file in the data root: "" when its frontmatter reads fine, the
    error to inject otherwise. None for any other file."""
    is_record = path.name == "state.md" or (path.parent.name == "knowledge" and path.suffix == ".md"
                                            and path.name != "index.md")
    if not is_record or not is_under(root, path):
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
    root = desk_root()
    if not (root / "projects").is_dir():
        return None
    path = tool_file_path(payload)
    if not path:
        return None
    # PreToolUse denies these edits; this check still catches one made while that hook failed
    if payload.get("tool_name") in ("Edit", "Write", "MultiEdit"):
        checked = desk_file_check(root, path)
        if checked is not None:
            return checked or None
    top = find_toplevel(path.parent)
    if not top:
        return None
    sid = payload.get("session_id")
    session = load_session(sid)
    if not isinstance(session.get("repos"), dict):
        session["repos"] = {}
    before = dict(session["repos"])
    project = project_for_toplevel(root, top, session["repos"])
    if session["repos"] != before:
        save_session(sid, session)
    if not project:
        return None
    rel = repo_relative(str(path), top)
    rel_cf = rel.casefold()
    matched = [e for e in kn_hook_index(root, project)
               if any(rel_cf.startswith(pre) and glob_regex(g).match(rel) for g, pre in e["paths"])]
    if not matched:
        return None
    # subagent tool calls share the session_id but have their own context: track per agent
    base = shown_dir(sid) / safe_name(payload.get("agent_id") or "main")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    picked, rest = [], 0
    for e in matched:
        if len(picked) < KN_INJECT_LIMIT:
            if claim_shown(base, e["id"]):
                picked.append(e)
        elif not (base / e["id"]).exists():
            rest += 1
    if not picked:
        return None
    lines = [f"[agent-desk knowledge] {rel} 관련 항목:"]
    for e in picked:
        flags = [e["kind"]] + (["재확인 필요"] if e["status"] == "stale" else [])
        line = f"- {e['id']} ({', '.join(f for f in flags if f)}) {e['title']}"
        if e["location"] and e["location"] != "inline":
            line += f" · 본문: {e['location']}"
        if e["status"] == "stale" and e["stale_reason"]:
            line += f" · 재확인 사유: {e['stale_reason']}"
        lines.append(line)
    if rest:
        lines.append(f"- 외 {rest}건: `kn search --path {rel}`")
    lines.append(f"항목 파일: {kn_dir(root, project).as_posix()}/<ID>-*.md. "
                 "태스크에 영향을 주는 항목은 본문을 읽고 반영할 것 (agent-desk:kn).")
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
            if payload.get("source") in ("compact", "clear"):
                reset_shown(sid)
            elif payload.get("source") == "startup":
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
