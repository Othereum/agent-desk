"""Constants, errors, data root and file io."""

from __future__ import annotations

import datetime as dt
import os
import tomllib
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 2
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
TASK_FILES = ("state.md", "decisions.md", "log.md")
OPEN_STATUSES = ("todo", "in_progress", "on_hold")
CLOSED_STATUSES = ("done", "cancelled")
# schema 1 names, read as the new ones until `adesk migrate` rewrites them
LEGACY_STATUSES = {"active": "in_progress", "paused": "on_hold", "abandoned": "cancelled"}
DEFAULT_ID_TEMPLATE = "{yymmdd}-{branch}"
DEFAULT_SHARED_BRANCHES = ["main", "master", "develop"]


class DeskError(Exception):
    pass


def desk_root() -> Path:
    env = os.environ.get("AGENT_DESK_ROOT")
    return Path(env).expanduser() if env else Path.home() / "agent-desk"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def is_write_temp(name: str) -> bool:
    """A file write_text is in the middle of writing (or left by a killed process)."""
    return name.startswith(".") and name.endswith(".tmp")


def write_text(path: Path, text: str) -> None:
    # write then rename: parallel agents read and write the same data root, and a reader
    # must never see a half-written file (an unreadable state.md drops the task from lists)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: another process has the file open for a moment
            if attempt == 49:
                tmp.unlink(missing_ok=True)
                raise
            import time
            time.sleep(0.01)


@contextmanager
def file_lock(name: str, timeout: float = 10.0, stale: float = 30.0):
    """Cross-process lock for read-then-write sequences, such as allocating the next ID.
    The lock file lives in the temp dir, not the data root, so it is never synced."""
    import tempfile
    import time
    path = Path(tempfile.gettempdir()) / "agent-desk-locks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale:  # left by a killed process
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                raise DeskError(f"다른 adesk 명령이 끝나기를 기다리다 시간이 초과되었습니다(잠금 {path})")
            time.sleep(0.02)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def schema_version(root: Path) -> int | None:
    """The data root's schema version, None when desk.toml does not exist yet."""
    marker = root / "desk.toml"
    if not marker.exists():
        return None
    return tomllib.loads(read_text(marker)).get("schema_version")


def write_schema_version(root: Path) -> None:
    write_text(root / "desk.toml", f"schema_version = {SCHEMA_VERSION}\n")


def ensure_root() -> Path:
    root = desk_root()
    version = schema_version(root)
    if version is None:
        write_schema_version(root)
    elif isinstance(version, int) and version < SCHEMA_VERSION:
        raise DeskError(f"데이터가 이전 형식입니다(스키마 {version}, 플러그인 {SCHEMA_VERSION}). "
                        "`adesk migrate` 로 변환한 뒤 다시 실행하세요")
    elif version != SCHEMA_VERSION:
        raise DeskError(f"데이터가 이 플러그인보다 새 형식입니다(스키마 {version}, 플러그인 {SCHEMA_VERSION}). "
                        f"플러그인을 업데이트하세요 ({root / 'desk.toml'})")
    return root


def python_alias() -> str | None:
    """The `python` that hooks.json runs, when it is a Windows app execution alias.

    An alias (...\\Microsoft\\WindowsApps\\python.exe) adds about 0.1s to every start,
    which every hook call pays."""
    if os.name != "nt":
        return None
    import shutil
    found = shutil.which("python")
    return found if found and "\\microsoft\\windowsapps\\" in found.casefold() else None


PYTHON_ALIAS_MESSAGE = ("python이 Windows 앱 실행 별칭({path})으로 실행되어 agent-desk hook마다 약 0.1초가 더 듭니다. "
                        "설정 > 앱 > 고급 앱 설정 > 앱 실행 별칭에서 python.exe·python3.exe를 끄세요. "
                        "Microsoft Store판 Python만 설치되어 있다면 python.org 설치본이나 Python install manager를 쓰세요.")


def today() -> str:
    return dt.date.today().isoformat()


def now_minute() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")
