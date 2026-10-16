"""Constants, errors, data root and file io."""

from __future__ import annotations

import datetime as dt
import os
import tomllib
from pathlib import Path


SCHEMA_VERSION = 1
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
TASK_FILES = ("state.md", "decisions.md", "log.md")
OPEN_STATUSES = ("active", "paused")
ALL_STATUSES = ("active", "paused", "done", "abandoned")
DEFAULT_ID_TEMPLATE = "{yymmdd}-{branch}"
DEFAULT_SHARED_BRANCHES = ["main", "master", "develop"]


class DeskError(Exception):
    pass


def desk_root() -> Path:
    env = os.environ.get("AGENT_DESK_ROOT")
    return Path(env).expanduser() if env else Path.home() / "agent-desk"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def ensure_root() -> Path:
    root = desk_root()
    marker = root / "desk.toml"
    if not marker.exists():
        write_text(marker, f"schema_version = {SCHEMA_VERSION}\n")
    else:
        version = tomllib.loads(read_text(marker)).get("schema_version")
        if version != SCHEMA_VERSION:
            raise DeskError(
                f"데이터 스키마 버전 불일치: 데이터={version}, 플러그인={SCHEMA_VERSION} ({marker})")
    return root


def today() -> str:
    return dt.date.today().isoformat()


def now_minute() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")
