"""`adesk migrate`: convert the data of the format before 0.13 (schema 3).

Before: one data root ($AGENT_DESK_ROOT or ~/agent-desk) with desk.toml and
projects/<KEY>/{project.toml, index.md, tasks/, archive/, trash/}.
After: each projects/<KEY>/ is a desk folder holding the task folders directly, registered in
each local clone with `git config agent-desk.dir`. State.md keeps only title, status, parent
and branches; a non-empty `related` moves to the body's 링크 (links) section: data of that format
was written from the Korean templates.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

from .core import DIR_KEY, DeskError, git, native_path, read_text, write_text
from .frontmatter import parse_frontmatter, split_frontmatter, update_frontmatter

LEGACY_SCHEMA = 3
OBSOLETE_KEYS = ("id", "project", "created", "updated", "verified", "related", "trashed", "links", "design_doc")
OBSOLETE_FILES = ("index.md", "project.toml", ".last-doctor")  # .last-doctor: left by versions before 0.10
BUSY_WAIT = 10.0  # seconds: a sync client or an indexer can hold a folder for a while


def retry_busy(action) -> None:
    """Run action, retrying while Windows refuses it because another process (a sync client,
    an indexer) has the file or folder open. Running migrate again resumes after a failure."""
    import time
    deadline, delay = time.monotonic() + BUSY_WAIT, 0.05
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() + delay > deadline:
                raise DeskError("Another program (such as a sync client) is using the files, so the migration did not finish. "
                                "Run `adesk migrate` again in a moment to resume") from None
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def remove(path: Path) -> None:
    """Delete an obsolete file or empty folder. A sync client may mark a folder read-only,
    which makes Windows refuse to delete it: clear the flag first."""
    import stat
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if path.is_dir() else 0))
    except OSError:
        pass
    retry_busy(path.rmdir if path.is_dir() else path.unlink)


def default_root() -> Path:
    env = os.environ.get("AGENT_DESK_ROOT")
    return Path(env).expanduser() if env else Path.home() / "agent-desk"


def legacy_root() -> Path | None:
    root = default_root()
    return root if (root / "desk.toml").is_file() else None


def run(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="adesk migrate", description="convert data of the format before 0.13")
    parser.add_argument("--dry-run", action="store_true", help="only show what would change")
    parser.add_argument("--root", help="the old data root (default: $AGENT_DESK_ROOT or ~/agent-desk)")
    args = parser.parse_args(argv)
    root = Path(native_path(args.root)).expanduser() if args.root else default_root()
    try:
        result = migrate(root, args.dry_run)
    except DeskError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


def migrate(root: Path, dry_run: bool) -> dict:
    marker = root / "desk.toml"
    if not marker.is_file():
        return {"root": root.as_posix(), "changed": [], "note": "no data of the old format to convert"}
    version = tomllib.loads(read_text(marker)).get("schema_version")
    if version != LEGACY_SCHEMA:
        raise DeskError(f"The data is schema {version}. Convert it to schema {LEGACY_SCHEMA} with `adesk migrate` of the 0.12.x plugin, "
                        "then run this again")
    projects = sorted(p for p in (root / "projects").iterdir() if p.is_dir()) if (root / "projects").is_dir() else []
    plans = [plan_project(p) for p in projects]  # checks everything before the first change
    changed = []
    for plan in plans:
        changed += apply_project(plan, dry_run)
    changed.append(f"delete {marker.as_posix()}")
    if not dry_run:
        remove(marker)
    return {"root": root.as_posix(), "dry_run": dry_run, "changed": changed,
            "note": "register the clones on other computers with `git config agent-desk.dir <desk folder>`"}


def plan_project(project: Path) -> dict:
    tasks = []
    for sub in ("tasks", "archive"):
        base = project / sub
        if base.is_dir():
            tasks += [d for d in sorted(base.iterdir()) if d.is_dir()]
    seen, problems = set(), []
    for d in tasks:
        if d.name in seen or (project / d.name).exists():
            problems.append(f"{d.name}: a task folder of the same name already exists")
        seen.add(d.name)
        try:
            parse_frontmatter(read_text(d / "state.md"))
        except (DeskError, OSError) as e:
            problems.append(f"{d.name}: cannot read state.md ({e})")
    trash = project / "trash"
    if trash.is_dir() and any(trash.iterdir()):
        problems.append(f"the trash ({trash.as_posix()}) is not empty: delete the tasks you do not need, restore the ones to keep "
                        "with `adesk restore` of 0.12.x, then run this again")
    if problems:
        raise DeskError(f"{project.name}: cannot convert. " + "; ".join(problems))
    config = project / "project.toml"
    paths = tomllib.loads(read_text(config)).get("paths", []) if config.is_file() else []
    return {"dir": project, "tasks": tasks, "paths": paths}


def apply_project(plan: dict, dry_run: bool) -> list[str]:
    project: Path = plan["dir"]
    changed = []
    for d in plan["tasks"]:
        changed.append(f"move {d.relative_to(project).as_posix()} → {d.name}")
        if not dry_run:
            dest = project / d.name
            retry_busy(lambda: os.rename(d, dest))
            rewrite_state(dest / "state.md")
    # before project.toml goes: a migrate run again after a failure still has the paths
    for raw in plan["paths"]:
        repo = Path(native_path(raw))
        if not repo.is_dir() or git(repo, "rev-parse", "--git-dir") is None:
            continue
        if git(repo, "config", "--get", DIR_KEY):
            continue
        changed.append(f"register {repo.as_posix()}: git config {DIR_KEY} {project.as_posix()}")
        if not dry_run:
            git(repo, "config", DIR_KEY, project.as_posix())
    for name in ("tasks", "archive", "trash"):
        if (project / name).is_dir():
            changed.append(f"delete {project.name}/{name}/")
            if not dry_run:
                remove(project / name)
    for name in OBSOLETE_FILES:
        if (project / name).is_file():
            changed.append(f"delete {project.name}/{name}")
            if not dry_run:
                remove(project / name)
    return changed


def rewrite_state(path: Path) -> None:
    """Drop the keys the new format does not keep. The file keeps its modification time: it now
    stands for the old `updated`."""
    stat = path.stat()
    text = read_text(path).replace("\r\n", "\n")
    meta = parse_frontmatter(text)
    related = meta.get("related") or []
    related = related if isinstance(related, list) else [related]
    text = update_frontmatter(text, {k: None for k in OBSOLETE_KEYS if k in meta})
    if related:
        text = add_link(text, f"- 관련 태스크: {', '.join(related)}")
    write_text(path, text)
    os.utime(path, (stat.st_atime, stat.st_mtime))


def add_link(text: str, line: str) -> str:
    fm, body = split_frontmatter(text)
    lines = body.rstrip("\n").split("\n")
    if "## 링크" not in lines:
        lines += ["", "## 링크", "", line]
    else:
        end = next((i for i in range(lines.index("## 링크") + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        while end > 0 and not lines[end - 1].strip():
            end -= 1
        if lines[end - 1] == "## 링크":
            lines.insert(end, "")
            end += 1
        lines.insert(end, line)
    return "---\n" + "\n".join(fm) + "\n---\n" + "\n".join(lines) + "\n"
