#!/usr/bin/env python3
"""agent-desk CLI: deterministic operations on the agent work-record store.

Data root: $AGENT_DESK_ROOT, or ~/agent-desk when unset.
Requires Python 3.11+ (tomllib). Standard library only.
"""

from __future__ import annotations

# argparse, shutil and subprocess are imported where used, and dataclasses/tempfile are avoided:
# hooks run on every tool use and each import adds startup time.
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path

SCHEMA_VERSION = 1
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TASK_FILES = ("state.md", "decisions.md", "log.md")
OPEN_STATUSES = ("active", "paused")
ALL_STATUSES = ("active", "paused", "done", "abandoned")
DEFAULT_ID_TEMPLATE = "{yymmdd}-{branch}"
DEFAULT_SHARED_BRANCHES = ["main", "master", "develop"]


class DeskError(Exception):
    pass


# ---------------------------------------------------------------- paths / io

def desk_root() -> Path:
    env = os.environ.get("AGENT_DESK_ROOT")
    return Path(env).expanduser() if env else Path.home() / "agent-desk"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def ensure_root() -> Path:
    root = desk_root()
    marker = root / "desk.toml"
    if not marker.exists():
        write_text(marker, f"schema_version = {SCHEMA_VERSION}\n")
    else:
        version = tomllib.loads(read_text(marker)).get("schema_version")
        if version != SCHEMA_VERSION:
            raise DeskError(
                f"데이터 스키마 버전 불일치: 데이터={version}, 플러그인={SCHEMA_VERSION} ({marker})")
    return root


def today() -> str:
    return dt.date.today().isoformat()


def now_minute() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- frontmatter
# Restricted YAML subset: `key: value` and flow lists `[a, "b, c"]`.

def _parse_list(raw: str) -> list[str]:
    items, buf, quoted, i = [], "", False, 0
    body = raw.strip()[1:-1]
    while i < len(body):
        c = body[i]
        if c == '"':
            quoted = not quoted
        elif c == "," and not quoted:
            items.append(buf.strip())
            buf = ""
        else:
            buf += c
        i += 1
    if buf.strip():
        items.append(buf.strip())
    return [x for x in items if x]


def _format_value(value) -> str:
    if isinstance(value, list):
        def q(s: str) -> str:
            return f'"{s}"' if any(ch in s for ch in ',[]"') else s
        return "[" + ", ".join(q(str(v).replace('"', "'")) for v in value) + "]"
    return str(value)


