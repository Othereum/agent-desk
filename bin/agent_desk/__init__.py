"""agent-desk CLI: deterministic operations on the agent work-record store.

Data root: $AGENT_DESK_ROOT, or ~/agent-desk when unset.
Requires Python 3.11+ (tomllib). Standard library only.

Modules, each importing only from those before it:
core (constants, errors, io) → frontmatter → gitinfo → model (projects, tasks, relations, session task)
→ index → doctor → hooks → cli (commands, argparse).

Hooks run on every tool use and each import adds startup time: the hook path imports
hooks only (not cli), argparse/shutil/subprocess are imported where used, and
dataclasses/tempfile are avoided.
"""

import json
import sys

HOOK_EVENTS = ("session-start", "pre-tool-use", "post-tool-use", "user-prompt-submit")


HOOK_FAILURE_EFFECT = {  # what the user lost, by event
    "session-start": "태스크 기록 주입이 빠졌습니다",
    # the tool call goes ahead unchecked: a desk file may have been edited by Edit/Write
    "pre-tool-use": "데스크 파일 편집 차단이 동작하지 않았습니다(이 도구 호출은 검사 없이 진행됨)",
    "post-tool-use": "worktree 전환 알림(또는 데스크 파일 검사)이 빠졌습니다",
    "user-prompt-submit": "작업 위치 변경 알림이 빠졌습니다",
}


def hook_failure(event: str, error: BaseException, stage: str = "") -> None:
    """Tell the user a hook failed: stderr of a hook that exits 0 is not shown anywhere, so a
    broken hook would silently stop all injection (e.g. a syntax error while editing the plugin)."""
    effect = HOOK_FAILURE_EFFECT.get(event, "기록 주입이 빠졌습니다")
    message = f"[agent-desk] hook {event}{' ' + stage if stage else ''} 실패, {effect}: {type(error).__name__}: {error}"
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

    # hooks run on every tool use: skip importing the commands and building the argparse tree
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "hook" and argv[1] in HOOK_EVENTS:
        try:
            from .hooks import cmd_hook
        except Exception as e:  # a hook must never break the session, but must not fail silently
            hook_failure(argv[1], e, stage="로드")
            return 0
        cmd_hook(argv[1])
        return 0

    from .cli import run
    return run(argv)
