"""SessionStart hook: the open tasks of this repository's desk."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .core import current_branch, desk_dir
from .desk import Task, is_legacy, load_tasks, stray_files

RULES_LINE = "Before using the desk (reading, recording or creating tasks), read the agent-desk:desk skill first."
MIGRATE_LINE = ("The agent-desk data is in an older format. Suggest running `adesk migrate` to the user "
                "(`--dry-run` shows the changes first).")


def session_start_context(payload: dict) -> str | None:
    cwd = Path(payload.get("cwd") or os.getcwd())
    desk = desk_dir(cwd)
    if desk is None:
        from .migrate import legacy_root
        return f"[agent-desk] {MIGRATE_LINE}" if legacy_root() else None
    branch = current_branch(cwd)
    head = f"[agent-desk] desk `{display_path(desk)}` · branch `{branch or '(none)'}`"
    if not desk.is_dir():
        return (f"{head}\nWarning: the folder that git config agent-desk.dir points to does not exist. "
                "Tell the user, and fix the registration if the folder was moved.")
    if is_legacy(desk):
        return f"{head}\n{MIGRATE_LINE}"
    tasks = load_tasks(desk)
    lines = [head] + task_list(tasks, branch) + warning_lines(tasks)
    return "\n".join(lines + ["", RULES_LINE])


def display_path(path: Path) -> str:
    """Path with the home directory shortened to `~`."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return path.as_posix()


def task_list(tasks: list[Task], branch: str | None) -> list[str]:
    """Open tasks, each child under its open parent."""
    open_tasks = [t for t in tasks if t.is_open and not t.error]
    if not open_tasks:
        return ["No open tasks."]
    open_ids = {t.id for t in open_tasks}
    lines = ["--- open tasks ---"]
    for t in open_tasks:
        if t.parent in open_ids:
            continue
        lines.append(task_line(t, branch, nested=False))
        lines += [task_line(c, branch, nested=True) for c in open_tasks if c.parent == t.id]
    return lines


def task_line(task: Task, branch: str | None, nested: bool) -> str:
    parts = [f"{task.id} ({task.title})", task.status]
    if task.parent and not nested:
        parts.append(f"parent {task.parent}")
    if branch and branch in task.branches:
        parts.append("on the current branch")
    return ("  - " if nested else "- ") + " · ".join(parts)


def warning_lines(tasks: list[Task]) -> list[str]:
    lines = [f"Warning: {t.id}: {t.error}, so it is left out of the list. Tell the user and fix it." for t in tasks if t.error]
    for t in tasks:
        if t.is_open and not t.error:
            strays = stray_files(t)
            if strays:
                lines.append(f"Warning: unexpected files in the {t.id} folder (possibly sync conflict copies): "
                             f"{', '.join(strays)}. Tell the user and ask whether to merge them.")
    return lines


def cmd_hook(event: str) -> None:
    from . import hook_failure
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        context = session_start_context(payload)
    except Exception as e:  # a hook must never break the session, but must not fail silently
        hook_failure(event, e)
        return
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}},
                         ensure_ascii=False))