def split_frontmatter(text: str) -> tuple[list[str], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise DeskError("frontmatter가 없습니다")
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            return lines[1:end], "\n".join(lines[end + 1:])
    raise DeskError("frontmatter가 닫히지 않았습니다")


def parse_frontmatter(text: str) -> dict:
    fm_lines, _ = split_frontmatter(text)
    data = {}
    for line in fm_lines:
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, raw = line.partition(":")
        # an inline comment needs two spaces before `#`, so a title like "PR #12" survives
        raw = re.split(r"\s{2,}#", raw, maxsplit=1)[0].strip()
        data[key.strip()] = _parse_list(raw) if raw.startswith("[") and raw.endswith("]") else raw
    return data


def update_frontmatter(text: str, updates: dict) -> str:
    fm_lines, body = split_frontmatter(text)
    pending = dict(updates)
    out = []
    for line in fm_lines:
        key = line.partition(":")[0].strip()
        if key in pending:
            out.append(f"{key}: {_format_value(pending.pop(key))}".rstrip())
        else:
            out.append(line)
    out.extend(f"{k}: {_format_value(v)}".rstrip() for k, v in pending.items())
    return "---\n" + "\n".join(out) + "\n---\n" + body


# ---------------------------------------------------------------- git

def git(cwd: Path, *args: str, timeout: float = 5) -> str | None:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def normalize_remote(url: str) -> str:
    u = url.strip()
    u = re.sub(r"^[a-z+]+://", "", u, flags=re.I)
    u = re.sub(r"^[^@/]+@", "", u)
    m = re.match(r"^([^/:]+):(?!\d+/)(.*)$", u)  # scp form host:path
    if m:
        u = f"{m.group(1)}/{m.group(2)}"
    u = re.sub(r"^([^/:]+):\d+/", r"\1/", u)  # drop port
    u = re.sub(r"\.git/?$", "", u).rstrip("/")
    return u.casefold()


def normalize_path(p: str | Path) -> str:
    return str(Path(p).expanduser().resolve()).replace("\\", "/").rstrip("/").casefold()


class RepoInfo:
    def __init__(self, toplevel: Path | None, branch: str | None, remotes: list[str] | None = None):
        self.toplevel, self.branch, self.remotes = toplevel, branch, remotes or []


def repo_info(cwd: Path) -> RepoInfo:
    top = git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return RepoInfo(None, None)
    branch = git(cwd, "symbolic-ref", "--short", "-q", "HEAD")
    remotes = []
    for name in (git(cwd, "remote") or "").split():
        url = git(cwd, "remote", "get-url", name)
        if url:
            remotes.append(url)
    return RepoInfo(Path(top), branch or None, remotes)


# ---------------------------------------------------------------- projects

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
        raise DeskError("현재 저장소가 등록된 프로젝트와 일치하지 않습니다. desk.py project-init 으로 등록하세요")
    return project, info


# ---------------------------------------------------------------- tasks

class Task:
    def __init__(self, id: str, dir: Path, meta: dict, archived: bool):
        self.id, self.dir, self.meta, self.archived = id, dir, meta, archived

    @property
    def state_path(self) -> Path:
        return self.dir / "state.md"

    @property
    def status(self) -> str:
        return self.meta.get("status", "")

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
            except DeskError:
                meta = {"status": "?", "title": "(frontmatter 오류)"}
            tasks.append(Task(d.name, d, meta, archived))
    return tasks


def get_task(project: Project, task_id: str) -> Task:
    for t in load_tasks(project, include_archive=True):
        if t.id.casefold() == task_id.casefold():
            return t
    raise DeskError(f"작업을 찾을 수 없습니다: {project.key}/{task_id}")


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
            raise DeskError("브랜치 없이 만드는 작업에는 --slug 가 필요합니다")
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
        raise DeskError(f"공용 브랜치는 작업에 연결할 수 없습니다: {branch}")
    owner = find_task_by_branch(project, branch)
    if owner and (task_id is None or owner.id.casefold() != task_id.casefold()):
        raise DeskError(f"브랜치 {branch} 는 이미 진행 중인 작업 {owner.id} 에 연결되어 있습니다")


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


# ---------------------------------------------------------------- relations
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
        raise DeskError("자기 자신을 상위 작업으로 지정할 수 없습니다")
    if parent.meta.get("parent"):
        raise DeskError(f"상위 작업은 한 단계만 둘 수 있습니다: {parent.id} 는 이미 {parent.meta['parent']} 의 하위 작업입니다")
    return parent


# ---------------------------------------------------------------- focus
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


def section_line(task: Task, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", read_text(task.state_path), re.M | re.S)
    for line in (m.group(1).splitlines() if m else []):
        s = line.strip().lstrip("-* ").strip()
        if s and not s.startswith("<!--"):
            return s[:200]
    return ""


# ---------------------------------------------------------------- index

def write_index(project: Project) -> Path:
    tasks = load_tasks(project, include_archive=True)
    open_tasks = sorted((t for t in tasks if not t.archived),
                        key=lambda t: t.meta.get("updated", ""), reverse=True)
    closed = sorted((t for t in tasks if t.archived),
                    key=lambda t: t.meta.get("updated", ""), reverse=True)

    focus_id = (get_focus_id(project) or "").casefold()

    def row(t: Task) -> str:
        status = t.status + (" · 포커스" if not t.archived and t.id.casefold() == focus_id else "")
        cells = [t.id, t.meta.get("title", ""), status, ", ".join(t.branches),
                 t.meta.get("parent", "") or "", t.meta.get("updated", "")]
        return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"

    head = "| ID | 제목 | 상태 | 브랜치 | 상위 | 갱신일 |\n|---|---|---|---|---|---|\n"
    text = (f"# {project.key} 작업 목록\n\n"
            "<!-- desk.py index 가 생성하는 파일입니다. 직접 수정하지 마세요. -->\n\n"
            "## 진행\n\n" + (head + "\n".join(row(t) for t in open_tasks) if open_tasks else "(없음)") +
            "\n\n## 아카이브\n\n" + (head + "\n".join(row(t) for t in closed) if closed else "(없음)") + "\n")
    path = project.dir / "index.md"
    write_text(path, text)
    return path


# ---------------------------------------------------------------- knowledge
# Project entries live in projects/<KEY>/knowledge/ (ids K0001...),
# global entries in <root>/knowledge/ (ids G0001...).

KN_KINDS = ("decision", "pitfall", "finding", "howto")
KN_STATUSES = ("valid", "stale", "superseded")


class Entry:
    def __init__(self, id: str, path: Path, meta: dict, body: str, scope: str):
        self.id, self.path, self.meta, self.body = id, path, meta, body
        self.scope = scope  # project key or "global"

    @property
    def status(self) -> str:
        return self.meta.get("status", "valid")

    def items(self, key: str) -> list[str]:
        v = self.meta.get(key, [])
        return v if isinstance(v, list) else [v] if v else []

    def summary(self) -> str:
        text = re.sub(r"<!--.*?-->", "", self.body, flags=re.S)
        for line in text.splitlines():
            line = line.strip().lstrip("#-* ").strip()
            if line:
                return line[:160]
        return ""

    def as_dict(self) -> dict:
        return {"id": self.id, "scope": self.scope, "kind": self.meta.get("kind", ""),
                "title": self.meta.get("title", ""), "status": self.status,
                "superseded_by": self.meta.get("superseded_by") or None,
                "stale_reason": self.meta.get("stale_reason") or None,
                "tags": self.items("tags"), "paths": self.items("paths"),
                "location": self.meta.get("location", "inline"),
                "verified": self.meta.get("verified", ""), "summary": self.summary(),
                "file": self.path.as_posix()}


def kn_dir(root: Path, project: Project | None) -> Path:
    return project.dir / "knowledge" if project else root / "knowledge"


def load_entries(root: Path, project: Project | None) -> list[Entry]:
    base = kn_dir(root, project)
    if not base.is_dir():
        return []
    entries = []
    for p in sorted(base.glob("*.md")):
        if p.name == "index.md":
            continue
        text = read_text(p)
        try:
            meta = parse_frontmatter(text)
            body = split_frontmatter(text)[1]
        except DeskError:
            continue
        eid = meta.get("id") or p.stem.split("-")[0]
        entries.append(Entry(eid, p, meta, body.strip(), project.key if project else "global"))
    return entries


def all_entries(root: Path, project: Project | None) -> list[Entry]:
    return (load_entries(root, project) if project else []) + load_entries(root, None)


def find_entry(root: Path, project: Project | None, entry_id: str) -> Entry:
    for e in all_entries(root, project):
        if e.id.casefold() == entry_id.casefold():
            return e
    raise DeskError(f"knowledge 항목을 찾을 수 없습니다: {entry_id}")


def next_entry_id(entries: list[Entry], prefix: str) -> str:
    nums = [int(m.group(1)) for e in entries if (m := re.fullmatch(rf"{prefix}(\d+)", e.id, re.I))]
    return f"{prefix}{max(nums, default=0) + 1:04d}"


def glob_regex(pattern: str) -> re.Pattern:
    p = pattern.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif p.startswith("**", i):
            out, i = out + ".*", i + 2
        elif p[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif p[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(p[i]), i + 1
    return re.compile(out + r"\Z", re.I)


def glob_prefix(pattern: str) -> str:
    """Literal leading part of a glob (casefolded), for cheap filtering before glob_regex."""
    p = pattern.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return re.split(r"[*?]", p, maxsplit=1)[0].casefold()


def repo_relative(path: str, toplevel: Path | None) -> str:
    p = Path(path)
    if p.is_absolute() and toplevel:
        try:
            p = p.resolve().relative_to(toplevel.resolve())
        except ValueError:
            pass
    return p.as_posix()


def entries_for_path(entries: list[Entry], rel_path: str) -> list[Entry]:
    return [e for e in entries if any(glob_regex(g).match(rel_path) for g in e.items("paths"))]


def search_score(e: Entry, terms: list[str]) -> int:
    fields = [(e.meta.get("title", ""), 3), (" ".join(e.items("tags")), 3),
              (" ".join(e.items("paths")), 2), (e.body, 1)]
    score = 0
    for t in terms:
        score += max((w for text, w in fields if t in text.casefold()), default=0)
    return score


def update_entry(e: Entry, updates: dict) -> None:
    write_text(e.path, update_frontmatter(read_text(e.path), updates))
    e.meta.update(updates)


def write_kn_index(root: Path, project: Project | None) -> Path | None:
    base = kn_dir(root, project)
    if not base.is_dir():
        return None
    entries = load_entries(root, project)
    label = f"{project.key} knowledge" if project else "전역 knowledge"

    def row(e: Entry) -> str:
        status = e.status + (f" → {e.meta['superseded_by']}" if e.meta.get("superseded_by") else "")
        cells = [e.id, e.meta.get("kind", ""), e.meta.get("title", ""), status,
                 ", ".join(e.items("tags")), e.meta.get("verified", "")]
        return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"

    text = (f"# {label}\n\n"
            "<!-- desk.py 가 생성하는 파일입니다. 직접 수정하지 마세요. -->\n\n"
            "| ID | 종류 | 제목 | 상태 | 태그 | 확인일 |\n|---|---|---|---|---|---|\n" +
            "\n".join(row(e) for e in entries) + "\n")
    path = base / "index.md"
    write_text(path, text)
    return path


def try_project(root: Path, args) -> tuple[Project | None, RepoInfo]:
    info = repo_info(Path(args.cwd))
    if args.project:
        return get_project(root, args.project), info
    return match_project(root, info), info


def split_csv(value: str | None) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()] if value else []


def cmd_kn_add(args) -> None:
    root = ensure_root()
    project, info = (None, repo_info(Path(args.cwd))) if args.is_global else current_project(root, Path(args.cwd), args.project)
    if args.is_global and args.paths:
        raise DeskError("전역 항목에는 --paths 를 쓸 수 없습니다. --tags 로 매칭합니다")
    entries = load_entries(root, project)
    entry_id = next_entry_id(entries, "K" if project else "G")
    sources = split_csv(args.sources)
    if not sources and project and info.branch:
        task = find_task_by_branch(project, info.branch)
        if task:
            sources = [task.id]
    body = args.body or "<!-- 요약: 15줄 이내. 사실 하나만 담고, 근거가 되는 코드 위치(경로:줄)를 포함한다. -->"
    values = {
        "id": entry_id, "title": args.title, "kind": args.kind,
        "paths": _format_value(split_csv(args.paths)), "tags": _format_value(split_csv(args.tags)),
        "sources": _format_value(sources), "location": args.location,
        "date": today(), "verified": now_minute(), "body": body,
    }
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
        entries = [e for e in entries if e.status != "superseded"]
    out([e.as_dict() for e in entries])


def cmd_kn_search(args) -> None:
    root = desk_root()
    project, info = try_project(root, args)
    entries = all_entries(root, project)
    if not args.all:
        entries = [e for e in entries if e.status != "superseded"]
    terms = [t.casefold() for t in args.terms]
    if args.path:
        matched = entries_for_path(entries, repo_relative(args.path, info.toplevel))
        if terms:
            matched = [e for e in matched if search_score(e, terms) > 0]
        ranked = matched
    else:
        if not terms:
            raise DeskError("검색어 또는 --path 가 필요합니다")
        scored = sorted(((search_score(e, terms), e) for e in entries), key=lambda x: -x[0])
        ranked = [e for s, e in scored if s > 0]
    out([e.as_dict() for e in ranked[:args.limit]])


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


def cmd_kn_verify(args) -> None:
    root = ensure_root()
    project, _ = try_project(root, args)
    e = find_entry(root, project, args.id)
    if e.status == "superseded":
        raise DeskError(f"대체된 항목입니다: {e.id} → {e.meta.get('superseded_by')}")
    verified = now_minute()
    update_entry(e, {"status": "valid", "verified": verified, "stale_reason": ""})
    write_kn_index(root, project if e.scope != "global" else None)
    out({"id": e.id, "status": "valid", "verified": verified})


def cmd_kn_stale(args) -> None:
    root = ensure_root()
    project, _ = try_project(root, args)
    e = find_entry(root, project, args.id)
    update_entry(e, {"status": "stale", "stale_reason": args.reason or "수동 표시"})
    write_kn_index(root, project if e.scope != "global" else None)
    out({"id": e.id, "status": "stale"})


# ---------------------------------------------------------------- doctor
# Checks the store for drift. Knowledge staleness is judged from git history on
# HEAD: commits (by author date, which survives rebase) after `verified` that touch
# the entry's paths. Only tree diffs are used (--name-only --no-renames) so a
# partial clone (blob:none) never has to fetch blobs.

DOCTOR_MARKER = ".last-doctor"
DOCTOR_REMIND_DAYS = 7
TASK_IDLE_DAYS = 30
KN_FILE_RE = re.compile(r"^[KG]\d{4}(-[^/\\]*)?\.md$")
REF_RE = re.compile(r"(?<![\w/.:-])((?:[\w.-]+/)+[\w.-]+\.\w+)(?::\d+)?")


def parse_when(value) -> float | None:
    for fmt, end_of_day in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            d = dt.datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
        return (d + dt.timedelta(days=1) if end_of_day else d).timestamp()
    return None


def project_repo(root: Path, project: Project, cwd: Path) -> Path | None:
    info = repo_info(cwd)
    if info.toplevel:
        p = match_project(root, info)
        if p and p.key == project.key:
            return info.toplevel
    for raw in project.config.get("paths", []):
        if (Path(raw) / ".git").exists():
            return Path(raw)
    return None


def git_pathspecs(patterns: list[str]) -> list[str]:
    return [":(glob,icase)" + p.replace("\\", "/").removeprefix("./") for p in patterns]


def commit_history(top: Path, patterns: list[str], since_ts: float) -> list[tuple[int, list[str]]]:
    """(author time, changed files) of HEAD commits touching patterns, newer than since_ts."""
    # --since filters by committer date, which is never earlier than the author date,
    # so it is a safe pre-filter; the author date decides.
    since_day = dt.date.fromtimestamp(since_ts) - dt.timedelta(days=1)
    raw = git(top, "-c", "core.quotePath=false", "log", f"--since={since_day.isoformat()}",
              "--no-renames", "--format=%x01%at", "--name-only", "--", *git_pathspecs(patterns),
              timeout=300)
    history = []
    for chunk in (raw or "").split("\x01")[1:]:
        lines = [l for l in chunk.splitlines() if l.strip()]
        if lines and int(lines[0]) > since_ts:
            history.append((int(lines[0]), lines[1:]))
    return history


def changes_since(history: list[tuple[int, list[str]]], patterns: list[str],
                  since_ts: float) -> tuple[int, set[str]]:
    regexes = [glob_regex(p) for p in patterns]
    commits, files = 0, set()
    for at, names in history:
        if at <= since_ts:
            continue
        hit = [n for n in names if any(rx.match(n) for rx in regexes)]
        if hit:
            commits += 1
            files.update(hit)
    return commits, files


def glob_matches_any(pattern: str, files: list[str]) -> bool:
    norm = pattern.replace("\\", "/").removeprefix("./")
    prefix = re.split(r"[*?]", norm, maxsplit=1)[0].casefold()
    rx = glob_regex(norm)
    return any(f.startswith(prefix) and rx.match(f) for f in files)


def missing_refs(e: Entry, top: Path) -> list[str]:
    refs = {m.group(1) for m in REF_RE.finditer(re.sub(r"\S+://\S+", "", e.body))}
    loc = str(e.meta.get("location", ""))
    if loc.startswith("repo:"):
        refs.add(loc[5:].split("#")[0])
    return sorted(r for r in refs if not (top / r).exists())


def doctor_knowledge(root: Path, project: Project | None, top: Path | None,
                     min_commits: int, issues: list) -> None:
    base = kn_dir(root, project)
    if not base.is_dir():
        return
    scope = project.key if project else "global"
    for p in base.iterdir():
        if not p.is_file() or p.name == "index.md":
            continue
        if not KN_FILE_RE.match(p.name):
            issues.append({"level": "warn", "kind": "stray-file", "target": p.as_posix(),
                           "message": "knowledge 폴더에 형식이 맞지 않는 파일(동기화 충돌 사본일 수 있음)"})
            continue
        try:
            parse_frontmatter(read_text(p))
        except DeskError as err:
            issues.append({"level": "warn", "kind": "frontmatter", "target": p.as_posix(),
                           "message": f"frontmatter를 읽을 수 없음({err})"})
    entries = load_entries(root, project)
    seen: dict[str, str] = {}
    for e in entries:
        if e.id in seen:
            issues.append({"level": "warn", "kind": "kn-duplicate-id", "id": e.id, "scope": scope,
                           "message": f"ID 중복: {seen[e.id]} / {e.path.name}"})
        seen[e.id] = e.path.name
    if not project or not top:
        return
    live = [e for e in entries if e.status != "superseded"]
    with_paths = [e for e in live if e.items("paths")]
    tracked, history = [], []
    if with_paths:
        tracked = [f.casefold() for f in (git(top, "-c", "core.quotePath=false", "ls-files",
                                               timeout=120) or "").splitlines()]
        sinces = [s for e in with_paths if (s := parse_when(e.meta.get("verified", ""))) is not None]
        if sinces:
            # one git log for all entries, then attributed per entry
            history = commit_history(top, sorted({g for e in with_paths for g in e.items("paths")}),
                                     min(sinces))
    for e in live:
        paths = e.items("paths")
        reasons, detail = [], {}
        if paths:
            if not any(glob_matches_any(g, tracked) for g in paths):
                reasons.append(("kn-paths-empty", "paths에 해당하는 추적 파일이 없음(이동·삭제되었을 수 있음)"))
            since = parse_when(e.meta.get("verified", ""))
            if since is not None:
                commits, files = changes_since(history, paths, since)
                if commits >= min_commits:
                    reasons.append(("kn-changed", f"확인 이후 커밋 {commits}건이 관련 파일 {len(files)}개를 변경"))
                    detail["changed_files"] = sorted(files)[:10]
        refs = missing_refs(e, top)
        if refs:
            reasons.append(("kn-missing-ref", f"본문이 참조하는 경로가 없음: {', '.join(refs[:5])}"))
        for kind, message in reasons:
            issues.append({"level": "warn", "kind": kind, "id": e.id, "scope": scope,
                           "status": e.status, "message": message, **detail})


def doctor_tasks(project: Project, top: Path | None, issues: list) -> None:
    now = dt.datetime.now().timestamp()
    for t in load_tasks(project, include_archive=True):
        if t.status == "?":
            issues.append({"level": "warn", "kind": "frontmatter", "target": t.state_path.as_posix(),
                           "message": "state.md frontmatter를 읽을 수 없음"})
            continue
        strays = stray_files(t)
        if strays:
            issues.append({"level": "warn", "kind": "stray-file", "target": t.dir.as_posix(),
                           "message": f"예상하지 않은 파일(동기화 충돌 사본일 수 있음): {', '.join(strays)}"})
        if t.archived or t.status not in OPEN_STATUSES:
            continue
        if top:
            gone = [b for b in t.branches if git(top, "rev-parse", "--verify", "-q", f"refs/heads/{b}") is None]
            if gone:
                issues.append({"level": "info", "kind": "task-branch-missing", "id": t.id,
                               "message": f"로컬에 없는 브랜치: {', '.join(gone)}. 머지 후 삭제되었다면 종료를 검토"})
        updated = parse_when(t.meta.get("updated", ""))
        if updated is not None and now - updated > TASK_IDLE_DAYS * 86400:
            issues.append({"level": "info", "kind": "task-idle", "id": t.id,
                           "message": f"{TASK_IDLE_DAYS}일 넘게 갱신되지 않음(상태 {t.status})"})


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
    doctor_tasks(project, top, issues)
    focus_id = get_focus_id(project)
    if focus_id and not get_focus_task(project):
        issues.append({"level": "info", "kind": "focus-stale", "id": focus_id,
                       "message": "포커스가 종료되었거나 없는 작업을 가리킴. `focus --clear` 또는 다른 작업으로 지정"})
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


# ---------------------------------------------------------------- commands

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
        "focus": get_focus_id(project) if project else None,
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
        "id": task_id, "title": args.title, "project": project.key,
        "branches": _format_value([branch] if branch else []),
        "parent": parent.id if parent else "", "related": _format_value(related),
        "date": today(), "datetime": now_minute(),
    }
    for name in TASK_FILES:
        write_text(tdir / name, render_template(project.lang, name, values))
    # a branchless task created on a shared branch (or detached) becomes the focus
    focused = (branch is None and not args.no_focus
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
            raise DeskError(f"진행 중인 작업만 포커스할 수 있습니다: {task.id} ({task.status})")
        set_focus(project, task.id)
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
            raise DeskError(f"{task.id} 에 하위 작업이 있어 상위 작업을 지정할 수 없습니다(한 단계만 허용)")
        parent = resolve_parent(project, args.parent, task.id)
        updates["parent"] = parent.id
        logs.append(f"상위 작업 지정: {parent.id}")
    elif args.no_parent and task.meta.get("parent"):
        updates["parent"] = ""
        logs.append(f"상위 작업 해제: {task.meta['parent']}")
    related = task_list_field(task, "related")
    for r in split_csv(args.add):
        rid = get_task(project, r).id
        if rid.casefold() == task.id.casefold():
            raise DeskError("자기 자신을 연관 작업으로 지정할 수 없습니다")
        if rid.casefold() not in (x.casefold() for x in related):
            related.append(rid)
            logs.append(f"연관 작업 추가: {rid}")
    for r in split_csv(args.remove):
        kept = [x for x in related if x.casefold() != r.casefold()]
        if len(kept) != len(related):
            related = kept
            logs.append(f"연관 작업 제거: {r}")
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
        raise DeskError("아카이브된 작업에는 브랜치를 연결할 수 없습니다")
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
        raise DeskError("아카이브된 작업입니다. desk.py reopen 을 사용하세요")
    if args.status == "active":
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
        raise DeskError("이미 아카이브된 작업입니다")
    dest = project.dir / "archive" / task.id
    if dest.exists():
        raise DeskError(f"아카이브에 같은 ID가 이미 있습니다: {dest}")
    set_meta(task, {"status": args.result})
    append_log(task, [f"작업 종료: {args.result}"] + ([args.note] if args.note else []))
    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.move(str(task.dir), str(dest))
    unfocused = (get_focus_id(project) or "").casefold() == task.id.casefold()
    if unfocused:
        set_focus(project, None)
    write_index(project)
    open_children = [t.id for t in children_of(project, task.id, open_only=True)]
    out({"task": task.id, "status": args.result, "dir": str(dest), "unfocused": unfocused,
         **({"warning": f"진행 중인 하위 작업이 남아 있습니다: {', '.join(open_children)}"} if open_children else {})})


def cmd_reopen(args) -> None:
    root = ensure_root()
    project, _ = current_project(root, Path(args.cwd), args.project)
    task = get_task(project, args.id)
    if not task.archived:
        raise DeskError("아카이브된 작업이 아닙니다")
    for b in task.branches:
        check_branch_free(project, b, task.id)
    dest = project.dir / "tasks" / task.id
    import shutil
    shutil.move(str(task.dir), str(dest))
    task.dir, task.archived = dest, False
    set_meta(task, {"status": "active"})
    append_log(task, ["작업 재개"])
    write_index(project)
    out({"task": task.id, "status": "active", "dir": str(dest)})


def cmd_list(args) -> None:
    root = desk_root()
    projects = [get_project(root, args.project)] if args.project else load_projects(root)
    rows = []
    for p in projects:
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


# ---------------------------------------------------------------- hook

def stray_files(task: Task) -> list[str]:
    return sorted(p.name for p in task.dir.iterdir() if p.is_file() and p.name not in TASK_FILES)


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
    kn_line = knowledge_line(root, project, cli)
    task, source = resolve_session_task(project, branch)
    if not task:
        lines = [f"{head} · 연결된 작업 없음.", f"CLI: {cli}"] + ([kn_line] if kn_line else [])
        branchless = [t for t in load_tasks(project) if t.status in OPEN_STATUSES and not t.branches]
        if branchless:
            listed = ", ".join(f"{t.id}({t.meta.get('title', '')})" for t in branchless[:5])
            more = f" 외 {len(branchless) - 5}건" if len(branchless) > 5 else ""
            lines.append(f"진행 중인 브랜치 없는 작업: {listed}{more}. 이어서 할 작업이면 사용자에게 확인하고 "
                         f"`{cli} focus <id>` 로 포커스를 지정할 것.")
        return "\n".join(lines + ["작업 생성 기준과 기록 규칙은 agent-desk:desk 스킬, knowledge 규칙은 agent-desk:kn 스킬을 따른다."])
    design_doc = task.meta.get("design_doc")
    label = "" if source == "branch" else " · 포커스"
    parts = [f"{head} · 작업 {task.id} ({task.status}{label})",
             f"작업 폴더: {task.dir.as_posix()}"] + ([f"설계 문서: {design_doc}"] if design_doc else []) + [
             f"CLI: {cli}"] + ([kn_line] if kn_line else [])
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
        parts += ["", f"연관 작업: {', '.join(related)} (필요할 때 `{cli} list --all` 로 위치를 찾아 읽을 것)"]
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
    design_doc = parent.meta.get("design_doc")
    return (["", f"--- 상위 작업 {parent.id} ({parent.status}): {parent.meta.get('title', '')} ---",
             f"목표: {section_line(parent, '목표') or '(비어 있음)'}",
             f"폴더: {parent.dir.as_posix()}"] + ([f"설계 문서: {design_doc}"] if design_doc else []) + [
             "결정 (본문은 상위 작업의 decisions.md):"] + ([f"- {t}" for t in titles] or ["(없음)"]) +
            ["상위 작업의 유효한 결정을 따른다. 어긋나는 결정이 필요하면 사용자에게 확인하고 상위 작업의 decisions.md에 기록할 것."])


def knowledge_line(root: Path, project: Project, cli: str) -> str | None:
    pn = sum(1 for e in load_entries(root, project) if e.status != "superseded")
    gn = sum(1 for e in load_entries(root, None) if e.status != "superseded")
    if not pn and not gn:
        return None
    line = (f"knowledge: 프로젝트 {pn}건 · 전역 {gn}건. 시스템을 조사하거나 수정하기 전에 "
            f"`{cli} kn search <검색어>` 또는 `kn search --path <파일>` 로 관련 항목을 확인할 것.")
    if pn:
        marker = project.dir / DOCTOR_MARKER
        age = (dt.datetime.now().timestamp() - marker.stat().st_mtime) / 86400 if marker.exists() else None
        if age is None or age >= DOCTOR_REMIND_DAYS:
            since = f"{int(age)}일째" if age is not None else "한 번도"
            line += (f"\ndoctor를 {since} 실행하지 않았다. 사용자에게 `{cli} doctor` 실행을 한 줄로 제안할 것"
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


HOOK_EVENTS = ("session-start", "post-tool-use", "user-prompt-submit")


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


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

    # hooks run on every tool use: skip building the argparse tree
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "hook" and argv[1] in HOOK_EVENTS:
        cmd_hook(argv[1])
        return 0

    import argparse
    ap = argparse.ArgumentParser(prog="desk.py", description="agent-desk CLI")
    ap.add_argument("--cwd", default=os.getcwd(), help="기준 디렉터리 (기본: 현재 디렉터리)")
    ap.add_argument("--project", help="프로젝트 key (기본: 현재 저장소로 자동 판별)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("where", help="현재 저장소·브랜치의 프로젝트와 작업 확인")

    p = sub.add_parser("project-init", help="현재 저장소를 프로젝트로 등록")
    p.add_argument("key")
    p.add_argument("--id-template", default=DEFAULT_ID_TEMPLATE)
    p.add_argument("--shared", help="공용 브랜치 목록 (쉼표 구분)")
    p.add_argument("--lang", default="ko")

    p = sub.add_parser("new", help="작업 생성")
    p.add_argument("--title", required=True)
    p.add_argument("--slug", help="브랜치 없는 작업의 ID 슬러그")
    p.add_argument("--branch", help="연결할 브랜치 (기본: 현재 브랜치)")
    p.add_argument("--no-branch", action="store_true")
    p.add_argument("--parent", help="상위 작업 ID (한 단계만 허용)")
    p.add_argument("--related", help="연관 작업 ID (쉼표 구분)")
    p.add_argument("--no-focus", action="store_true", help="브랜치 없는 작업을 만들어도 포커스로 지정하지 않음")

    p = sub.add_parser("focus", help="포커스 작업 조회·지정·해제 (브랜치에 연결된 작업이 없을 때 쓰는 작업)")
    p.add_argument("id", nargs="?")
    p.add_argument("--clear", action="store_true")

    p = sub.add_parser("relate", help="작업의 상위·연관 관계 변경")
    p.add_argument("id")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--parent", help="상위 작업 지정")
    g.add_argument("--no-parent", action="store_true", help="상위 작업 해제")
    p.add_argument("--add", help="연관 작업 추가 (쉼표 구분)")
    p.add_argument("--remove", help="연관 작업 제거 (쉼표 구분)")

    p = sub.add_parser("link", help="작업에 브랜치 추가 연결")
    p.add_argument("id")
    p.add_argument("--branch")

    p = sub.add_parser("status", help="작업 상태 변경 (active/paused)")
    p.add_argument("id")
    p.add_argument("status", choices=OPEN_STATUSES)

    p = sub.add_parser("close", help="작업 종료 후 아카이브")
    p.add_argument("id")
    p.add_argument("result", choices=("done", "abandoned"))
    p.add_argument("--note")

    p = sub.add_parser("reopen", help="아카이브된 작업 재개")
    p.add_argument("id")

    p = sub.add_parser("list", help="작업 목록")
    p.add_argument("--all", action="store_true", help="아카이브 포함")

    sub.add_parser("index", help="작업·knowledge index.md 재생성")

    kn = sub.add_parser("kn", help="knowledge 항목 관리").add_subparsers(dest="kn_cmd", required=True)
    p = kn.add_parser("add", help="항목 추가")
    p.add_argument("--title", required=True)
    p.add_argument("--kind", required=True, choices=KN_KINDS)
    p.add_argument("--paths", help="관련 경로 glob (저장소 기준, 쉼표 구분)")
    p.add_argument("--tags", help="태그 (쉼표 구분)")
    p.add_argument("--sources", help="출처 작업 ID (기본: 현재 브랜치의 작업)")
    p.add_argument("--location", default="inline", help="inline | repo:<경로> | claude-docs:<url>")
    p.add_argument("--slug", help="파일명용 영문 슬러그")
    p.add_argument("--body", help="본문 (생략 시 안내 주석만 넣고 이후 직접 편집)")
    p.add_argument("--global", dest="is_global", action="store_true", help="전역 항목으로 추가")
    p = kn.add_parser("list", help="항목 목록 (프로젝트 + 전역)")
    p.add_argument("--all", action="store_true", help="대체된 항목 포함")
    p = kn.add_parser("search", help="항목 검색")
    p.add_argument("terms", nargs="*")
    p.add_argument("--path", help="이 파일에 해당하는 항목 (paths glob 매칭)")
    p.add_argument("--all", action="store_true", help="대체된 항목 포함")
    p.add_argument("--limit", type=int, default=10)
    p = kn.add_parser("supersede", help="항목을 다른 항목으로 대체")
    p.add_argument("id")
    p.add_argument("--by", required=True)
    p = kn.add_parser("verify", help="코드와 대조해 확인했음을 기록")
    p.add_argument("id")
    p = kn.add_parser("stale", help="재확인 필요로 표시")
    p.add_argument("id")
    p.add_argument("--reason", help="재확인이 필요한 이유")

    p = sub.add_parser("doctor", help="저장소 상태 점검 (낡은 knowledge, 충돌 사본, 방치된 작업)")
    p.add_argument("--apply", action="store_true", help="낡은 것으로 판단된 valid 항목을 stale로 표시")
    p.add_argument("--min-commits", type=int, default=3, help="확인 이후 이 수 이상의 커밋이 paths를 바꾸면 낡은 것으로 판단")

    p = sub.add_parser("hook", help="Claude Code hook 진입점")
    p.add_argument("event", choices=HOOK_EVENTS)

    args = ap.parse_args(argv)
    handlers = {
        "where": cmd_where, "project-init": cmd_project_init, "new": cmd_new,
        "link": cmd_link, "relate": cmd_relate, "focus": cmd_focus, "status": cmd_status, "close": cmd_close, "reopen": cmd_reopen,
        "list": cmd_list, "index": cmd_index, "hook": lambda a: cmd_hook(a.event), "doctor": cmd_doctor,
    }
    kn_handlers = {
        "add": cmd_kn_add, "list": cmd_kn_list, "search": cmd_kn_search,
        "supersede": cmd_kn_supersede, "verify": cmd_kn_verify, "stale": cmd_kn_stale,
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


if __name__ == "__main__":
    sys.exit(main())
