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

REMOVED_COMMAND = ("The adesk recording commands (focus, new, put, append, log, status, close, ...) were removed in 0.13. "
                   "Edit the desk files directly (agent-desk:desk skill). "
                   "Remaining command: adesk migrate [--dry-run] [--root DIR]")


def hook_failure(event: str, error: BaseException) -> None:
    """Tell the user a hook failed: stderr of a hook that exits 0 is not shown anywhere, so a
    broken hook would silently stop the injection (e.g. a syntax error while editing the plugin)."""
    message = f"[agent-desk] hook {event} failed, the task list was not injected: {type(error).__name__}: {error}"
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
