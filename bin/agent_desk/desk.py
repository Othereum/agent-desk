"""Reading a desk folder: one folder per task, holding state.md (frontmatter: title, status,
parent, branches), decisions.md, log.md and files/."""

from __future__ import annotations

from pathlib import Path

from .core import FILES_DIR, OPEN_STATUSES, CLOSED_STATUSES, TASK_FILES, DeskError, read_text
from .frontmatter import parse_frontmatter


class Task:
    def __init__(self, dir: Path, meta: dict, error: str = ""):
        self.dir, self.meta, self.error = dir, meta, error

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def title(self) -> str:
        return str(self.meta.get("title", ""))

    @property
    def status(self) -> str:
        return str(self.meta.get("status", ""))

    @property
    def parent(self) -> str:
        return str(self.meta.get("parent") or "")

    @property
    def branches(self) -> list[str]:
        value = self.meta.get("branches") or []
        return value if isinstance(value, list) else [value]

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


def load_tasks(desk: Path) -> list[Task]:
    """Every task folder, sorted by id. A folder without a readable state.md comes back with an error."""
    tasks = []
    for d in sorted(p for p in desk.iterdir() if p.is_dir() and not p.name.startswith(".")):
        state = d / "state.md"
        if not state.is_file():
            tasks.append(Task(d, {}, "state.md가 없다"))
            continue
        try:
            meta = parse_frontmatter(read_text(state))
        except (DeskError, OSError, UnicodeDecodeError) as e:
            tasks.append(Task(d, {}, f"state.md frontmatter를 읽을 수 없다({e})"))
            continue
        task = Task(d, meta)
        if task.status not in OPEN_STATUSES + CLOSED_STATUSES:
            task.error = f"status가 {', '.join(OPEN_STATUSES + CLOSED_STATUSES)} 중 하나가 아니다({task.status or '없음'})"
        tasks.append(task)
    return tasks


def stray_files(task: Task) -> list[str]:
    """What the task folder holds besides its files and the attachments folder: a sync conflict
    copy, or a file put in the wrong place (a folder ends with /)."""
    return sorted(p.name + "/" if p.is_dir() else p.name for p in task.dir.iterdir()
                  if not (p.is_file() and (p.name in TASK_FILES or p.name.endswith(".tmp")))
                  and not (p.is_dir() and p.name == FILES_DIR))


def is_legacy(desk: Path) -> bool:
    """A project folder of the format before 0.13 (tasks/ and archive/, project.toml)."""
    return (desk / "project.toml").is_file() or (desk / "tasks").is_dir()
