"""agent-desk: a SessionStart hook that lists the repository's open tasks, and `migrate` for
data of the format before 0.13.

The desk itself is plain markdown that the agent reads and edits with its own file tools
(skills/desk/SKILL.md); nothing here writes it except migrate.
Requires Python 3.11+. Standard library only.

Modules, each importing only from those before it:
core (io, git) → frontmatter → desk (task folders) → hooks, migrate.
"""

import json
import sys

REMOVED_COMMAND = ("adesk 의 기록 명령(focus, new, put, append, log, status, close 등)은 0.13에서 없어졌습니다. "
                   "데스크 파일은 직접 고칩니다(agent-desk:desk 스킬). "
                   "남은 명령: adesk migrate [--dry-run] [--root DIR]")


def hook_failure(event: str, error: BaseException) -> None:
    """Tell the user a hook failed: stderr of a hook that exits 0 is not shown anywhere, so a
    broken hook would silently stop the injection (e.g. a syntax error while editing the plugin)."""
    message = f"[agent-desk] hook {event} 실패, 태스크 목록 주입이 빠졌습니다: {type(error).__name__}: {error}"
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["hook", "session-start"]:
        try:
            from .hooks import cmd_hook
        except Exception as e:
            hook_failure(argv[1], e)
            return 0
        cmd_hook(argv[1])
        return 0
    if argv[:1] == ["hook"]:
        return 0  # an event an older hooks.json still sends (e.g. while the plugin updates)
    if argv[:1] == ["migrate"]:
        from .migrate import run
        return run(argv[1:])
    print(REMOVED_COMMAND, file=sys.stderr)
    return 2
