"""Projects, tasks, task relations (parent/related) and the focus task."""

from __future__ import annotations

import datetime as dt
import re
import tomllib
from pathlib import Path

from .core import (
    DEFAULT_ID_TEMPLATE, DEFAULT_SHARED_BRANCHES, DeskError, LEGACY_STATUSES, OPEN_STATUSES, PLUGIN_ROOT, TASK_FILES,
    now_minute, read_text, today, write_text,
)
from .frontmatter import parse_frontmatter, update_frontmatter
from .gitinfo import RepoInfo, normalize_path, normalize_remote, repo_info


class Project:
    def __init__(self, key: str, dir: Path, config: dict):
        self.key, self.dir, self.config = key, dir, config

    @property
    def id_template(self) -> str:
        return self.config.get("id_template", DEFAULT_ID_TEMPLATE)

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
    def __init__(self, id: str, dir: Path, meta: dict, archived: bool):
        self.id, self.dir, self.meta, self.archived = id, dir, meta, archived

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


def load_tasks(project: Project, include_archive: bool = False) -> list[Task]:
    tasks = []
    sources = [("tasks", False)] + ([("archive", True)] if include_archive else [])
    for sub, archived in sources:
        base = project.dir / sub
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            state = d / "state.md"
            if not state.is_file():
                continue
            try:
                meta = parse_frontmatter(read_text(state))
            except DeskError as err:
                meta = {"status": "?", "title": "(frontmatter 오류)", "error": str(err)}
            tasks.append(Task(d.name, d, meta, archived))
    return tasks


def get_task(project: Project, task_id: str) -> Task:
    for t in load_tasks(project, include_archive=True):
        if t.id.casefold() == task_id.casefold():
            return t
    raise DeskError(f"태스크를 찾을 수 없습니다: {project.key}/{task_id}")


def find_task_by_branch(project: Project, branch: str) -> Task | None:
    for t in load_tasks(project):
        if t.status in OPEN_STATUSES and branch.casefold() in (b.casefold() for b in t.branches):
            return t
    return None


_INVALID = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def sanitize(name: str) -> str:
    s = name.replace("/", "__")
    s = _INVALID.sub("_", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s.rstrip(". ") or "task"


def build_id(project: Project, branch: str | None, slug: str | None) -> str:
    d = dt.date.today()
    if branch:
        raw = project.id_template.format(
            branch=branch, slug=slug or branch,
            yymmdd=d.strftime("%y%m%d"), yyyymmdd=d.strftime("%Y%m%d"))
    else:
        if not slug:
            raise DeskError("브랜치 없이 만드는 태스크에는 --slug 가 필요합니다")
        raw = f"{d.strftime('%y%m%d')}-{slug}"
    base = sanitize(raw)
    taken = {t.id.casefold() for t in load_tasks(project, include_archive=True)}
    if base.casefold() not in taken:
        return base
    n = 2
    while f"{base}~{n}".casefold() in taken:
        n += 1
    return f"{base}~{n}"


def check_branch_free(project: Project, branch: str, task_id: str | None = None) -> None:
    if project.is_shared(branch):
        raise DeskError(f"공용 브랜치는 태스크에 연결할 수 없습니다: {branch}")
    # an unreadable state.md hides its branches, so this branch may already be taken
    broken = [t.id for t in load_tasks(project) if t.status == "?"]
    if broken:
        raise DeskError(f"state.md frontmatter를 읽을 수 없는 태스크가 있어 브랜치 중복을 확인할 수 없습니다: "
                        f"{', '.join(broken)}. 먼저 고치세요(doctor로 위치 확인)")
    owner = find_task_by_branch(project, branch)
    if owner and (task_id is None or owner.id.casefold() != task_id.casefold()):
        raise DeskError(f"브랜치 {branch} 는 이미 진행 중인 태스크 {owner.id} 에 연결되어 있습니다")


def render_template(lang: str, name: str, values: dict) -> str:
    path = PLUGIN_ROOT / "templates" / lang / name
    if not path.is_file():
        path = PLUGIN_ROOT / "templates" / "ko" / name
    text = read_text(path)
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return re.sub(r"[ \t]+$", "", text, flags=re.M)


def append_log(task: Task, lines: list[str]) -> None:
    path = task.dir / "log.md"
    text = read_text(path) if path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    text += f"\n## {now_minute()}\n" + "".join(f"- {l}\n" for l in lines)
    write_text(path, text)


def set_meta(task: Task, updates: dict) -> None:
    updates = {**updates, "updated": today()}
    write_text(task.state_path, update_frontmatter(read_text(task.state_path), updates))
    task.meta.update(updates)


# A task may have one parent (one level only: a parent cannot itself have a parent)
# and a free list of related tasks. Both are stored by task id.

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


# The project's current task, independent of branches. A session uses the task
# linked to the current branch first; the focused task only when there is none
# (typically on a shared branch such as main in main-only projects).

FOCUS_FILE = "focus.txt"


def get_focus_id(project: Project) -> str | None:
    try:
        value = read_text(project.dir / FOCUS_FILE).strip()
    except OSError:
        return None
    return value or None


def get_focus_task(project: Project) -> Task | None:
    focus_id = get_focus_id(project)
    if not focus_id:
        return None
    try:
        task = get_task(project, focus_id)
    except DeskError:
        return None
    return task if not task.archived and task.status in OPEN_STATUSES else None


def set_focus(project: Project, task_id: str | None) -> None:
    path = project.dir / FOCUS_FILE
    if task_id:
        write_text(path, task_id + "\n")
    elif path.exists():
        path.unlink()


def resolve_session_task(project: Project, branch: str | None) -> tuple[Task | None, str]:
    """The task a session works on: ("branch" | "focus" | "") source."""
    if branch:
        task = find_task_by_branch(project, branch)
        if task:
            return task, "branch"
    task = get_focus_task(project)
    return (task, "focus") if task else (None, "")


def stray_files(task: Task) -> list[str]:
    return sorted(p.name for p in task.dir.iterdir() if p.is_file() and p.name not in TASK_FILES)
