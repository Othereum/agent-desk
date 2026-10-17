"""CLI commands and the argparse tree."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from .core import (
    CLOSED_STATUSES, DEFAULT_ID_TEMPLATE, DEFAULT_SHARED_BRANCHES, DeskError, LEGACY_STATUSES, OPEN_STATUSES,
    PYTHON_ALIAS_MESSAGE, SCHEMA_VERSION, TASK_FILES, desk_root, ensure_root, file_lock, now_minute, python_alias,
    read_text, schema_version, today, write_schema_version, write_text,
)
from .frontmatter import format_scalar, format_value, update_frontmatter
from .gitinfo import repo_info
from .model import (
    TRASH_DIR, TRASH_LOG_LINE, append_log, build_id, check_branch_free, children_of, current_project,
    find_trashed, get_focus_id, get_focus_task, get_project, get_task, load_projects, load_tasks, load_trash,
    main_worktree, match_project, projects_dir, render_template, resolve_parent, resolve_session_task, sanitize,
    set_focus, set_meta, task_list_field, trashed_at,
)
from .index import write_index
from .knowledge import (
    KN_KINDS, all_entries, entries_for_path, entry_references, find_entry, inject_order, kn_dir, load_entries,
    next_entry_id, repo_relative, search_score, split_csv, tombstone_entry, try_project, update_entry, write_kn_index,
)
from .doctor import DOCTOR_MARKER, doctor_knowledge, doctor_tasks, project_repo
from . import HOOK_EVENTS
from .hooks import cmd_hook


def out(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_where(args) -> None:
    root = desk_root()
    info = repo_info(Path(args.cwd))
    project = match_project(root, info) if (root / "projects").is_dir() else None
    task, source = resolve_session_task(project, info.branch) if project else (None, "")
    out({
        "root": str(root),
        "repo": str(info.toplevel) if info.toplevel else None,
        "branch": info.branch,
        "project": project.key if project else None,
        "project_dir": str(project.dir) if project else None,
        "task": task.id if task else None,
        "task_source": source or None,
        "task_dir": str(task.dir) if task else None,
        "focus": (focus.id if (focus := get_focus_task(project)) else None) if project else None,
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
    # a branchless task created on a shared branch (or detached) becomes the focus
    focused = (branch is None and not args.no_focus and not args.todo
               and (not info.branch or project.is_shared(info.branch)))
    if focused:
        set_focus(project, task_id)
    write_index(project)
    out({"task": task_id, "dir": str(tdir), "branch": branch,
         "parent": parent.id if parent else None, "related": related, "focused": focused})


def cmd_focus(args) -> None:
    root = ensure_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    if args.clear:
        set_focus(project, None)
    elif args.id:
        task = get_task(project, args.id)
        if task.archived or task.status not in OPEN_STATUSES:
            raise DeskError(f"열린 태스크만 포커스할 수 있습니다: {task.id} ({task.status})")
        set_focus(project, task.id)
        if task.status == "todo":  # focusing a task is starting it
            set_meta(task, {"status": "in_progress"})
            append_log(task, ["상태 변경: todo → in_progress (포커스 지정)"])
            write_index(project)
    focus = get_focus_task(project)
    out({"project": project.key, "focus": focus.id if focus else None,
         "title": focus.meta.get("title", "") if focus else None})


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
    set_meta(task, {"status": args.result})
    append_log(task, [f"태스크 종료: {args.result}"] + ([args.note] if args.note else []))
    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.move(str(task.dir), str(dest))
    unfocused = (get_focus_id(project) or "").casefold() == task.id.casefold()
    if unfocused:
        set_focus(project, None)
    write_index(project)
    open_children = [t.id for t in children_of(project, task.id, open_only=True)]
    out({"task": task.id, "status": args.result, "dir": str(dest), "unfocused": unfocused,
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
    import shutil
    shutil.move(str(task.dir), str(dest))
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
    append_log(task, [TRASH_LOG_LINE])
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(task.dir), str(dest))
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
    import shutil
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(task.dir), str(dest))
    task.dir, task.archived, task.trashed = dest, archived, False
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
        task, _ = resolve_session_task(project, info.branch)
        if task:
            sources = [task.id]
    body = args.body or "<!-- 요약: 15줄 이내. 사실 하나만 담고, 근거가 되는 코드 위치(경로:줄)를 포함한다. -->"
    values = {
        "id": "", "title": format_scalar(args.title), "kind": args.kind,
        "paths": format_value(split_csv(args.paths)), "tags": format_value(split_csv(args.tags)),
        "sources": format_value(sources), "location": format_scalar(args.location),
        "date": today(), "verified": now_minute(), "body": body,
    }
    base = kn_dir(root, project)
    # parallel agents may add entries at once: allocate the ID and create the file under one lock
    with file_lock(f"kn-{project.key if project else 'global'}"):
        entry_id = values["id"] = next_entry_id(load_entries(root, project), "K" if project else "G")
        name = f"{entry_id}-{sanitize(args.slug)}.md" if args.slug else f"{entry_id}.md"
        path = base / name
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
    update_entry(e, {"status": "valid", "verified": verified, "stale_reason": ""})
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
    focus_id = get_focus_id(project)
    if focus_id and not get_focus_task(project):
        issues.append({"level": "info", "kind": "focus-stale", "id": focus_id,
                       "message": "포커스가 종료되었거나 휴지통에 있거나 없는 태스크를 가리킴. `focus --clear` 또는 다른 태스크로 지정"})
    doctor_knowledge(root, project, top, args.min_commits, issues)
    doctor_knowledge(root, None, None, args.min_commits, issues)

    applied = []
    if args.apply:
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


def run(argv: list[str]) -> int:
    import argparse
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
    p.add_argument("--no-focus", action="store_true", help="브랜치 없는 태스크를 만들어도 포커스로 지정하지 않음")
    p.add_argument("--todo", action="store_true", help="착수 전(todo) 태스크로 만듦. 포커스로 지정하지 않음")

    p = sub.add_parser("focus", help="포커스 태스크 조회·지정·해제 (브랜치에 연결된 태스크가 없을 때 쓰는 태스크)")
    p.add_argument("id", nargs="?")
    p.add_argument("--clear", action="store_true")

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

    p = sub.add_parser("migrate", help="데이터 폴더를 현재 스키마로 변환")
    p.add_argument("--dry-run", action="store_true", help="바꿀 내용만 출력")

    kn = sub.add_parser("kn", help="knowledge 항목 관리").add_subparsers(dest="kn_cmd", required=True)
    p = kn.add_parser("add", help="항목 추가")
    p.add_argument("--title", required=True)
    p.add_argument("--kind", required=True, choices=KN_KINDS)
    p.add_argument("--paths", help="관련 경로 glob (저장소 기준, 쉼표 구분)")
    p.add_argument("--tags", help="태그 (쉼표 구분)")
    p.add_argument("--sources", help="출처 태스크 ID (기본: 현재 브랜치의 태스크, 없으면 포커스 태스크)")
    p.add_argument("--location", default="inline", help="inline | repo:<경로> | claude-docs:<url>")
    p.add_argument("--slug", help="파일명용 영문 슬러그")
    p.add_argument("--body", help="본문 (생략 시 안내 주석만 넣고 이후 직접 편집)")
    p.add_argument("--global", dest="is_global", action="store_true", help="전역 항목으로 추가")
    p = kn.add_parser("list", help="항목 목록 (프로젝트 + 전역)")
    p.add_argument("--all", action="store_true", help="대체·폐기·삭제된 항목 포함")
    p = kn.add_parser("search", help="항목 검색")
    p.add_argument("terms", nargs="*")
    p.add_argument("--path", help="이 파일에 해당하는 항목 (paths glob 매칭)")
    p.add_argument("--all", action="store_true", help="대체·폐기·삭제된 항목 포함")
    p.add_argument("--limit", type=int, default=10, help="검색어 결과 최대 건수 (--path 에는 적용하지 않음)")
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

    args = ap.parse_args(argv)
    handlers = {
        "where": cmd_where, "project-init": cmd_project_init, "new": cmd_new,
        "link": cmd_link, "relate": cmd_relate, "focus": cmd_focus, "status": cmd_status, "migrate": cmd_migrate, "close": cmd_close, "reopen": cmd_reopen,
        "delete": cmd_delete, "restore": cmd_restore,
        "list": cmd_list, "index": cmd_index, "hook": lambda a: cmd_hook(a.event), "doctor": cmd_doctor,
    }
    kn_handlers = {
        "add": cmd_kn_add, "list": cmd_kn_list, "search": cmd_kn_search,
        "supersede": cmd_kn_supersede, "retire": cmd_kn_retire, "delete": cmd_kn_delete,
        "verify": cmd_kn_verify, "stale": cmd_kn_stale,
    }
    try:
        if args.cmd == "kn":
            kn_handlers[args.kn_cmd](args)
        else:
            handlers[args.cmd](args)
    except DeskError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0
