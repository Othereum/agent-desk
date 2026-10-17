"""git calls and repository identity (toplevel, branch, remotes)."""

from __future__ import annotations

import re
from pathlib import Path


def git(cwd: Path, *args: str, timeout: float = 5) -> str | None:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def normalize_remote(url: str) -> str:
    u = url.strip()
    u = re.sub(r"^[a-z+]+://", "", u, flags=re.I)
    u = re.sub(r"^[^@/]+@", "", u)
    m = re.match(r"^([^/:]+):(?!\d+/)(.*)$", u)  # scp form host:path
    if m:
        u = f"{m.group(1)}/{m.group(2)}"
    u = re.sub(r"^([^/:]+):\d+/", r"\1/", u)  # drop port
    u = re.sub(r"\.git/?$", "", u).rstrip("/")
    return u.casefold()


def normalize_path(p: str | Path) -> str:
    return str(Path(native_path(str(p))).expanduser().resolve()).replace("\\", "/").rstrip("/").casefold()


def native_path(p: str) -> str:
    """A Git Bash / MSYS path (/c/Users/...) as the Windows path it means; any other path as is."""
    import os
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])(/.*)?$", p)
        if m:
            return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return p


def is_under(root: Path, path: Path) -> bool:
    """True when path is root or inside it (case, separators, symlinks and `..` normalized)."""
    r, p = normalize_path(root), normalize_path(path)
    return p == r or p.startswith(r + "/")


class RepoInfo:
    def __init__(self, toplevel: Path | None, branch: str | None, remotes: list[str] | None = None):
        self.toplevel, self.branch, self.remotes = toplevel, branch, remotes or []


def repo_info(cwd: Path) -> RepoInfo:
    top = git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return RepoInfo(None, None)
    branch = git(cwd, "symbolic-ref", "--short", "-q", "HEAD")
    remotes = []
    for name in (git(cwd, "remote") or "").split():
        url = git(cwd, "remote", "get-url", name)
        if url:
            remotes.append(url)
    return RepoInfo(Path(top), branch or None, remotes)
