"""CLI commands and the argparse tree."""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path

from .core import (
    CLOSED_STATUSES, DEFAULT_SHARED_BRANCHES, DeskError, FILE_SIZE_LIMIT, FILES_DIR, LEGACY_STATUSES, OPEN_STATUSES,
    PYTHON_ALIAS_MESSAGE, SCHEMA_VERSION, TASK_FILES, desk_root, ensure_root, now_minute, python_alias,
    move_dir, read_text, retry_busy, root_lock, schema_version, today, write_bytes, write_schema_version, write_text,
)
from .frontmatter import format_scalar, format_value, parse_frontmatter, split_frontmatter, update_frontmatter
from .gitinfo import RepoInfo, is_under, native_path, normalize_path, repo_info
from .model import (
    Project, TRASH_DIR, TRASH_LOG_LINE, append_log, append_log_entry, attachments, build_id, check_branch_linkable, children_of, current_project,
    decision_numbers, decision_problems, env_session_id, find_trashed, get_project, get_session_task, get_task,
    load_projects, load_tasks, load_trash, main_worktree, match_project, projects_dir, render_template,
    resolve_parent, set_meta, set_session_task, task_dir_of, task_list_field, tasks_on_branch, touch_task, trashed_at,
)
from .index import write_index
from .doctor import doctor_tasks, project_repo
from . import HOOK_EVENTS
from .hooks import cmd_hook, task_context, task_records


def out(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def try_project(root: Path, args) -> tuple[Project | None, RepoInfo]:
    info = repo_info(Path(args.cwd))
    if args.project:
        return get_project(root, args.project), info
    return match_project(root, info), info


def split_csv(value: str | None) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()] if value else []


