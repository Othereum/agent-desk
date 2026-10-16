"""agent-desk CLI: deterministic operations on the agent work-record store.

Data root: $AGENT_DESK_ROOT, or ~/agent-desk when unset.
Requires Python 3.11+ (tomllib). Standard library only.

Modules, each importing only from those before it:
core (constants, errors, io) → frontmatter → gitinfo → model (projects, tasks, relations, focus)
→ index → knowledge → doctor → hooks → cli (commands, argparse).

Hooks run on every tool use and each import adds startup time: the hook path imports
hooks only (not cli), argparse/shutil/subprocess are imported where used, and
dataclasses/tempfile are avoided.
"""

import sys

HOOK_EVENTS = ("session-start", "post-tool-use", "user-prompt-submit")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

    # hooks run on every tool use: skip importing the commands and building the argparse tree
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "hook" and argv[1] in HOOK_EVENTS:
        from .hooks import cmd_hook
        cmd_hook(argv[1])
        return 0

    from .cli import run
    return run(argv)
