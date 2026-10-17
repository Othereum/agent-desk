"""Knowledge entries: loading, lookup, path matching, search and index."""

from __future__ import annotations

import re
from pathlib import Path

from .core import DeskError, TASK_FILES, read_text, today, write_text
from .frontmatter import parse_frontmatter, split_frontmatter, update_frontmatter
from .gitinfo import RepoInfo, repo_info
from .model import Project, get_project, load_projects, load_tasks, load_trash, match_project


# Project entries live in projects/<KEY>/knowledge/ (ids K0001...),
# global entries in <root>/knowledge/ (ids G0001...).

KN_KINDS = ("decision", "pitfall", "finding", "howto")
KN_INJECT_KIND_ORDER = ("pitfall", "finding", "howto", "decision")


def inject_order(kind: str, entry_id: str) -> tuple:
    """Sort key for entries shown for a file: pitfalls first, then newest first."""
    rank = KN_INJECT_KIND_ORDER.index(kind) if kind in KN_INJECT_KIND_ORDER else len(KN_INJECT_KIND_ORDER)
    return rank, -int(re.sub(r"\D", "", entry_id) or 0)


KN_STATUSES = ("valid", "stale", "superseded", "retired", "deleted")
KN_INACTIVE = ("superseded", "retired", "deleted")  # kept for history, out of injection and search


class Entry:
    def __init__(self, id: str, path: Path, meta: dict, body: str, scope: str):
        self.id, self.path, self.meta, self.body = id, path, meta, body
        self.scope = scope  # project key or "global"

    @property
    def status(self) -> str:
        return self.meta.get("status", "valid")

    @property
    def live(self) -> bool:
        return self.status not in KN_INACTIVE

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
                "retired_reason": self.meta.get("retired_reason") or None,
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


def entry_references(root: Path, e: Entry) -> list[str]:
    """`path:line` of every record that mentions the entry's id: task files (archive and trash
    included: a trashed task can be restored) and other knowledge entries (body or frontmatter, e.g. superseded_by). Project ids are
    numbered per project, so a project entry is looked up in its own project only."""
    projects = [p for p in load_projects(root) if e.scope in ("global", p.key)]
    files = [p for proj in projects for t in load_tasks(proj, include_archive=True) + load_trash(proj)
             for p in (t.dir / name for name in TASK_FILES) if p.exists()]
    for base in [kn_dir(root, proj) for proj in projects] + [kn_dir(root, None)]:
        if base.is_dir():
            files += [p for p in sorted(base.glob("*.md")) if p.name != "index.md" and p != e.path]
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(e.id)}(?!\d)", re.I)
    return [f"{p.as_posix()}:{n}" for p in files
            for n, line in enumerate(read_text(p).splitlines(), 1) if pattern.search(line)]


def tombstone_entry(e: Entry) -> Path:
    """Replace the entry with a stub that keeps its id taken (so it is never reused) and
    drops everything else, the slug in the file name included."""
    path = e.path.with_name(f"{e.id}.md")
    write_text(path, f"---\nid: {e.id}\nstatus: deleted\ndeleted: {today()}\n---\n")
    if path != e.path:
        e.path.unlink()
    return path


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
