"""doctor: drift checks for knowledge and tasks."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from .core import DeskError, OPEN_STATUSES, is_write_temp, read_text
from .frontmatter import frontmatter_comments
from .gitinfo import git, repo_info
from .model import Project, load_tasks, load_trash, match_project, stray_files, trashed_at
from .knowledge import Entry, glob_regex, kn_dir, load_entries


# Checks the store for drift. Knowledge staleness is judged from git history on
# HEAD: commits (by author date, which survives rebase) after `verified` that touch
# the entry's paths. Only tree diffs are used (--name-only --no-renames) so a
# partial clone (blob:none) never has to fetch blobs.

DOCTOR_MARKER = ".last-doctor"


DOCTOR_REMIND_DAYS = 7
TASK_IDLE_DAYS = 30
TRASH_KEEP_DAYS = 30
KN_FILE_RE = re.compile(r"^[KG]\d{4}(-[^/\\]*)?\.md$")
# extension is ASCII so a Korean particle right after a path ("hooks.py는") is not read as part of it
REF_RE = re.compile(r"(?<![\w/.:-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+)(?::\d+)?")


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


def title_comment_issue(path: Path, comment: str) -> dict:
    # before 0.4.0 titles were written unquoted, so "PR #12" now reads as "PR" plus a comment
    return {"level": "warn", "kind": "frontmatter-title-comment", "target": path.as_posix(),
            "message": f"title 줄의 `{comment.strip()}` 부분이 주석으로 읽힌다. 제목의 일부라면 title 값을 따옴표로 감쌀 것"}


def doctor_knowledge(root: Path, project: Project | None, top: Path | None,
                     min_commits: int, issues: list) -> None:
    base = kn_dir(root, project)
    if not base.is_dir():
        return
    scope = project.key if project else "global"
    for p in base.iterdir():
        if not p.is_file() or p.name == "index.md" or is_write_temp(p.name):
            continue
        if not KN_FILE_RE.match(p.name):
            issues.append({"level": "warn", "kind": "stray-file", "target": p.as_posix(),
                           "message": "knowledge 폴더에 형식이 맞지 않는 파일(동기화 충돌 사본일 수 있음)"})
            continue
        try:
            comments = frontmatter_comments(read_text(p))
        except DeskError as err:
            issues.append({"level": "warn", "kind": "frontmatter", "target": p.as_posix(),
                           "message": f"frontmatter를 읽을 수 없음({err})"})
            continue
        if "title" in comments:
            issues.append(title_comment_issue(p, comments["title"]))
    entries = load_entries(root, project)
    seen: dict[str, str] = {}
    for e in entries:
        if e.id in seen:
            issues.append({"level": "warn", "kind": "kn-duplicate-id", "id": e.id, "scope": scope,
                           "message": f"ID 중복: {seen[e.id]} / {e.path.name}"})
        seen[e.id] = e.path.name
    if not project or not top:
        return
    live = [e for e in entries if e.live]
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
