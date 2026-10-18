"""doctor: drift checks for tasks."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from .core import OPEN_STATUSES, read_text
from .frontmatter import frontmatter_comments
from .gitinfo import git, repo_info
from .model import Project, load_tasks, load_trash, match_project, stray_files, trashed_at


TASK_IDLE_DAYS = 30
TRASH_KEEP_DAYS = 30


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


def title_comment_issue(path: Path, comment: str) -> dict:
    # before 0.4.0 titles were written unquoted, so "PR #12" now reads as "PR" plus a comment
    return {"level": "warn", "kind": "frontmatter-title-comment", "target": path.as_posix(),
            "message": f"title 줄의 `{comment.strip()}` 부분이 주석으로 읽힌다. 제목의 일부라면 title 값을 따옴표로 감쌀 것"}


def doctor_tasks(project: Project, top: Path | None, issues: list) -> None:
    now = dt.datetime.now().timestamp()
    for t in load_tasks(project, include_archive=True):
        if t.status == "?":
            issues.append({"level": "warn", "kind": "frontmatter", "target": t.state_path.as_posix(),
                           "message": f"state.md frontmatter를 읽을 수 없음({t.meta.get('error', '')})"})
            continue
        comments = frontmatter_comments(read_text(t.state_path))
        if "title" in comments:
            issues.append(title_comment_issue(t.state_path, comments["title"]))
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
                               "message": f"로컬에 없는 브랜치: {', '.join(gone)}. 머지 후 삭제되었다면 종료를, 버린 브랜치라면 adesk unlink 를 검토"})
        updated = parse_when(t.meta.get("updated", ""))
        if updated is not None and now - updated > TASK_IDLE_DAYS * 86400:
            issues.append({"level": "info", "kind": "task-idle", "id": t.id,
                           "message": f"{TASK_IDLE_DAYS}일 넘게 갱신되지 않음(상태 {t.status})"})
    # the trash is not checked for conflict copies: nothing reads it until a task is restored
    for t in load_trash(project):
        when = parse_when(trashed_at(t) or "")
        if when is None:
            try:
                when = (t.dir / "log.md").stat().st_mtime
            except OSError:
                continue
        if now - when > TRASH_KEEP_DAYS * 86400:
            issues.append({"level": "info", "kind": "trash-old", "id": t.id,
                           "message": f"휴지통에 {TRASH_KEEP_DAYS}일 넘게 있음. 사용자가 원하면 `delete {t.id} --purge` 로 영구 삭제"})