def cmd_where(args) -> None:
    root = desk_root()
    info = repo_info(Path(args.cwd))
    project = match_project(root, info) if (root / "projects").is_dir() else None
    focus = get_session_task(project, env_session_id()) if project else None
    branch_tasks = tasks_on_branch(project, info.branch) if project and info.branch else []
    out({
        "root": str(root),
        "repo": str(info.toplevel) if info.toplevel else None,
        "branch": info.branch,
        "project": project.key if project else None,
        "project_dir": str(project.dir) if project else None,
        "session": env_session_id(),
        "focus": focus.id if focus else None,
        "focus_dir": str(focus.dir) if focus else None,
        "branch_tasks": [t.id for t in branch_tasks],
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
    project, _ = current_project(root, Path(args.cwd), args.project)
    # only creates the records: starting the work is `focus`, and a branch is linked only when named
    branch = args.branch
    if branch:
        check_branch_linkable(project, branch)
    parent = resolve_parent(project, args.parent) if args.parent else None
    task_id = build_id(project, args.slug)
    tdir = project.dir / "tasks" / task_id
    values = {
        "id": task_id, "title": format_scalar(args.title), "project": project.key,
        "branches": format_value([branch] if branch else []),
        "parent": parent.id if parent else "",
        "status": "todo",
        "date": today(), "datetime": now_minute(),
    }
    for name in TASK_FILES:
        write_text(tdir / name, render_template(project.lang, name, values))
    write_index(project)
    out({"task": task_id, "dir": str(tdir), "branch": branch,
         "parent": parent.id if parent else None})


def cmd_focus(args) -> None:
    """Choose the task this session works on (its focus) and print its records (text, not JSON)."""
    root = ensure_root()
    project, info = current_project(root, Path(args.cwd), args.project)
    if not args.id and not args.clear:
        raise DeskError("포커스할 태스크 ID를 지정하세요 (해제는 --clear)")
    task = None if args.clear else get_task(project, args.id)
    if task and (task.archived or task.status not in OPEN_STATUSES):
        raise DeskError(f"열린 태스크만 포커스할 수 있습니다: {task.id} ({task.status}). 종료된 태스크는 adesk reopen 으로 먼저 재개하세요"
                        f"(읽기만 하려면 adesk show {task.id})")
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


def cmd_show(args) -> None:
    """Print a task's records (text, not JSON) without touching the focus, status or log:
    open, closed and trashed tasks alike."""
    root = ensure_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    task = next((t for t in load_tasks(project, include_archive=True) if t.id.casefold() == args.id.casefold()),
                None) or find_trashed(project, args.id) or get_task(project, args.id)  # get_task: not found at all
    where = "휴지통" if task.trashed else "아카이브" if task.archived else "열린 태스크"
    parts = [f"[agent-desk] 프로젝트 {project.key} · 태스크 {task.id} ({task.status}, {where}) · 읽기 전용(포커스는 바뀌지 않음)",
             f"태스크 폴더: {task.dir.as_posix()}"]
    print("\n".join(parts + task_records(project, task)))


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
    check_branch_linkable(project, branch)
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
        out({"task": trashed.id, "status": "purged", "note": PURGE_NOTE})
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


LINKS_HEADING = "## 링크"


def add_link_items(text: str, items: list[str]) -> str:
    """Adds items to the end of the `## 링크` section of a state.md, appending the section if missing."""
    lines = text.rstrip("\n").split("\n")
    if LINKS_HEADING not in lines:
        return "\n".join(lines + ["", LINKS_HEADING, ""] + items) + "\n"
    start = lines.index(LINKS_HEADING)
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    if end == start + 1:
        items = [""] + items
    return "\n".join(lines[:end] + items + lines[end:]) + "\n"


def migrate_link_fields(root: Path, dry_run: bool) -> list[dict]:
    """Schema 2 → 3: `links`, `design_doc` and `related` move from the frontmatter to the `## 링크` section of the body."""
    fields = ("links", "design_doc", "related")
    changed = []
    for p in load_projects(root):
        for t in load_tasks(p, include_archive=True) + load_trash(p):
            if not any(f in t.meta for f in fields):
                continue
            items = (([f"- 설계 문서: {t.meta['design_doc']}"] if t.meta.get("design_doc") else [])
                     + [f"- {url}" for url in task_list_field(t, "links")]
                     + [f"- 연관 태스크: {rid}" for rid in task_list_field(t, "related")])
            if items:
                changed.append({"project": p.key, "task": t.id, "moved": items})
            if not dry_run:
                text = update_frontmatter(read_text(t.state_path), dict.fromkeys(fields))
                write_text(t.state_path, add_link_items(text, items) if items else text)
    return changed


MIGRATIONS = {1: migrate_task_statuses, 2: migrate_link_fields}  # from version N to N + 1


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
    out([str(write_index(p)) for p in projects])


PURGE_NOTE = "데이터 폴더를 동기화하거나 백업하고 있다면 그 버전 기록에는 이전 내용이 남아 있을 수 있다"


def desk_target(root: Path, raw: str, cwd: Path) -> Path:
    """The existing data root file a put/append names: absolute, or relative to the data root
    (then to the current directory). Anything outside the data root is refused."""
    given = Path(native_path(raw)).expanduser()
    candidates = [given] if given.is_absolute() else [root / given, cwd / given]
    path = next((p for p in candidates if p.exists()), candidates[0])
    if not is_under(root, path) or normalize_path(path) == normalize_path(root):
        raise DeskError(f"데이터 폴더({root.as_posix()}) 안의 파일만 쓸 수 있습니다: {raw}")
    if not path.is_file():
        raise DeskError(f"데이터 폴더에 없는 파일입니다: {path.as_posix()}. 태스크 파일은 "
                        "`adesk new` 로 만들고, 위치는 `adesk where` 의 task_dir 로 확인하세요")
    path = path.resolve()  # the file's own spelling: what is checked depends on the name (STATE.MD is state.md)
    if path.name == "index.md":
        raise DeskError("index.md 는 자동 생성 파일입니다. `adesk index` 로 다시 만드세요")
    if path.parent.name == FILES_DIR and (path.parent.parent / "state.md").is_file():
        raise DeskError(f"첨부파일은 put·append로 쓰지 않습니다. 고친 파일을 "
                        f"`adesk files {path.parent.parent.name} add <파일> --replace` 로 바꾸세요")
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
    """"state" for the files whose frontmatter only the CLI writes."""
    return "state" if path.name == "state.md" else None


def check_desk_text(path: Path, text: str) -> None:
    """Refuse content the CLI and hooks could not read back: frontmatter of state.md,
    TOML of project.toml and desk.toml."""
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
    # an empty value and a missing key mean the same
    kept = {k: written.get(k) for k in dict.fromkeys([*written, *given])
            if k not in auto and (written.get(k) or "") != (given.get(k) or "")}
    return merged, kept


def auto_updates(path: Path, kind: str | None, meta: dict | None, verified: bool) -> dict:
    """The frontmatter keys the CLI sets itself when it writes the file."""
    if verified and not kind:
        raise DeskError("--verified 는 state.md 에만 쓸 수 있습니다")
    if verified and meta is None:
        raise DeskError(f"{path.name} 의 frontmatter를 읽을 수 없어 --verified 를 적용할 수 없습니다. 먼저 고치세요")
    auto = {}
    if kind == "state" or (meta and "updated" in meta):
        auto["updated"] = today()
    if verified:
        auto["verified"] = now_minute()
    return auto


def refresh_indexes(root: Path, path: Path) -> None:
    """index.md shows titles, statuses and `updated`: rebuild the one the written file feeds."""
    rel = path.resolve().relative_to(root.resolve()).parts
    if rel[0] == "projects" and not (root / "projects" / rel[1] / "project.toml").is_file():
        return
    if rel[0] == "projects" and task_dir_of(path):
        write_index(get_project(root, rel[1]))


KEPT_NOTE = ("frontmatter는 현재 값을 유지했습니다. 바꾸려면 `adesk set`(제목·상위 태스크), `status`·`link` 를 쓰세요")


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
        if path.name == "decisions.md":
            taken = set(decision_numbers(old))
            clash = [n for n in decision_numbers(content) if n in taken]
            if clash:
                # another session added a decision since this one read the list
                raise DeskError(f"결정 번호가 이미 있습니다: {', '.join(f'D{n}' for n in clash)}. 쓰지 않았습니다. "
                                f"다음 번호는 D{max(taken) + 1} 입니다. 번호를 고쳐 다시 덧붙이세요")
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
    if append and path.name == "log.md" and task_dir_of(path):
        warnings.append("log.md 항목은 `adesk log <id> --file <파일|->` 로 덧붙이세요. 시각 제목을 CLI가 현재 시각으로 붙입니다")
    write_text(path, text)
    task_dir = task_dir_of(path)
    if task_dir and kind != "state" and not touch_task(task_dir):
        warnings.append("state.md 의 frontmatter를 읽을 수 없어 updated 를 갱신하지 못했습니다")
    refresh_indexes(root, path)
    out({"file": path.as_posix(), "action": "append" if append else "put", "lines": text.count("\n"),
         **({"kept": kept, "note": KEPT_NOTE} if kept else {}), **{k: auto[k] for k in ("verified",) if k in auto},
         **({"warnings": warnings} if warnings else {})})


LOG_HEADING = re.compile(r"^#{1,6}\s*\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\s*$")


def cmd_log(args) -> None:
    """Add a log entry; the CLI writes the time heading. A time heading the agent wrote out of habit is dropped."""
    root = ensure_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    task = get_task(project, args.id)
    lines = read_content(args.file).splitlines(keepends=True)
    kept = [l for l in lines if not LOG_HEADING.match(l)]
    body = "".join(kept).strip("\n")
    if not body.strip():
        raise DeskError("쓸 내용이 비어 있습니다. 시각 제목은 CLI가 붙이니 본문만 넘기세요")
    stamp = append_log_entry(task, body + "\n")
    warnings = [] if touch_task(task.dir) else ["state.md 의 frontmatter를 읽을 수 없어 updated 를 갱신하지 못했습니다"]
    write_index(project)
    out({"task": task.id, "file": (task.dir / "log.md").as_posix(), "time": stamp,
         **({"note": "시각 제목은 CLI가 붙이므로 넘긴 시각 제목은 뺐습니다"} if len(kept) != len(lines) else {}),
         **({"warnings": warnings} if warnings else {})})


def refuse_option(hint: str):
    """An argparse action for an option an agent may guess here but another command handles (an agent tries the usual names first)."""
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
    if args.parent:
        if children_of(project, task.id):
            raise DeskError(f"{task.id} 에 하위 태스크가 있어 상위 태스크를 지정할 수 없습니다(한 단계만 허용)")
        parent = resolve_parent(project, args.parent, task.id)
        if parent.id != task.meta.get("parent"):
            updates["parent"] = parent.id
            logs.append(f"상위 태스크 지정: {parent.id}")
    elif (args.no_parent or args.parent == "") and task.meta.get("parent"):
        updates["parent"] = ""
        logs.append(f"상위 태스크 해제: {task.meta['parent']}")
    if updates:
        set_meta(task, updates)
        append_log(task, logs)
        write_index(project)
    out({"task": task.id, "title": task.meta.get("title", ""), "parent": task.meta.get("parent") or None,
         "changed": bool(updates)})


FILE_REMOVE_ACTIONS = ("rm", "remove", "delete")


def cmd_files(args) -> None:
    """A task's attachments (files/): list, add (refused over an existing one unless --replace), rm."""
    root = ensure_root() if args.action else desk_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    task = get_task(project, args.id)
    fdir = task.dir / FILES_DIR
    if args.action in (None, "list", "ls"):
        if args.names:
            raise DeskError("목록에는 파일 이름을 지정하지 않습니다. 추가는 `add`, 삭제는 `rm`")
        out({"task": task.id, "dir": fdir.as_posix(),
             "files": [{"name": p.name, "size": p.stat().st_size} for p in attachments(task)]})
        return
    if not args.names:
        raise DeskError(f"{'첨부할 파일' if args.action == 'add' else '지울 첨부파일 이름'}을 지정하세요")
    if args.action == "add":
        add_attachments(task, fdir, args)
    else:
        remove_attachments(task, fdir, args)
    write_index(project)


def add_attachments(task, fdir: Path, args) -> None:
    sources, seen = [], set()
    for raw in args.names:
        src = Path(native_path(raw)).expanduser()
        if not src.is_file():
            raise DeskError(f"파일이 아닙니다: {raw}")
        if src.name.casefold() in seen:
            raise DeskError(f"같은 이름의 파일을 한 번에 첨부할 수 없습니다: {src.name}")
        seen.add(src.name.casefold())
        sources.append(src)
    # every check before the first write: a refused add leaves the attachments as they were
    large = [f"{s.name} ({s.stat().st_size / 1024 / 1024:.1f}MB)" for s in sources if s.stat().st_size > FILE_SIZE_LIMIT]
    if large and not args.large:
        raise DeskError(f"{FILE_SIZE_LIMIT // 1024 // 1024}MB를 넘는 파일입니다: {', '.join(large)}. 데이터 폴더가 동기화될 수 있으니 "
                        "사용자에게 확인한 뒤 --large 로 다시 실행하세요")
    existing = [s.name for s in sources if (fdir / s.name).exists()]
    if existing and not args.replace:
        raise DeskError(f"같은 이름의 첨부파일이 이미 있습니다: {', '.join(existing)}. 바꾸려면 --replace")
    for s in sources:
        try:
            data = s.read_bytes()
        except OSError as e:
            raise DeskError(f"파일을 읽을 수 없습니다: {s} ({e.strerror or e})")
        write_bytes(fdir / s.name, data)  # as given: no BOM or line ending clean-up
    added = [s.name for s in sources if s.name not in existing]
    append_log(task, [f"첨부파일 추가: {n}" for n in added] + [f"첨부파일 교체: {n}" for n in existing])
    touch_task(task.dir)
    out({"task": task.id, "dir": fdir.as_posix(), "added": added, "replaced": existing})


def remove_attachments(task, fdir: Path, args) -> None:
    names = [Path(native_path(n)).name for n in args.names]  # a path to the attachment works as its name
    missing = [n for n in names if not (fdir / n).is_file()]
    if missing:
        raise DeskError(f"없는 첨부파일입니다: {', '.join(missing)}. 목록은 `adesk files {task.id}`")
    for n in names:
        retry_busy((fdir / n).unlink)
    if not any(fdir.iterdir()):
        fdir.rmdir()
    append_log(task, [f"첨부파일 삭제: {n}" for n in names])
    touch_task(task.dir)
    out({"task": task.id, "removed": names})


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
    applied = []
    if args.apply and (project.dir / OBSOLETE_FOCUS_FILE).exists():
        (project.dir / OBSOLETE_FOCUS_FILE).unlink()
        applied.append(OBSOLETE_FOCUS_FILE)
    out({"project": project.key, "repo": top.as_posix() if top else None,
         "issues": issues, "applied": applied})


# commands that never change the data root. Every other command, a new one included, holds the
# data root lock for its whole run. hook only reads (its session cache is in the temp dir)
# the focus file of releases before 0.8 (one current task per project, replaced by the session's task)
OBSOLETE_FOCUS_FILE = "focus.txt"

READ_ONLY_COMMANDS = {"where", "list", "show", "hook"}


def changes_data(args) -> bool:
    name = args.cmd
    if name == "doctor":
        return args.apply  # without --apply it only reads
    if name == "files":
        return args.action not in (None, "list", "ls")
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
    p.add_argument("--shared", help="공용 브랜치 목록 (쉼표 구분)")
    p.add_argument("--lang", default="ko")

    p = sub.add_parser("new", help="태스크 생성 (예정 상태로 기록만 만든다. 착수는 focus)")
    p.add_argument("--title", required=True)
    p.add_argument("--slug", required=True, help="ID 슬러그 (ID는 <yymmdd>-<slug>)")
    p.add_argument("--branch", help="이 태스크의 작업을 할 브랜치에 연결 (ID에는 영향 없음)")
    p.add_argument("--parent", help="상위 태스크 ID (한 단계만 허용)")

    p = sub.add_parser("focus", help="이 세션의 포커스(작업할 태스크)를 정하고 그 기록을 텍스트로 출력 (todo면 진행 중으로)")
    p.add_argument("id", nargs="?")
    p.add_argument("--clear", action="store_true", help="이 세션의 포커스 해제")

    p = sub.add_parser("show", help="태스크 기록을 텍스트로 출력 (포커스·상태를 바꾸지 않음, 종료·휴지통 태스크 포함)")
    p.add_argument("id")

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

    sub.add_parser("index", help="태스크 index.md 재생성")

    p = sub.add_parser("set", help="태스크의 제목·상위 태스크 변경",
                       description="상태는 status·close, 브랜치는 link 로 바꾼다.")
    p.add_argument("id")
    p.add_argument("--title", help="제목 (오타 수정용. 일의 범위가 바뀌면 새 태스크)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--parent", help="상위 태스크 지정·변경 (한 단계만 허용, 빈 값이면 해제)")
    g.add_argument("--no-parent", action="store_true", help="상위 태스크 해제")
    for opts, hint in ((("--status",), "`adesk status <id> <상태>`, 종료는 `adesk close`"),
                       (("--branch", "--branches"), "`adesk link <id> --branch <브랜치>`")):
        p.add_argument(*opts, nargs="?", action=refuse_option(hint), help=argparse.SUPPRESS)

    p = sub.add_parser("files", help="태스크 첨부파일(files/) 목록·추가·삭제",
                       description="첨부파일은 받은 그대로 저장하고, 고친 첨부파일은 add --replace 로 바꾼다.")
    p.add_argument("id")
    p.add_argument("action", nargs="?", choices=("list", "ls", "add", *FILE_REMOVE_ACTIONS),
                   help="생략하면 목록. add: 파일 첨부, rm: 첨부파일 삭제(되살릴 수 없음)")
    p.add_argument("names", nargs="*", help="add: 첨부할 파일 경로, rm: 지울 첨부파일 이름")
    p.add_argument("--replace", action="store_true", help="add: 같은 이름의 첨부파일을 바꾼다")
    p.add_argument("--large", action="store_true",
                   help=f"add: {FILE_SIZE_LIMIT // 1024 // 1024}MB를 넘는 파일도 첨부한다 (사용자 확인 뒤)")

    p = sub.add_parser("log", help="태스크 log.md에 항목 추가 (시각 제목은 CLI가 붙임)")
    p.add_argument("id")
    p.add_argument("--file", "-f", required=True, help="항목 본문(bullet)이 든 파일 (- 이면 표준 입력)")

    for name, help_text in (("put", "데이터 폴더 파일을 --file 내용으로 교체 (frontmatter는 현재 값 유지)"),
                            ("append", "데이터 폴더 파일 끝에 --file 내용을 덧붙임")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("path", help="데이터 폴더 안의 파일 (절대 경로 또는 데이터 폴더 기준 상대 경로)")
        p.add_argument("--file", "-f", required=True, help="쓸 내용이 든 파일 (- 이면 표준 입력)")
        if name == "put":
            p.add_argument("--verified", action="store_true",
                           help="내용을 코드와 대조해 확인했음을 기록 (state.md)")

    p = sub.add_parser("migrate", help="데이터 폴더를 현재 스키마로 변환")
    p.add_argument("--dry-run", action="store_true", help="바꿀 내용만 출력")

    p = sub.add_parser("doctor", help="데스크 점검 (충돌 사본, frontmatter 오류, 방치된 태스크)")
    p.add_argument("--apply", action="store_true", help="이전 버전이 남긴 파일(프로젝트 공용 포커스 파일)을 지운다")

    p = sub.add_parser("hook", help="Claude Code hook 진입점")
    p.add_argument("event", choices=HOOK_EVENTS)

    command = next((a for i, a in enumerate(argv) if not a.startswith("-")
                    and (i == 0 or argv[i - 1] not in ("--cwd", "--project"))), None)
    if command in ("open", "use", "switch"):  # guesses for focus
        argv = [*argv[:argv.index(command)], "focus", *argv[argv.index(command) + 1:]]
    if command == "relate":  # removed in 0.12: the parent is set by `set`
        ap.exit(2, "오류: relate 는 없습니다. 상위 태스크는 `adesk set <id> --parent <P>`·`--no-parent` 로 바꾸세요\n")
    args = ap.parse_args(argv)
    handlers = {
        "where": cmd_where, "project-init": cmd_project_init, "new": cmd_new, "set": cmd_set,
        "link": cmd_link, "unlink": cmd_unlink, "focus": cmd_focus, "show": cmd_show, "status": cmd_status, "migrate": cmd_migrate, "close": cmd_close, "reopen": cmd_reopen,
        "delete": cmd_delete, "restore": cmd_restore, "list": cmd_list, "index": cmd_index, "files": cmd_files, "log": cmd_log,
        "put": lambda a: write_desk_file(a, append=False), "append": lambda a: write_desk_file(a, append=True), "hook": lambda a: cmd_hook(a.event), "doctor": cmd_doctor,
    }
    handler = handlers[args.cmd]
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
