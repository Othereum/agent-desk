"""`adesk migrate`: convert the data of the format before 0.13 (schema 3).

Before: one data root ($AGENT_DESK_ROOT or ~/agent-desk) with desk.toml and
projects/<KEY>/{project.toml, index.md, tasks/, archive/, trash/}.
After: each projects/<KEY>/ is a desk folder holding the task folders directly, registered in
each local clone with `git config agent-desk.dir`. State.md keeps only title, status, parent
and branches; a non-empty `related` moves to the body's 링크 section.
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


def default_root() -> Path:
    env = os.environ.get("AGENT_DESK_ROOT")
    return Path(env).expanduser() if env else Path.home() / "agent-desk"


def legacy_root() -> Path | None:
    root = default_root()
    return root if (root / "desk.toml").is_file() else None


def run(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="adesk migrate", description="0.13 이전 형식의 데이터를 변환")
    parser.add_argument("--dry-run", action="store_true", help="바꿀 내용만 보여 준다")
    parser.add_argument("--root", help="이전 데이터 루트 (기본: $AGENT_DESK_ROOT 또는 ~/agent-desk)")
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
        return {"root": root.as_posix(), "changed": [], "note": "변환할 이전 형식 데이터가 없습니다"}
    version = tomllib.loads(read_text(marker)).get("schema_version")
    if version != LEGACY_SCHEMA:
        raise DeskError(f"스키마 {version} 데이터입니다. 0.12.x 플러그인에서 `adesk migrate` 로 스키마 {LEGACY_SCHEMA}까지 "
                        "변환한 뒤 다시 실행하세요")
    projects = sorted(p for p in (root / "projects").iterdir() if p.is_dir()) if (root / "projects").is_dir() else []
    plans = [plan_project(p) for p in projects]  # checks everything before the first change
    changed = []
    for plan in plans:
        changed += apply_project(plan, dry_run)
    changed.append(f"삭제 {marker.as_posix()}")
    if not dry_run:
        marker.unlink()
    return {"root": root.as_posix(), "dry_run": dry_run, "changed": changed,
            "note": "다른 PC의 클론은 `git config agent-desk.dir <데스크 폴더>` 로 등록하세요"}


def plan_project(project: Path) -> dict:
    tasks = []
    for sub in ("tasks", "archive"):
        base = project / sub
        if base.is_dir():
            tasks += [d for d in sorted(base.iterdir()) if d.is_dir()]
    seen, problems = set(), []
    for d in tasks:
        if d.name in seen or (project / d.name).exists():
            problems.append(f"{d.name}: 같은 이름의 태스크 폴더가 이미 있음")
        seen.add(d.name)
        try:
            parse_frontmatter(read_text(d / "state.md"))
        except (DeskError, OSError) as e:
            problems.append(f"{d.name}: state.md를 읽을 수 없음({e})")
    trash = project / "trash"
    if trash.is_dir() and any(trash.iterdir()):
        problems.append(f"휴지통({trash.as_posix()})이 비어 있지 않음: 필요 없는 태스크는 지우고, 남길 태스크는 "
                        "0.12.x에서 `adesk restore` 로 되살린 뒤 다시 실행")
    if problems:
        raise DeskError(f"{project.name}: 변환할 수 없습니다. " + "; ".join(problems))
    config = project / "project.toml"
    paths = tomllib.loads(read_text(config)).get("paths", []) if config.is_file() else []
    return {"dir": project, "tasks": tasks, "paths": paths}


def apply_project(plan: dict, dry_run: bool) -> list[str]:
    project: Path = plan["dir"]
    changed = []
    for d in plan["tasks"]:
        changed.append(f"이동 {d.relative_to(project).as_posix()} → {d.name}")
        if not dry_run:
            dest = project / d.name
            os.rename(d, dest)
            rewrite_state(dest / "state.md")
    for name in ("tasks", "archive", "trash"):
        if (project / name).is_dir():
            changed.append(f"삭제 {project.name}/{name}/")
            if not dry_run:
                (project / name).rmdir()
    for name in ("index.md", "project.toml"):
        if (project / name).is_file():
            changed.append(f"삭제 {project.name}/{name}")
            if not dry_run:
                (project / name).unlink()
    for raw in plan["paths"]:
        repo = Path(native_path(raw))
        if not repo.is_dir() or git(repo, "rev-parse", "--git-dir") is None:
            continue
        if git(repo, "config", "--get", DIR_KEY):
            continue
        changed.append(f"등록 {repo.as_posix()}: git config {DIR_KEY} {project.as_posix()}")
        if not dry_run:
            git(repo, "config", DIR_KEY, project.as_posix())
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
