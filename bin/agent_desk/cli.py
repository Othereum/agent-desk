"""CLI commands and the argparse tree."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tomllib
from pathlib import Path

from .core import (
    CLOSED_STATUSES, DEFAULT_ID_TEMPLATE, DEFAULT_SHARED_BRANCHES, DeskError, LEGACY_STATUSES, OPEN_STATUSES,
    PYTHON_ALIAS_MESSAGE, SCHEMA_VERSION, TASK_FILES, desk_root, ensure_root, now_minute, python_alias,
    move_dir, read_text, root_lock, schema_version, today, write_schema_version, write_text,
)
from .frontmatter import format_scalar, format_value, parse_frontmatter, split_frontmatter, update_frontmatter
from .gitinfo import is_under, native_path, normalize_path, repo_info
from .model import (
    TRASH_DIR, TRASH_LOG_LINE, append_log, build_id, check_branch_free, children_of, current_project,
    decision_problems, env_session_id, find_task_by_branch, find_trashed, get_project, get_session_task, get_task,
    load_projects, load_tasks, load_trash, main_worktree, match_project, projects_dir, render_template,
    resolve_parent, sanitize, set_meta, set_session_task, task_dir_of, task_list_field, touch_task, trashed_at,
)
from .index import write_index
from .knowledge import (
    KN_INACTIVE, KN_KINDS, all_entries, entries_for_path, entry_references, find_entry, inject_order, kn_dir, load_entries,
    next_entry_id, repo_relative, search_score, split_csv, tombstone_entry, try_project, update_entry, write_kn_index,
)
from .doctor import DOCTOR_MARKER, doctor_knowledge, doctor_tasks, project_repo
from . import HOOK_EVENTS
from .hooks import cmd_hook, task_context


def out(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_where(args) -> None:
    root = desk_root()
    info = repo_info(Path(args.cwd))
    project = match_project(root, info) if (root / "projects").is_dir() else None
    focus = get_session_task(project, env_session_id()) if project else None
    branch_task = find_task_by_branch(project, info.branch) if project and info.branch else None
    out({
        "root": str(root),
        "repo": str(info.toplevel) if info.toplevel else None,
        "branch": info.branch,
        "project": project.key if project else None,
        "project_dir": str(project.dir) if project else None,
        "session": env_session_id(),
        "focus": focus.id if focus else None,
        "focus_dir": str(focus.dir) if focus else None,
        "branch_task": branch_task.id if branch_task else None,
    })


def cmd_project_init(args) -> None:
    root = ensure_root()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.key):
        raise DeskError("프로젝트 key 는 영문·숫자·_.- 만 사용할 수 있습니다")
    for p in load_projects(root):
        if p.key.casefold() == args.key.casefold():
            raise DeskError(f"이미 등록된 프로젝트입니다: {p.key}")
    info = repo_info(Path(args.cwd))
    if not info.toplevel:
        raise DeskError("git 저장소 안에서 실행해야 합니다")
    remotes = info.remotes[:1] if info.remotes else []
    shared = args.shared.split(",") if args.shared else DEFAULT_SHARED_BRANCHES

    def toml_list(xs):
        return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in xs) + "]"

    text = (f'key = "{args.key}"\n'
            f"remotes = {toml_list(remotes)}\n"
            f"paths = {toml_list([str(main_worktree(info.toplevel)).replace(chr(92), '/')])}\n"
            f'id_template = "{args.id_template}"\n'
            f"shared_branches = {toml_list(shared)}\n"
            f'lang = "{args.lang}"\n')
    pdir = projects_dir(root) / args.key
    write_text(pdir / "project.toml", text)
    (pdir / "tasks").mkdir(exist_ok=True)
    (pdir / "archive").mkdir(exist_ok=True)
    write_index(get_project(root, args.key))
    out({"project": args.key, "dir": str(pdir), "config": text})


def cmd_new(args) -> None:
    root = ensure_root()
    project, info = current_project(root, Path(args.cwd), args.project)
    branch = None
    if not args.no_branch:
        branch = args.branch or info.branch
        if not branch:
            raise DeskError("현재 브랜치를 알 수 없습니다. --branch 또는 --no-branch 를 지정하세요")
        check_branch_free(project, branch)
    parent = resolve_parent(project, args.parent) if args.parent else None
    related = [get_task(project, r).id for r in split_csv(args.related)]
    task_id = build_id(project, branch, args.slug)
    tdir = project.dir / "tasks" / task_id
    values = {
        "id": task_id, "title": format_scalar(args.title), "project": project.key,
        "branches": format_value([branch] if branch else []),
        "parent": parent.id if parent else "", "related": format_value(related),
        "status": "todo" if args.todo else "in_progress",
        "date": today(), "datetime": now_minute(),
    }
    for name in TASK_FILES:
        write_text(tdir / name, render_template(project.lang, name, values))
    # creating a task to work on now makes it this session's focus; a todo task is for later
    sid = env_session_id()
    focused = bool(sid) and not args.todo and not args.no_focus
    if focused:
        set_session_task(sid, project, task_id)
    write_index(project)
    out({"task": task_id, "dir": str(tdir), "branch": branch,
         "parent": parent.id if parent else None, "related": related, "focused": focused})


def cmd_focus(args) -> None:
    """Choose the task this session works on (its focus) and print its records (text, not JSON)."""
    root = ensure_root()
    project, info = current_project(root, Path(args.cwd), args.project)
    if not args.id and not args.clear:
        raise DeskError("포커스할 태스크 ID를 지정하세요 (해제는 --clear)")
    task = None if args.clear else get_task(project, args.id)
    if task and (task.archived or task.status not in OPEN_STATUSES):
        raise DeskError(f"열린 태스크만 포커스할 수 있습니다: {task.id} ({task.status}). 종료된 태스크는 adesk reopen 으로 먼저 재개하세요")
    sid = env_session_id()
    if not sid:
        raise DeskError("Claude Code 세션 밖이라 포커스를 둘 수 없습니다(포커스는 세션마다 따로 있다. CLAUDE_CODE_SESSION_ID 없음)")
    if not task:
        set_session_task(sid, project, None)
        out({"project": project.key, "focus": None})
        return
    set_session_task(sid, project, task.id)
    if task.status == "todo":  # focusing a task is starting it
        set_meta(task, {"status": "in_progress"})
        append_log(task, ["상태 변경: todo → in_progress (포커스)"])
        write_index(project)
    print(task_context(root, project, task, info.branch, chosen=True))


def cmd_relate(args) -> None:
    root = ensure_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    task = get_task(project, args.id)
    updates, logs = {}, []
    if args.parent:
        if children_of(project, task.id):
            raise DeskError(f"{task.id} 에 하위 태스크가 있어 상위 태스크를 지정할 수 없습니다(한 단계만 허용)")
        parent = resolve_parent(project, args.parent, task.id)
        updates["parent"] = parent.id
        logs.append(f"상위 태스크 지정: {parent.id}")
    elif args.no_parent and task.meta.get("parent"):
        updates["parent"] = ""
        logs.append(f"상위 태스크 해제: {task.meta['parent']}")
    related = list(task_list_field(task, "related"))  # a copy: compared with the stored list below
    for r in split_csv(args.add):
        rid = get_task(project, r).id
        if rid.casefold() == task.id.casefold():
            raise DeskError("자기 자신을 연관 태스크로 지정할 수 없습니다")
        if rid.casefold() not in (x.casefold() for x in related):
            related.append(rid)
            logs.append(f"연관 태스크 추가: {rid}")
    for r in split_csv(args.remove):
        kept = [x for x in related if x.casefold() != r.casefold()]
        if len(kept) != len(related):
            related = kept
            logs.append(f"연관 태스크 제거: {r}")
    if related != task_list_field(task, "related"):
        updates["related"] = related
    if updates:
        set_meta(task, updates)
        append_log(task, logs)
        write_index(project)
    out({"task": task.id, "parent": task.meta.get("parent") or None,
         "related": task_list_field(task, "related"), "changed": bool(updates)})


def cmd_link(args) -> None:
    root = ensure_root()
    project, info = current_project(root, Path(args.cwd), args.project)
    task = get_task(project, args.id)
    if task.archived:
        raise DeskError("아카이브된 태스크에는 브랜치를 연결할 수 없습니다")
    branch = args.branch or info.branch
    if not branch:
        raise DeskError("연결할 브랜치를 알 수 없습니다")
    if branch.casefold() in (b.casefold() for b in task.branches):
        out({"task": task.id, "branches": task.branches, "changed": False})
        return
    check_branch_free(project, branch, task.id)
    set_meta(task, {"branches": task.branches + [branch]})
    append_log(task, [f"브랜치 연결: {branch}"])
    write_index(project)
    out({"task": task.id, "branches": task.branches, "changed": True})


def cmd_unlink(args) -> None:
    root = ensure_root()
    project, info = current_project(root, Path(args.cwd), args.project)
    task = get_task(project, args.id)
    if task.archived:
        raise DeskError("아카이브된 태스크의 브랜치 연결은 바꿀 수 없습니다")
    branch = args.branch or info.branch
    if not branch:
        raise DeskError("해제할 브랜치를 알 수 없습니다")
    kept = [b for b in task.branches if b.casefold() != branch.casefold()]
    if len(kept) == len(task.branches):
        out({"task": task.id, "branches": task.branches, "changed": False})
        return
    set_meta(task, {"branches": kept})
    append_log(task, [f"브랜치 연결 해제: {branch}"])
    write_index(project)
    out({"task": task.id, "branches": kept, "changed": True,
         **({"warning": "연결된 브랜치가 없는 태스크가 되었습니다. 브랜치에서 이어 갈 일이면 adesk link 로 다시 연결하세요"} if not kept else {})})


def cmd_status(args) -> None:
    root = ensure_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    task = get_task(project, args.id)
    if task.archived:
        raise DeskError("아카이브된 태스크입니다. adesk reopen 을 사용하세요")
    if args.status == "in_progress":
        for b in task.branches:
            check_branch_free(project, b, task.id)
    old = task.status
    set_meta(task, {"status": args.status})
    append_log(task, [f"상태 변경: {old} → {args.status}"])
    write_index(project)
    out({"task": task.id, "status": args.status})


def cmd_close(args) -> None:
    root = ensure_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    task = get_task(project, args.id)
    if task.archived:
        raise DeskError("이미 아카이브된 태스크입니다")
    dest = project.dir / "archive" / task.id
    if dest.exists():
        raise DeskError(f"아카이브에 같은 ID가 이미 있습니다: {dest}")
    # the status is written before the move: when the move fails the task is already out of the
    # open lists, and running the same close again only finishes the move
    if task.status != args.result:
        set_meta(task, {"status": args.result})
        append_log(task, [f"태스크 종료: {args.result}"] + ([args.note] if args.note else []))
    move_dir(task.dir, dest)
    write_index(project)
    open_children = [t.id for t in children_of(project, task.id, open_only=True)]
    out({"task": task.id, "status": args.result, "dir": str(dest),
         **({"warning": f"열린 하위 태스크가 남아 있습니다: {', '.join(open_children)}"} if open_children else {})})


def cmd_reopen(args) -> None:
    root = ensure_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    task = get_task(project, args.id)
    if not task.archived:
        raise DeskError("아카이브된 태스크가 아닙니다")
    for b in task.branches:
        check_branch_free(project, b, task.id)
    dest = project.dir / "tasks" / task.id
    move_dir(task.dir, dest)
    task.dir, task.archived = dest, False
    set_meta(task, {"status": "in_progress"})
    append_log(task, ["태스크 재개"])
    write_index(project)
    out({"task": task.id, "status": "in_progress", "dir": str(dest)})


def cmd_delete(args) -> None:
    root = ensure_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    import shutil
    trashed = find_trashed(project, args.id)
    if args.purge:
        if not trashed:
            get_task(project, args.id)  # not found at all: say so
            raise DeskError(f"휴지통에 있는 태스크만 영구히 지울 수 있습니다. 먼저 adesk delete {args.id} 로 휴지통으로 옮기세요")
        shutil.rmtree(trashed.dir)
        out({"task": trashed.id, "status": "purged", "note": KN_DELETE_NOTE})
        return
    if trashed:
        raise DeskError(f"이미 휴지통에 있는 태스크입니다: {trashed.id}. 영구히 지우려면 --purge")
    task = get_task(project, args.id)
    dest = project.dir / TRASH_DIR / task.id
    if dest.exists():  # a leftover folder without state.md
        raise DeskError(f"휴지통에 같은 이름의 폴더가 이미 있습니다: {dest}")
    set_meta(task, {"trashed": now_minute()})
    append_log(task, [TRASH_LOG_LINE])
    dest.parent.mkdir(parents=True, exist_ok=True)
    move_dir(task.dir, dest)
    write_index(project)
    out({"task": task.id, "status": task.status, "dir": str(dest), "deleted": True})


def cmd_restore(args) -> None:
    root = ensure_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    task = find_trashed(project, args.id)
    if not task:
        get_task(project, args.id)  # not found at all: say so
        raise DeskError(f"휴지통에 없는 태스크입니다: {args.id}")
    for t in load_tasks(project, include_archive=True):
        if t.id.casefold() == task.id.casefold():
            raise DeskError(f"같은 ID의 태스크가 이미 있습니다: {t.dir}")
    archived = task.status in CLOSED_STATUSES
    if not archived:
        for b in task.branches:
            check_branch_free(project, b, task.id)
    dest = project.dir / ("archive" if archived else "tasks") / task.id
    if dest.exists():  # a leftover folder without state.md
        raise DeskError(f"같은 이름의 폴더가 이미 있습니다: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    move_dir(task.dir, dest)
    task.dir, task.archived, task.trashed = dest, archived, False
    set_meta(task, {"trashed": None})
    append_log(task, ["휴지통에서 되살림"])
    write_index(project)
    out({"task": task.id, "status": task.status, "dir": str(dest), "archived": archived})


def migrate_task_statuses(root: Path, dry_run: bool) -> list[dict]:
    """Schema 1 → 2: active/paused/abandoned become in_progress/on_hold/cancelled."""
    changed = []
    for p in load_projects(root):
        touched = False
        for t in load_tasks(p, include_archive=True):
            old = t.meta.get("status", "")
            if old in LEGACY_STATUSES:
                changed.append({"project": p.key, "task": t.id, "from": old, "to": LEGACY_STATUSES[old]})
                if not dry_run:
                    write_text(t.state_path, update_frontmatter(read_text(t.state_path),
                                                                {"status": LEGACY_STATUSES[old]}))
                    touched = True
        if touched:
            write_index(p)
    return changed


MIGRATIONS = {1: migrate_task_statuses}  # from version N to N + 1


def cmd_migrate(args) -> None:
    root = desk_root()
    version = schema_version(root)
    if version is None or version == SCHEMA_VERSION:
        out({"schema_version": version or SCHEMA_VERSION, "changed": [], "note": "변환할 것이 없습니다"})
        return
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise DeskError(f"이 플러그인보다 새 형식이거나 알 수 없는 스키마입니다: {version}. 플러그인을 업데이트하세요")
    broken = [f"{p.key}/{t.id}" for p in load_projects(root) for t in load_tasks(p, include_archive=True)
              if t.status == "?"]
    if broken:
        raise DeskError(f"state.md frontmatter를 읽을 수 없는 태스크가 있어 변환할 수 없습니다: {', '.join(broken)}. 먼저 고치세요")
    changed = []
    for v in range(version, SCHEMA_VERSION):
        changed += MIGRATIONS[v](root, args.dry_run)
    if not args.dry_run:
        write_schema_version(root)
    out({"from": version, "to": SCHEMA_VERSION, "dry_run": args.dry_run, "changed": changed})


def cmd_list(args) -> None:
    root = desk_root()
    projects = [get_project(root, args.project)] if args.project else load_projects(root)
    rows = []
    for p in projects:
        if args.deleted:
            for t in load_trash(p):
                rows.append({"project": p.key, "id": t.id, "title": t.meta.get("title", ""),
                             "status": t.status, "branches": t.branches,
                             "parent": t.meta.get("parent") or None, "deleted": trashed_at(t)})
            continue
        for t in load_tasks(p, include_archive=args.all):
            rows.append({"project": p.key, "id": t.id, "title": t.meta.get("title", ""),
                         "status": t.status, "branches": t.branches,
                         "parent": t.meta.get("parent") or None,
                         "updated": t.meta.get("updated", ""), "archived": t.archived})
    out(rows)


def cmd_index(args) -> None:
    root = desk_root()
    projects = [get_project(root, args.project)] if args.project else load_projects(root)
    written = [str(write_index(p)) for p in projects]
    written += [str(x) for x in [write_kn_index(root, p) for p in projects] + [write_kn_index(root, None)] if x]
    out(written)


def cmd_kn_add(args) -> None:
    root = ensure_root()
    project, info = (None, repo_info(Path(args.cwd))) if args.is_global else current_project(root, Path(args.cwd), args.project)
    if args.is_global and args.paths:
        raise DeskError("전역 항목에는 --paths 를 쓸 수 없습니다. --tags 로 매칭합니다")
    sources = split_csv(args.sources)
    if not sources and project:
        task = get_session_task(project, env_session_id())
        if task:
            sources = [task.id]
    body = args.body or "<!-- 요약: 15줄 이내. 사실 하나만 담고, 근거가 되는 코드 위치(경로:줄)를 포함한다. -->"
    values = {
        "id": "", "title": format_scalar(args.title), "kind": args.kind,
        "paths": format_value(split_csv(args.paths)), "tags": format_value(split_csv(args.tags)),
        "sources": format_value(sources), "location": format_scalar(args.location),
        "date": today(), "verified": now_minute(), "body": body,
    }
    # allocating the ID reads then writes: safe because `run` holds the data root lock
    entry_id = values["id"] = next_entry_id(load_entries(root, project), "K" if project else "G")
    name = f"{entry_id}-{sanitize(args.slug)}.md" if args.slug else f"{entry_id}.md"
    path = kn_dir(root, project) / name
    write_text(path, render_template(project.lang if project else "ko", "knowledge.md", values))
    write_kn_index(root, project)
    out({"id": entry_id, "scope": project.key if project else "global", "file": path.as_posix()})


def cmd_kn_list(args) -> None:
    root = desk_root()
    project, _ = try_project(root, args)
    entries = all_entries(root, project)
    if not args.all:
        entries = [e for e in entries if e.live]
    out([e.as_dict() for e in entries])


def cmd_kn_search(args) -> None:
    root = desk_root()
    project, info = try_project(root, args)
    entries = all_entries(root, project)
    if not args.all:
        entries = [e for e in entries if e.live]
    terms = [t.casefold() for t in args.terms]
    if args.path:
        matched = entries_for_path(entries, repo_relative(args.path, info.toplevel))
        if terms:
            matched = [e for e in matched if search_score(e, terms) > 0]
        # all of them, in injection order: the hook points here for the entries it left out
        out([e.as_dict() for e in sorted(matched, key=lambda e: inject_order(e.meta.get("kind", ""), e.id))])
        return
    if not terms:
        raise DeskError("검색어 또는 --path 가 필요합니다")
    scored = sorted(((search_score(e, terms), e) for e in entries), key=lambda x: -x[0])
    out([e.as_dict() for s, e in scored if s > 0][:args.limit])


def cmd_kn_show(args) -> None:
    root = desk_root()
    project, _ = try_project(root, args)
    # inactive entries too: their status (superseded_by, retired_reason) is in the output
    shown = [dict(e.as_dict(), body=e.body) for e in (find_entry(root, project, i) for i in args.ids)]
    out(shown[0] if len(shown) == 1 else shown)


def cmd_kn_supersede(args) -> None:
    root = ensure_root()
    project, _ = try_project(root, args)
    old = find_entry(root, project, args.id)
    new = find_entry(root, project, args.by)
    if old.id == new.id:
        raise DeskError("자기 자신으로 대체할 수 없습니다")
    update_entry(old, {"status": "superseded", "superseded_by": new.id})
    write_kn_index(root, project if old.scope != "global" else None)
    out({"id": old.id, "status": "superseded", "superseded_by": new.id})


def check_live(e) -> None:
    if e.status == "superseded":
        raise DeskError(f"대체된 항목입니다: {e.id} → {e.meta.get('superseded_by')}")
    if e.status == "retired":
        raise DeskError(f"폐기된 항목입니다: {e.id} ({e.meta.get('retired_reason') or '사유 없음'})")
    if e.status == "deleted":
        raise DeskError(f"삭제된 항목입니다: {e.id}")


def cmd_kn_retire(args) -> None:
    root = ensure_root()
    project, _ = try_project(root, args)
    e = find_entry(root, project, args.id)
    check_live(e)
    update_entry(e, {"status": "retired", "retired_reason": args.reason})
    write_kn_index(root, project if e.scope != "global" else None)
    out({"id": e.id, "status": "retired"})


KN_DELETE_NOTE = "데이터 폴더를 동기화하거나 백업하고 있다면 그 버전 기록에는 이전 내용이 남아 있을 수 있다"


def cmd_kn_delete(args) -> None:
    root = ensure_root()
    project, _ = try_project(root, args)
    e = find_entry(root, project, args.id)
    if args.purge:
        # the highest id is reused once its file is gone: only safe while nothing refers to it
        refs = entry_references(root, e)
        if refs:
            listed = ", ".join(refs[:10]) + (f" 외 {len(refs) - 10}곳" if len(refs) > 10 else "")
            hint = "" if e.status == "deleted" else " 또는 --purge 없이 삭제(내용만 지우고 ID는 남김)"
            raise DeskError(f"{e.id} 를 참조하는 기록이 있어 파일을 지울 수 없습니다: {listed}. 참조를 정리하세요{hint}")
        e.path.unlink()
        status, path = "purged", None
    else:
        if e.status == "deleted":
            raise DeskError(f"이미 삭제된 항목입니다: {e.id}. 파일까지 지우려면 --purge")
        status, path = "deleted", tombstone_entry(e).as_posix()
    write_kn_index(root, project if e.scope != "global" else None)
    out({"id": e.id, "status": status, "file": path, "note": KN_DELETE_NOTE})


def cmd_kn_verify(args) -> None:
    root = ensure_root()
    project, _ = try_project(root, args)
    e = find_entry(root, project, args.id)
    check_live(e)
    verified = now_minute()
    updates = {"status": "valid", "verified": verified}
    if "stale_reason" in e.meta:
        updates["stale_reason"] = None  # drop it: an entry that was never stale has no such line
    update_entry(e, updates)
    write_kn_index(root, project if e.scope != "global" else None)
    out({"id": e.id, "status": "valid", "verified": verified})


def cmd_kn_stale(args) -> None:
    root = ensure_root()
    project, _ = try_project(root, args)
    e = find_entry(root, project, args.id)
    check_live(e)
    update_entry(e, {"status": "stale", "stale_reason": args.reason or "수동 표시"})
    write_kn_index(root, project if e.scope != "global" else None)
    out({"id": e.id, "status": "stale"})


def desk_target(root: Path, raw: str, cwd: Path) -> Path:
    """The existing data root file a put/append names: absolute, or relative to the data root
    (then to the current directory). Anything outside the data root is refused."""
    given = Path(native_path(raw)).expanduser()
    candidates = [given] if given.is_absolute() else [root / given, cwd / given]
    path = next((p for p in candidates if p.exists()), candidates[0])
    if not is_under(root, path) or normalize_path(path) == normalize_path(root):
        raise DeskError(f"데이터 폴더({root.as_posix()}) 안의 파일만 쓸 수 있습니다: {raw}")
    if not path.is_file():
        raise DeskError(f"데이터 폴더에 없는 파일입니다: {path.as_posix()}. 태스크와 knowledge 파일은 "
                        "`adesk new`·`adesk kn add` 로 만들고, 위치는 `adesk where` 의 task_dir 로 확인하세요")
    path = path.resolve()  # the file's own spelling: what is checked depends on the name (STATE.MD is state.md)
    if path.name == "index.md":
        raise DeskError("index.md 는 자동 생성 파일입니다. `adesk index` 로 다시 만드세요")
    return path


def read_content(source: str) -> str:
    if source == "-":
        text = sys.stdin.read()
    else:
        try:
            text = read_text(Path(native_path(source)).expanduser())
        except OSError as e:
            raise DeskError(f"--file 을 읽을 수 없습니다: {source} ({e.strerror or e})")
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.strip():
        raise DeskError("쓸 내용이 비어 있습니다")
    return text if text.endswith("\n") else text + "\n"


def frontmatter_kind(path: Path) -> str | None:
    """"state" or "knowledge" for the files whose frontmatter only the CLI writes (K0045)."""
    if path.name == "state.md":
        return "state"
    if path.parent.name == "knowledge" and path.suffix == ".md":
        return "knowledge"
    return None


def check_desk_text(path: Path, text: str) -> None:
    """Refuse content the CLI and hooks could not read back: frontmatter of state.md and
    knowledge entries, TOML of project.toml and desk.toml."""
    if path.suffix == ".toml":
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise DeskError(f"{path.name} 의 TOML을 읽을 수 없어 쓰지 않았습니다: {e}")
    elif frontmatter_kind(path):
        try:
            parse_frontmatter(text)
        except DeskError as e:
            raise DeskError(f"{path.name} 의 frontmatter를 읽을 수 없어 쓰지 않았습니다: {e}. "
                            "agent-desk:desk 스킬의 frontmatter 형식을 따르세요") from None


def current_meta(path: Path) -> dict | None:
    try:
        return parse_frontmatter(read_text(path))
    except DeskError:
        return None


def keep_frontmatter(path: Path, text: str, auto: dict, warnings: list[str]) -> tuple[str, dict]:
    """The copy's body under the current file's frontmatter (the copy's own frontmatter, if it
    has one, is ignored) with the CLI's `auto` updates applied. Returns it and `kept`: the keys
    whose value in the copy differed from the one written (None: not written), auto keys aside."""
    current = read_text(path)
    if text.split("\n", 1)[0].strip() == "---":
        try:
            body = split_frontmatter(text)[1]
        except DeskError:
            raise DeskError("사본이 `---` 로 시작하지만 frontmatter가 닫히지 않았습니다. "
                            "frontmatter는 현재 파일 값을 유지하니 본문만 넘겨도 됩니다") from None
        try:
            given = parse_frontmatter(text)
        except DeskError as e:
            given = {}
            warnings.append(f"사본의 frontmatter를 읽을 수 없어 무시했습니다: {e}")
    else:
        body, given = text, None  # body only
    merged = "---\n" + "\n".join(split_frontmatter(current)[0]) + "\n---\n" + body
    if auto:
        merged = update_frontmatter(merged, auto)
    if given is None:
        return merged, {}
    written = parse_frontmatter(merged)
    # an empty value and a missing key mean the same (`kn verify` writes `stale_reason:`)
    kept = {k: written.get(k) for k in dict.fromkeys([*written, *given])
            if k not in auto and (written.get(k) or "") != (given.get(k) or "")}
    return merged, kept


def auto_updates(path: Path, kind: str | None, meta: dict | None, verified: bool) -> dict:
    """The frontmatter keys the CLI sets itself when it writes the file."""
    if verified and not kind:
        raise DeskError("--verified 는 state.md 와 knowledge 항목에만 쓸 수 있습니다")
    if verified and meta is None:
        raise DeskError(f"{path.name} 의 frontmatter를 읽을 수 없어 --verified 를 적용할 수 없습니다. 먼저 고치세요")
    auto = {}
    if kind == "state" or (meta and "updated" in meta):
        auto["updated"] = today()
    if verified and kind == "state":
        auto["verified"] = now_minute()
    elif verified:  # the same as `kn verify`
        status = meta.get("status", "valid")
        if status in KN_INACTIVE:
            raise DeskError(f"{status} 상태인 항목은 확인 표시할 수 없습니다: {path.name}")
        auto.update({"status": "valid", "verified": now_minute()})
        if "stale_reason" in meta:
            auto["stale_reason"] = None
    return auto


def refresh_indexes(root: Path, path: Path) -> None:
    """index.md shows titles, statuses and `updated`: rebuild the one the written file feeds."""
    rel = path.resolve().relative_to(root.resolve()).parts
    if rel[0] == "projects" and not (root / "projects" / rel[1] / "project.toml").is_file():
        return
    if rel[0] == "projects" and task_dir_of(path):
        write_index(get_project(root, rel[1]))
    elif path.parent.name == "knowledge" and path.suffix == ".md":
        write_kn_index(root, get_project(root, rel[1]) if rel[0] == "projects" else None)


KEPT_NOTE = ("frontmatter는 현재 값을 유지했습니다. 바꾸려면 `adesk set`(제목·설계 문서·링크), `status`·`link`·`relate`, "
             "`kn edit`·`kn verify` 를 쓰세요")


def write_desk_file(args, append: bool) -> None:
    root = ensure_root()
    path = desk_target(root, args.path, Path(args.cwd))
    content = read_content(args.file)
    kind = frontmatter_kind(path)
    meta = current_meta(path) if kind else None
    auto = auto_updates(path, kind, meta, getattr(args, "verified", False))
    warnings, kept = [], {}
    if append:
        old = read_text(path)
        if old and not old.endswith("\n"):
            old += "\n"
        if old.strip() and content.startswith("#") and not old.endswith("\n\n"):
            old += "\n"  # a new heading starts after a blank line, as `append_log` writes it
        text = old + content
        check_desk_text(path, text)
        if auto:
            text = update_frontmatter(text, auto)
    elif kind and meta is not None:
        text, kept = keep_frontmatter(path, content, auto, warnings)
    else:
        # also the repair of an unreadable state.md or entry: the only case the copy's
        # frontmatter is written, once it reads back (no command could fix it otherwise)
        check_desk_text(path, content)
        text = update_frontmatter(content, auto) if auto else content
    if path.name == "decisions.md":
        warnings += decision_problems(text)
    write_text(path, text)
    task_dir = task_dir_of(path)
    if task_dir and kind != "state" and not touch_task(task_dir):
        warnings.append("state.md 의 frontmatter를 읽을 수 없어 updated 를 갱신하지 못했습니다")
    refresh_indexes(root, path)
    out({"file": path.as_posix(), "action": "append" if append else "put", "lines": text.count("\n"),
         **({"kept": kept, "note": KEPT_NOTE} if kept else {}), **{k: auto[k] for k in ("verified",) if k in auto},
         **({"warnings": warnings} if warnings else {})})


def refuse_option(hint: str):
    """An argparse action for an option an agent may guess here but another command handles (K0035)."""
    import argparse

    class Refuse(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            parser.exit(2, f"오류: {option_string} 는 이 명령으로 바꾸지 않습니다. {hint}\n")
    return Refuse


def cmd_set(args) -> None:
    root = ensure_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    task = get_task(project, args.id)
    if task.status == "?":
        raise DeskError(f"{task.id} 의 state.md frontmatter를 읽을 수 없습니다: {task.meta.get('error', '')}. "
                        "사본을 고쳐 `adesk put` 으로 바로잡으세요")
    updates, logs = {}, []
    if args.title is not None:
        if not args.title.strip():
            raise DeskError("제목이 비어 있습니다")
        if args.title != task.meta.get("title", ""):
            updates["title"] = args.title
            logs.append(f"제목 변경: {task.meta.get('title', '')} → {args.title}")
    if args.design_doc is not None and args.design_doc != (task.meta.get("design_doc") or ""):
        updates["design_doc"] = args.design_doc
        logs.append(f"설계 문서 지정: {args.design_doc}" if args.design_doc else "설계 문서 해제")
    links = list(task_list_field(task, "links"))  # a copy: compared with the stored list below
    for url in args.add_link or []:
        if url not in links:
            links.append(url)
            logs.append(f"링크 추가: {url}")
    for url in args.remove_link or []:
        if url in links:
            links.remove(url)
            logs.append(f"링크 제거: {url}")
    if links != task_list_field(task, "links"):
        updates["links"] = links
    if updates:
        set_meta(task, updates)
        append_log(task, logs)
        write_index(project)
    out({"task": task.id, "title": task.meta.get("title", ""), "design_doc": task.meta.get("design_doc") or None,
         "links": task_list_field(task, "links"), "changed": bool(updates)})


KN_LIST_KEYS = ("paths", "tags", "sources")


def cmd_kn_set(args) -> None:
    root = ensure_root()
    project, _ = try_project(root, args)
    e = find_entry(root, project, args.id)
    if e.status == "deleted":
        raise DeskError(f"삭제된 항목입니다: {e.id}")
    if args.title is not None and not args.title.strip():
        raise DeskError("제목이 비어 있습니다")
    updates = {k: v for k in ("title", "kind", "location")
               if (v := getattr(args, k)) is not None and v != e.meta.get(k)}
    for key in KN_LIST_KEYS:
        one = key[:-1]  # paths -> path
        whole, add, remove = getattr(args, key), getattr(args, f"add_{one}"), getattr(args, f"remove_{one}")
        items = split_csv(whole) if whole is not None else list(e.items(key))
        items += [x for x in add or [] if x not in items]
        items = [x for x in items if x not in (remove or [])]
        if items != e.items(key):
            updates[key] = items
    if e.scope == "global" and updates.get("paths"):
        raise DeskError("전역 항목에는 paths 를 쓸 수 없습니다. tags 로 매칭합니다")
    if updates:
        update_entry(e, updates)
        write_kn_index(root, project if e.scope != "global" else None)
    out({"id": e.id, "changed": sorted(updates),
         **{k: e.meta.get(k) for k in ("title", "kind", "location", *KN_LIST_KEYS)}})


def cmd_doctor(args) -> None:
    root = ensure_root()
    project, _ = try_project(root, args)
    if not project:
        raise DeskError("프로젝트를 알 수 없습니다. 등록된 저장소에서 실행하거나 --project 를 지정하세요")
    top = project_repo(root, project, Path(args.cwd))
    issues: list[dict] = []
    if not top:
        issues.append({"level": "info", "kind": "no-repo",
                       "message": "프로젝트 저장소를 찾지 못해 git 기반 점검을 건너뜀"})
    alias = python_alias()
    if alias:
        issues.append({"level": "warn", "kind": "python-alias", "target": alias,
                       "message": PYTHON_ALIAS_MESSAGE.format(path=alias)})
    doctor_tasks(project, top, issues)
    if (project.dir / OBSOLETE_FOCUS_FILE).exists():
        issues.append({"level": "info", "kind": "focus-obsolete", "target": str(project.dir / OBSOLETE_FOCUS_FILE),
                       "message": "0.8 이전의 프로젝트 공용 포커스 파일(지금 포커스는 세션마다 따로 둔다). `doctor --apply` 로 지운다"})
    doctor_knowledge(root, project, top, args.min_commits, issues)
    doctor_knowledge(root, None, None, args.min_commits, issues)

    applied = []
    if args.apply:
        if (project.dir / OBSOLETE_FOCUS_FILE).exists():
            (project.dir / OBSOLETE_FOCUS_FILE).unlink()
            applied.append(OBSOLETE_FOCUS_FILE)
        stale_kinds = ("kn-changed", "kn-paths-empty", "kn-missing-ref")
        reasons: dict[str, list[str]] = {}
        for i in issues:
            if i["kind"] in stale_kinds and i.get("status") == "valid":
                reasons.setdefault(i["id"], []).append(i["message"])
        for entry_id, msgs in reasons.items():
            e = find_entry(root, project, entry_id)
            update_entry(e, {"status": "stale", "stale_reason": "; ".join(msgs)})
            applied.append(entry_id)
        if applied:
            write_kn_index(root, project)
    write_text(project.dir / DOCTOR_MARKER, dt.datetime.now().isoformat(timespec="minutes") + "\n")
    out({"project": project.key, "repo": top.as_posix() if top else None,
         "issues": issues, "marked_stale": applied})


# commands that never change the data root. Every other command, a new one included, holds the
# data root lock for its whole run (K0044). hook only reads (its session cache is in the temp dir)
# the focus file of releases before 0.8 (one current task per project, replaced by the session's task)
OBSOLETE_FOCUS_FILE = "focus.txt"

READ_ONLY_COMMANDS = {"where", "list", "hook", "kn list", "kn search", "kn show"}


def changes_data(args) -> bool:
    name = f"kn {args.kn_cmd}" if args.cmd == "kn" else args.cmd
    if name == "doctor":
        return args.apply  # without --apply it writes only its own marker file, in one atomic write
    return name not in READ_ONLY_COMMANDS


def run(argv: list[str]) -> int:
    import argparse

    class Once(argparse.Action):
        """An option that takes one value: given twice, argparse would keep the last silently."""
        def __call__(self, parser, namespace, values, option_string=None):
            if getattr(namespace, self.dest) is not None:
                parser.error(f"{option_string} 는 한 번만 지정할 수 있습니다. 파일마다 따로 실행하세요")
            setattr(namespace, self.dest, values)

    ap = argparse.ArgumentParser(prog="adesk", description="agent-desk CLI")
    ap.add_argument("--cwd", default=os.getcwd(), help="기준 디렉터리 (기본: 현재 디렉터리)")
    ap.add_argument("--project", help="프로젝트 key (기본: 현재 저장소로 자동 판별)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("where", help="현재 저장소·브랜치의 프로젝트와 태스크 확인")

    p = sub.add_parser("project-init", help="현재 저장소를 프로젝트로 등록")
    p.add_argument("key")
    p.add_argument("--id-template", default=DEFAULT_ID_TEMPLATE)
    p.add_argument("--shared", help="공용 브랜치 목록 (쉼표 구분)")
    p.add_argument("--lang", default="ko")

    p = sub.add_parser("new", help="태스크 생성")
    p.add_argument("--title", required=True)
    p.add_argument("--slug", help="브랜치 없는 태스크의 ID 슬러그")
    p.add_argument("--branch", help="연결할 브랜치 (기본: 현재 브랜치)")
    p.add_argument("--no-branch", action="store_true")
    p.add_argument("--parent", help="상위 태스크 ID (한 단계만 허용)")
    p.add_argument("--related", help="연관 태스크 ID (쉼표 구분)")
    p.add_argument("--no-focus", action="store_true", help="만든 태스크에 이 세션의 포커스를 두지 않음 (서브에이전트 등)")
    p.add_argument("--no-open", dest="no_focus", action="store_true", help=argparse.SUPPRESS)  # a guess (K0035)
    p.add_argument("--todo", action="store_true", help="착수 전(todo) 태스크로 만듦. 포커스를 두지 않음")

    p = sub.add_parser("focus", help="이 세션의 포커스(작업할 태스크)를 정하고 그 기록을 텍스트로 출력 (todo면 진행 중으로)")
    p.add_argument("id", nargs="?")
    p.add_argument("--clear", action="store_true", help="이 세션의 포커스 해제")

    p = sub.add_parser("relate", help="태스크의 상위·연관 관계 변경")
    p.add_argument("id")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--parent", help="상위 태스크 지정")
    g.add_argument("--no-parent", action="store_true", help="상위 태스크 해제")
    p.add_argument("--add", help="연관 태스크 추가 (쉼표 구분)")
    p.add_argument("--remove", help="연관 태스크 제거 (쉼표 구분)")

    p = sub.add_parser("link", help="태스크에 브랜치 추가 연결")
    p.add_argument("id")
    p.add_argument("--branch")

    p = sub.add_parser("unlink", help="태스크에서 브랜치 연결 해제")
    p.add_argument("id")
    p.add_argument("--branch", help="생략하면 현재 브랜치")

    p = sub.add_parser("status", help="열린 태스크의 상태 변경 (todo/in_progress/on_hold)")
    p.add_argument("id")
    p.add_argument("status", choices=OPEN_STATUSES)

    p = sub.add_parser("close", help="태스크 종료 후 아카이브")
    p.add_argument("id")
    p.add_argument("result", choices=CLOSED_STATUSES)
    p.add_argument("--note")

    p = sub.add_parser("reopen", help="아카이브된 태스크 재개")
    p.add_argument("id")

    p = sub.add_parser("delete", help="태스크를 휴지통으로 옮김 (--purge: 휴지통의 태스크를 영구 삭제)")
    p.add_argument("id")
    p.add_argument("--purge", action="store_true", help="휴지통에 있는 태스크를 영구히 지운다")

    p = sub.add_parser("restore", help="휴지통의 태스크를 되살림")
    p.add_argument("id")

    p = sub.add_parser("list", help="태스크 목록")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="아카이브 포함")
    g.add_argument("--deleted", action="store_true", help="휴지통의 태스크만")

    sub.add_parser("index", help="태스크·knowledge index.md 재생성")

    p = sub.add_parser("set", help="태스크의 제목·설계 문서·링크 변경",
                       description="상태는 status·close, 브랜치는 link, 상위·연관 태스크는 relate 로 바꾼다.")
    p.add_argument("id")
    p.add_argument("--title", help="제목 (오타 수정용. 일의 범위가 바뀌면 새 태스크)")
    p.add_argument("--design-doc", help="설계 문서 URL (빈 값이면 해제)")
    p.add_argument("--add-link", action="append", metavar="URL", help="링크 추가 (반복 가능)")
    p.add_argument("--remove-link", action="append", metavar="URL", help="링크 제거 (반복 가능)")
    for opts, hint in ((("--status",), "`adesk status <id> <상태>`, 종료는 `adesk close`"),
                       (("--branch", "--branches"), "`adesk link <id> --branch <브랜치>`"),
                       (("--parent", "--related"), "`adesk relate <id> --parent/--add/--remove`")):
        p.add_argument(*opts, nargs="?", action=refuse_option(hint), help=argparse.SUPPRESS)

    for name, help_text in (("put", "데이터 폴더 파일을 --file 내용으로 교체 (frontmatter는 현재 값 유지)"),
                            ("append", "데이터 폴더 파일 끝에 --file 내용을 덧붙임")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("path", help="데이터 폴더 안의 파일 (절대 경로 또는 데이터 폴더 기준 상대 경로)")
        p.add_argument("--file", "-f", required=True, help="쓸 내용이 든 파일 (- 이면 표준 입력)")
        if name == "put":
            p.add_argument("--verified", action="store_true",
                           help="내용을 코드와 대조해 확인했음을 기록 (state.md, knowledge 항목)")

    p = sub.add_parser("migrate", help="데이터 폴더를 현재 스키마로 변환")
    p.add_argument("--dry-run", action="store_true", help="바꿀 내용만 출력")

    kn = sub.add_parser("kn", help="knowledge 항목 관리").add_subparsers(dest="kn_cmd", required=True)
    p = kn.add_parser("add", help="항목 추가")
    p.add_argument("--title", required=True)
    p.add_argument("--kind", required=True, choices=KN_KINDS)
    p.add_argument("--paths", help="관련 경로 glob (저장소 기준, 쉼표 구분)")
    p.add_argument("--tags", help="태그 (쉼표 구분)")
    p.add_argument("--sources", help="출처 태스크 ID (기본: 이 세션의 포커스)")
    p.add_argument("--location", default="inline", help="inline | repo:<경로> | claude-docs:<url>")
    p.add_argument("--slug", help="파일명용 영문 슬러그")
    p.add_argument("--body", help="본문 (생략 시 안내 주석만 넣고 이후 put 으로 작성)")
    p.add_argument("--global", dest="is_global", action="store_true", help="전역 항목으로 추가")
    p = kn.add_parser("set", help="항목의 제목·종류·location·paths·tags·sources 변경",
                      description="제목·종류는 오타·분류 수정용이다. 내용의 의미가 바뀌면 새 항목을 추가하고 supersede 한다. "
                                  "상태는 kn verify·stale·supersede·retire 로 바꾼다.")
    p.add_argument("id")
    p.add_argument("--title")
    p.add_argument("--kind", choices=KN_KINDS)
    p.add_argument("--location", help="inline | repo:<경로> | claude-docs:<url>")
    for key, what in (("paths", "관련 경로 glob"), ("tags", "태그"), ("sources", "출처 태스크 ID")):
        one = key[:-1]
        p.add_argument(f"--{key}", help=f"{what}, 목록 전체를 바꿈 (쉼표 구분)")
        p.add_argument(f"--add-{one}", action="append", metavar=one.upper(), help=f"{what} 추가 (반복 가능)")
        p.add_argument(f"--remove-{one}", action="append", metavar=one.upper(), help=f"{what} 제거 (반복 가능)")
    for opts, hint in ((("--status",), "`kn verify`·`kn stale`·`kn supersede`·`kn retire` 를 쓰세요"),
                       (("--verified",), "`adesk kn verify <id>` 를 쓰세요")):
        p.add_argument(*opts, nargs="?", action=refuse_option(hint), help=argparse.SUPPRESS)
    p = kn.add_parser("list", help="항목 목록 (프로젝트 + 전역)")
    p.add_argument("--all", action="store_true", help="대체·폐기·삭제된 항목 포함")
    p = kn.add_parser("search", help="항목 검색")
    p.add_argument("terms", nargs="*", help="검색어. 공백이 든 검색어는 구절 그대로와 단어별로 함께 찾는다")
    p.add_argument("--path", action=Once, help="이 파일에 해당하는 항목 (paths glob 매칭, 파일 하나)")
    p.add_argument("--all", action="store_true", help="대체·폐기·삭제된 항목 포함")
    p.add_argument("--limit", type=int, default=10, help="검색어 결과 최대 건수 (--path 에는 적용하지 않음)")
    p = kn.add_parser("show", help="항목 본문 전체 (대체·폐기·삭제된 항목은 상태와 함께)")
    p.add_argument("ids", nargs="+", metavar="id")
    p = kn.add_parser("supersede", help="항목을 다른 항목으로 대체")
    p.add_argument("id")
    p.add_argument("--by", required=True)
    p = kn.add_parser("retire", help="대체할 항목 없이 폐기 (파일은 남김)")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p = kn.add_parser("delete", help="내용을 지우고 ID만 남김 (--purge: 참조가 없으면 파일까지 삭제)")
    p.add_argument("id")
    p.add_argument("--purge", action="store_true", help="파일을 지운다. 이 ID를 참조하는 기록이 있으면 거부")
    p = kn.add_parser("verify", help="코드와 대조해 확인했음을 기록")
    p.add_argument("id")
    p = kn.add_parser("stale", help="재확인 필요로 표시")
    p.add_argument("id")
    p.add_argument("--reason", help="재확인이 필요한 이유")

    p = sub.add_parser("doctor", help="저장소 상태 점검 (낡은 knowledge, 충돌 사본, 방치된 태스크)")
    p.add_argument("--apply", action="store_true", help="낡은 것으로 판단된 valid 항목을 stale로 표시")
    p.add_argument("--min-commits", type=int, default=3, help="확인 이후 이 수 이상의 커밋이 paths를 바꾸면 낡은 것으로 판단")

    p = sub.add_parser("hook", help="Claude Code hook 진입점")
    p.add_argument("event", choices=HOOK_EVENTS)

    command = next((a for i, a in enumerate(argv) if not a.startswith("-")
                    and (i == 0 or argv[i - 1] not in ("--cwd", "--project"))), None)
    if command in ("open", "use", "switch"):  # guesses for focus (K0035)
        argv = [*argv[:argv.index(command)], "focus", *argv[argv.index(command) + 1:]]
    args = ap.parse_args(argv)
    handlers = {
        "where": cmd_where, "project-init": cmd_project_init, "new": cmd_new, "set": cmd_set,
        "link": cmd_link, "unlink": cmd_unlink, "relate": cmd_relate, "focus": cmd_focus, "status": cmd_status, "migrate": cmd_migrate, "close": cmd_close, "reopen": cmd_reopen,
        "delete": cmd_delete, "restore": cmd_restore, "list": cmd_list, "index": cmd_index,
        "put": lambda a: write_desk_file(a, append=False), "append": lambda a: write_desk_file(a, append=True), "hook": lambda a: cmd_hook(a.event), "doctor": cmd_doctor,
    }
    kn_handlers = {
        "add": cmd_kn_add, "set": cmd_kn_set, "list": cmd_kn_list, "search": cmd_kn_search, "show": cmd_kn_show,
        "supersede": cmd_kn_supersede, "retire": cmd_kn_retire, "delete": cmd_kn_delete,
        "verify": cmd_kn_verify, "stale": cmd_kn_stale,
    }
    handler = kn_handlers[args.kn_cmd] if args.cmd == "kn" else handlers[args.cmd]
    try:
        if changes_data(args):
            with root_lock(desk_root()):
                handler(args)
        else:
            handler(args)
    except DeskError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0
