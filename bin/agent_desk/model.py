"""Projects, tasks, the parent relation and the task a session works on."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tomllib
from pathlib import Path

from .core import (
    DEFAULT_SHARED_BRANCHES, DeskError, FILES_DIR, LEGACY_STATUSES, OPEN_STATUSES, PLUGIN_ROOT, TASK_FILES,
    is_write_temp, now_minute, read_text, today, write_text,
)
from .frontmatter import parse_frontmatter, update_frontmatter
from .gitinfo import RepoInfo, normalize_path, normalize_remote, repo_info


class Project:
    def __init__(self, key: str, dir: Path, config: dict):
        self.key, self.dir, self.config = key, dir, config

    @property
    def shared_branches(self) -> list[str]:
        return self.config.get("shared_branches", DEFAULT_SHARED_BRANCHES)

    @property
    def lang(self) -> str:
        return self.config.get("lang", "ko")

    def is_shared(self, branch: str) -> bool:
        return branch.casefold() in (b.casefold() for b in self.shared_branches)


def projects_dir(root: Path) -> Path:
    return root / "projects"


def load_projects(root: Path) -> list[Project]:
    result = []
    pdir = projects_dir(root)
    if not pdir.is_dir():
        return result
    for d in sorted(pdir.iterdir()):
        cfg = d / "project.toml"
        if cfg.is_file():
            result.append(Project(d.name, d, tomllib.loads(read_text(cfg))))
    return result


def git_dir(top: Path) -> Path | None:
    """The git directory of a checkout: <top>/.git, or where a .git file points
    (linked worktree, submodule). Reads the file, no git call."""
    dot_git = top / ".git"
    if dot_git.is_dir():
        return dot_git
    try:
        line = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = Path(line[len("gitdir:"):].strip())
    return gitdir if gitdir.is_absolute() else top / gitdir


def main_worktree(top: Path) -> Path:
    """The main checkout of a linked worktree (whose git dir is <main>/.git/worktrees/<name>);
    `top` itself otherwise."""
    gitdir = git_dir(top)
    if not gitdir or gitdir.parent.name != "worktrees":  # main checkout, or e.g. a submodule
        return top
    return gitdir.parent.parent.parent


def head_branch(top: Path) -> str | None:
    """Current branch read from HEAD without running git; a short SHA when detached."""
    gitdir = git_dir(top)
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip() if gitdir else ""
    except OSError:
        return None
    if head.startswith("ref: "):
        return head[5:].removeprefix("refs/heads/")
    return head[:12] or None


def match_project(root: Path, info: RepoInfo) -> Project | None:
    if not info.toplevel:
        return None
    remotes = {normalize_remote(r) for r in info.remotes}
    # a linked worktree also matches by its main checkout's path
    tops = {normalize_path(info.toplevel), normalize_path(main_worktree(info.toplevel))}
    for p in load_projects(root):
        if remotes & {normalize_remote(r) for r in p.config.get("remotes", [])}:
            return p
    for p in load_projects(root):
        if tops & {normalize_path(x) for x in p.config.get("paths", [])}:
            return p
    return None


def get_project(root: Path, key: str) -> Project:
    for p in load_projects(root):
        if p.key.casefold() == key.casefold():
            return p
    raise DeskError(f"등록되지 않은 프로젝트: {key}")


def current_project(root: Path, cwd: Path, key: str | None) -> tuple[Project, RepoInfo]:
    info = repo_info(cwd)
    if key:
        return get_project(root, key), info
    project = match_project(root, info)
    if not project:
        raise DeskError("현재 저장소가 등록된 프로젝트와 일치하지 않습니다. adesk project-init 으로 등록하세요")
    return project, info


class Task:
    def __init__(self, id: str, dir: Path, meta: dict, archived: bool, trashed: bool = False):
        self.id, self.dir, self.meta, self.archived, self.trashed = id, dir, meta, archived, trashed

    @property
    def state_path(self) -> Path:
        return self.dir / "state.md"

    @property
    def status(self) -> str:
        status = self.meta.get("status", "")
        return LEGACY_STATUSES.get(status, status)

    @property
    def branches(self) -> list[str]:
        b = self.meta.get("branches", [])
        return b if isinstance(b, list) else [b] if b else []


# Deleting a task moves its folder to <project>/trash/<id>. Everything that reads tasks
# (lists, session injection, index, branch matching, session task) looks at tasks/ and archive/
# only, so a trashed task is simply gone until it is restored; references to it are left
# as they are and read as a missing task.
TRASH_DIR = "trash"
TRASH_LOG_LINE = "휴지통으로 옮김"


def _scan(base: Path, archived: bool, trashed: bool = False) -> list[Task]:
    tasks = []
    if not base.is_dir():
        return tasks
    for d in sorted(base.iterdir()):
        state = d / "state.md"
        if not state.is_file():
            continue
        try:
            meta = parse_frontmatter(read_text(state))
        except DeskError as err:
            meta = {"status": "?", "title": "(frontmatter 오류)", "error": str(err)}
        tasks.append(Task(d.name, d, meta, archived, trashed))
    return tasks


def load_tasks(project: Project, include_archive: bool = False) -> list[Task]:
    return _scan(project.dir / "tasks", False) + (_scan(project.dir / "archive", True) if include_archive else [])


def load_trash(project: Project) -> list[Task]:
    return _scan(project.dir / TRASH_DIR, False, trashed=True)


def find_trashed(project: Project, task_id: str) -> Task | None:
    for t in load_trash(project):
        if t.id.casefold() == task_id.casefold():
            return t
    return None


def trashed_at(task: Task) -> str | None:
    """When the task was moved to the trash (`trashed` in state.md, written by delete)."""
    return task.meta.get("trashed") or None


def get_task(project: Project, task_id: str) -> Task:
    for t in load_tasks(project, include_archive=True):
        if t.id.casefold() == task_id.casefold():
            return t
    trashed = find_trashed(project, task_id)
    if trashed:
        raise DeskError(f"휴지통에 있는 태스크입니다: {trashed.id}. 되살리려면 adesk restore {trashed.id}")
    raise DeskError(f"태스크를 찾을 수 없습니다: {project.key}/{task_id}")


def tasks_on_branch(project: Project, branch: str) -> list[Task]:
    """The open tasks whose work happens on the branch. A branch may carry several."""
    return [t for t in load_tasks(project)
            if t.status in OPEN_STATUSES and branch.casefold() in (b.casefold() for b in t.branches)]


_INVALID = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def sanitize(name: str) -> str:
    s = name.replace("/", "__")
    s = _INVALID.sub("_", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s.rstrip(". ") or "task"


def build_id(project: Project, slug: str) -> str:
    # never from a branch name: a branch may carry several tasks and is often linked after creation.
    # id_template in a project.toml of releases before 0.11 is ignored
    base = sanitize(f"{dt.date.today().strftime('%y%m%d')}-{slug}")
    taken = {t.id.casefold() for t in load_tasks(project, include_archive=True)}
    # a trashed task keeps its ID, so recreating a deleted task gets ~2 rather than an error
    taken |= {t.id.casefold() for t in load_trash(project)}
    if base.casefold() not in taken:
        return base
    n = 2
    while f"{base}~{n}".casefold() in taken:
        n += 1
    return f"{base}~{n}"


def check_branch_linkable(project: Project, branch: str) -> None:
    # every task on a shared branch would be a candidate in every session there: the link says nothing
    if project.is_shared(branch):
        raise DeskError(f"공용 브랜치는 태스크에 연결할 수 없습니다: {branch}")


def render_template(lang: str, name: str, values: dict) -> str:
    path = PLUGIN_ROOT / "templates" / lang / name
    if not path.is_file():
        path = PLUGIN_ROOT / "templates" / "ko" / name
    text = read_text(path)
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return re.sub(r"[ \t]+$", "", text, flags=re.M)


def append_log(task: Task, lines: list[str]) -> None:
    append_log_entry(task, "".join(f"- {l}\n" for l in lines))


def append_log_entry(task: Task, body: str) -> str:
    """Add an entry under a heading with the current time (the agent cannot know it); returns that time."""
    path = task.dir / "log.md"
    text = read_text(path) if path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    stamp = now_minute()
    text += f"\n## {stamp}\n" + body
    write_text(path, text)
    return stamp


def set_meta(task: Task, updates: dict) -> None:
    updates = {**updates, "updated": today()}
    write_text(task.state_path, update_frontmatter(read_text(task.state_path), updates))
    task.meta.update(updates)
    for k in [k for k, v in updates.items() if v is None]:
        del task.meta[k]


def task_dir_of(path: Path) -> Path | None:
    """The task folder a task file (state.md, decisions.md, log.md) belongs to, None otherwise."""
    d = path.parent
    if path.name in TASK_FILES and d.parent.name in ("tasks", "archive") and (d / "state.md").is_file():
        return d
    return None


def touch_task(task_dir: Path) -> bool:
    """Set the task's `updated` to today after the CLI wrote one of its files. False when its
    state.md frontmatter cannot be read (then it is left as it is)."""
    state = task_dir / "state.md"
    try:
        text = read_text(state)
        if parse_frontmatter(text).get("updated") == today():
            return True
        write_text(state, update_frontmatter(text, {"updated": today()}))
    except (DeskError, OSError):
        return False
    return True


# Decision headings: the one line of agent-written text the CLI reads (session injection lists
# them), so writes of decisions.md check them. Examples inside <!-- --> are not entries.
DECISION_LINE = re.compile(r"^## (D\d+\..*)$", re.M)
DECISION_LIKE = re.compile(r"#{1,6}[ \t]*D(\d+)")  # a heading meant as a decision, well formed or not
# a note may follow the parenthesis (`→ K0012`: releases before 0.10 pointed to the knowledge entry it moved to)
DECISION_HEAD = re.compile(r"## D(\d+)\. \S.*  \(\d{4}-\d{2}-\d{2}, [^)\s][^)]*\)(?:\s+→\s*\S.*)?")


def strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def decision_numbers(text: str) -> list[int]:
    """The numbers of the decision headings in text, well formed or not, in order."""
    return [int(m.group(1)) for line in strip_comments(text).splitlines() if (m := DECISION_LIKE.match(line))]


def decision_problems(text: str, before: str = "") -> list[str]:
    """Warnings about decision headings: the format `## D<n>. 제목  (YYYY-MM-DD, 상태)`, numbers
    in order without a repeat, and numbers in before (the file as it was) that text dropped.
    A gap left by a decision the user deleted is not a problem: numbers are never reused."""
    problems, numbers = [], []
    for line in strip_comments(text).splitlines():
        like = DECISION_LIKE.match(line)
        if not like:
            continue
        numbers.append(int(like.group(1)))
        if not DECISION_HEAD.fullmatch(line.rstrip()):
            problems.append(f"결정 머리 형식이 아닙니다(`## D<번호>. 제목  (YYYY-MM-DD, 상태)`, "
                            f"제목과 괄호 사이는 공백 두 칸): {line.strip()}")
    seen = set()
    for n in numbers:
        if n in seen:
            problems.append(f"결정 번호가 겹칩니다: D{n}")
        seen.add(n)
    lost = [n for n in dict.fromkeys(decision_numbers(before)) if n not in seen]
    if lost:
        problems.append(f"이번 쓰기로 결정이 사라졌습니다: {', '.join(f'D{n}' for n in lost)}. "
                        "사용자가 지우라고 한 것이 아니면 되살리세요")
    if numbers != sorted(numbers):
        problems.append(f"결정 번호가 순서대로가 아닙니다: {', '.join(f'D{n}' for n in numbers)}")
    return problems


# A task may have one parent (one level only: a parent cannot itself have a parent), stored by task id.

def task_list_field(task: Task, key: str) -> list[str]:
    v = task.meta.get(key, [])
    return v if isinstance(v, list) else [v] if v else []


def children_of(project: Project, task_id: str, open_only: bool = False) -> list[Task]:
    return [t for t in load_tasks(project, include_archive=not open_only)
            if (t.meta.get("parent") or "").casefold() == task_id.casefold()
            and (not open_only or t.status in OPEN_STATUSES)]


def resolve_parent(project: Project, parent_id: str, child_id: str | None = None) -> Task:
    parent = get_task(project, parent_id)
    if child_id and parent.id.casefold() == child_id.casefold():
        raise DeskError("자기 자신을 상위 태스크로 지정할 수 없습니다")
    if parent.meta.get("parent"):
        raise DeskError(f"상위 태스크는 한 단계만 둘 수 있습니다: {parent.id} 는 이미 {parent.meta['parent']} 의 하위 태스크입니다")
    return parent


# The task a session works on, its focus, is the one the user chose in it (`adesk focus`), kept per Claude
# Code session id in the local session cache, never in the synced data root: other sessions and
# machines work on other tasks. The id survives compact and resume and changes on /clear.
# Subagents share the session id, so they do not change it (desk skill).

def session_dir() -> Path:
    # same lookup order as tempfile.gettempdir(), without importing it (pulls in shutil, random)
    tmp = os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP")
    if not tmp:
        import tempfile
        tmp = tempfile.gettempdir()
    return Path(tmp) / "agent-desk-sessions"


def safe_name(name: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name or "unknown")


def env_session_id() -> str | None:
    """The Claude Code session a CLI call runs in (set for the Bash tool), None outside one."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def session_tasks(session_id: str | None) -> dict[str, str]:
    """{project key: task id} chosen in the session."""
    if not session_id:
        return {}
    try:
        data = json.loads(read_text(session_dir() / safe_name(session_id) / "task.json"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}


def set_session_task(session_id: str, project: Project, task_id: str | None) -> None:
    tasks = session_tasks(session_id)
    if task_id:
        tasks[project.key] = task_id
    else:
        tasks.pop(project.key, None)
    write_text(session_dir() / safe_name(session_id) / "task.json", json.dumps(tasks, ensure_ascii=False) + "\n")


def get_session_task(project: Project, session_id: str | None) -> Task | None:
    """The open task chosen in the session for this project; None when none was chosen or it
    has since been closed, trashed or removed."""
    task_id = session_tasks(session_id).get(project.key)
    if not task_id:
        return None
    try:
        task = get_task(project, task_id)
    except DeskError:
        return None
    return task if not task.archived and task.status in OPEN_STATUSES else None


def stray_files(task: Task) -> list[str]:
    """What the task folder holds besides its files and the attachments folder (a folder ends with /)."""
    return sorted(p.name + "/" if p.is_dir() else p.name for p in task.dir.iterdir()
                  if not (p.is_file() and (p.name in TASK_FILES or is_write_temp(p.name)))
                  and not (p.is_dir() and p.name == FILES_DIR))


def attachments(task: Task) -> list[Path]:
    """The files in the task's attachments folder (`adesk files`)."""
    d = task.dir / FILES_DIR
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_file() and not is_write_temp(p.name)),
                  key=lambda p: p.name.casefold())
