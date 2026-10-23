"""Shared fixtures: a throwaway git repository and desk, and running bin/desk.py as Claude Code does.

Child processes get HOME/USERPROFILE, AGENT_DESK_ROOT and the git global config pointed inside a
temp dir, so nothing reads or writes the real desk, git config or legacy data root."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DESK_PY = REPO / "bin" / "desk.py"


def child_env(base: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("AGENT_DESK_", "CLAUDE", "GIT_"))}
    gitconfig = base / "gitconfig"
    gitconfig.write_text("", encoding="utf-8")
    env.update({
        "AGENT_DESK_ROOT": str(base / "legacy"),
        "HOME": str(base / "home"), "USERPROFILE": str(base / "home"),
        "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
    })
    return env


def run(env: dict, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(DESK_PY), *args], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", env=env)


def git(env: dict, cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          encoding="utf-8", env=env, check=True).stdout.strip()


def init_repo(env: dict, path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True)
    git(env, path, "init", "-q", "-b", branch)
    return path


def write_task(desk: Path, task_id: str, title: str = "t", status: str = "in_progress",
               parent: str = "", branches: str = "[]", body: str = "## 목표\n") -> Path:
    d = desk / task_id
    d.mkdir(parents=True)
    (d / "state.md").write_text(f"---\ntitle: {title}\nstatus: {status}\nparent: {parent}\n"
                                f"branches: {branches}\n---\n{body}", encoding="utf-8")
    return d


def session_start(env: dict, cwd: Path) -> tuple[subprocess.CompletedProcess, str | None]:
    r = run(env, "hook", "session-start", stdin=json.dumps({"cwd": str(cwd), "source": "startup"}))
    if not r.stdout.strip():
        return r, None
    return r, json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
