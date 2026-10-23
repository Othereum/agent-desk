"""Constants, errors, file io and git calls."""

from __future__ import annotations

import os
import re
from pathlib import Path


TASK_FILES = ("state.md", "decisions.md", "log.md")
FILES_DIR = "files"  # a task's attachments: evidence kept as given, the conclusions go in the task files
OPEN_STATUSES = ("todo", "in_progress", "on_hold")
CLOSED_STATUSES = ("done", "cancelled")
DIR_KEY = "agent-desk.dir"  # git config key: the repository's desk folder


class DeskError(Exception):
    pass


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, text: str) -> None:
    # write then rename: a session reading the desk never sees a half-written file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def git(cwd: Path, *args: str, timeout: float = 5) -> str | None:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def native_path(p: str) -> str:
    """A Git Bash / MSYS path (/c/Users/...) as the Windows path it means; any other path as is."""
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])(/.*)?$", p)
        if m:
            return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return p


def desk_dir(cwd: Path) -> Path | None:
    """The desk folder registered for the repository at cwd (shared by its worktrees), None when unregistered."""
    value = git(cwd, "config", "--get", DIR_KEY)
    return Path(native_path(value)).expanduser() if value else None


def current_branch(cwd: Path) -> str | None:
    return git(cwd, "symbolic-ref", "--short", "-q", "HEAD") or None
