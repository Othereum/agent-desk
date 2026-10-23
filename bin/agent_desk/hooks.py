"""SessionStart hook: the open tasks of this repository's desk."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .core import current_branch, desk_dir
from .desk import Task, is_legacy, load_tasks, stray_files

RULES_LINE = ("사용자의 일이 이 중 하나면 그 태스크의 state.md를 읽고 시작한다. "
              "태스크 생성·기록·과거 결정 조회는 agent-desk:desk 스킬을 따른다.")
MIGRATE_LINE = ("agent-desk 데이터가 이전 형식이다. 사용자에게 `adesk migrate` 실행을 한 줄로 제안할 것"
                "(`--dry-run` 으로 바뀔 내용을 먼저 보여 줄 수 있음).")


def session_start_context(payload: dict) -> str | None:
    cwd = Path(payload.get("cwd") or os.getcwd())
    desk = desk_dir(cwd)
    if desk is None:
        from .migrate import legacy_root
        return f"[agent-desk] {MIGRATE_LINE}" if legacy_root() else None
    branch = current_branch(cwd)
    head = f"[agent-desk] 데스크 {desk.as_posix()} · 브랜치 {branch or '(없음)'}"
    if not desk.is_dir():
        return (f"{head}\n경고: git config agent-desk.dir 가 가리키는 폴더가 없다. "
                "사용자에게 알리고, 폴더를 옮겼으면 등록을 고칠 것.")
    if is_legacy(desk):
        return f"{head}\n{MIGRATE_LINE}"
    tasks = load_tasks(desk)
    lines = [head] + task_list(tasks, branch) + warning_lines(tasks)
    return "\n".join(lines + [RULES_LINE])


def task_list(tasks: list[Task], branch: str | None) -> list[str]:
    """Open tasks, each child under its open parent."""
    open_tasks = [t for t in tasks if t.is_open and not t.error]
    if not open_tasks:
        return ["열린 태스크 없음."]
    open_ids = {t.id for t in open_tasks}
    lines = ["--- 열린 태스크 ---"]
    for t in open_tasks:
        if t.parent in open_ids:
            continue
        lines.append(task_line(t, branch, nested=False))
        lines += [task_line(c, branch, nested=True) for c in open_tasks if c.parent == t.id]
    return lines


def task_line(task: Task, branch: str | None, nested: bool) -> str:
    parts = [f"{task.id} ({task.title})", task.status]
    if task.parent and not nested:
        parts.append(f"상위 {task.parent}")
    if branch and branch in task.branches:
        parts.append("이 브랜치")
    return ("  - " if nested else "- ") + " · ".join(parts)


def warning_lines(tasks: list[Task]) -> list[str]:
    lines = [f"경고: {t.id} 의 {t.error}. 목록에서 빠졌다. 사용자에게 알리고 고칠 것." for t in tasks if t.error]
    for t in tasks:
        if t.is_open and not t.error:
            strays = stray_files(t)
            if strays:
                lines.append(f"경고: {t.id} 폴더에 예상하지 않은 파일이 있다(동기화 충돌 사본일 수 있음): "
                             f"{', '.join(strays)}. 사용자에게 알리고 병합 여부를 확인할 것.")
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
