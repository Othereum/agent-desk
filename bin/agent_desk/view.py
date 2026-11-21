"""`adesk view`: a read-only page in the browser listing the tasks of every desk next to this
repository's desk, with each task's files, refreshed live.

`adesk view` starts the server as a detached process and returns once the page is up, so no
terminal has to stay open. The server reads the files on every request (nothing is cached or
watched), listens on 127.0.0.1 only, and answers only under a random token.

Browsers freeze or unload hidden tabs, so a page's polling cannot tell whether it is still open:
the server does not stop by itself. Instead one server runs per set of desks, and its address is
kept in a state file so that `adesk view` reuses a running server, replaces one running other code
(after a plugin update), and starts a new one at the same address, which lets a page left open
reconnect.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .core import FILES_DIR, TASK_FILES, DeskError, desk_dir, native_path, read_text
from .desk import Task, is_legacy, load_tasks, stray_files
from .frontmatter import split_frontmatter

POLL_MS = 2000
START_TIMEOUT = 10
PING_TIMEOUT = 1  # a refused connection to localhost takes about 2 seconds on Windows
REPLACE_WAIT = 3
PAGE_FILE = Path(__file__).with_name("view.html")
STATE_DIR = Path(tempfile.gettempdir()) / "agent-desk-view"


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="adesk view",
        description="Open a live, read-only view of the desks in the browser. Without FOLDER, shows "
                    "every desk in the folder that holds this repository's desk.")
    parser.add_argument("folder", nargs="?", metavar="FOLDER",
                        help="a desk folder, or a folder holding desk folders")
    parser.add_argument("--no-open", action="store_true", help="print the address without opening a browser")
    parser.add_argument("--shortcut", choices=["menu", "desktop", "both"],
                        help="instead of opening, add an entry that opens these desks without Claude Code to the "
                             "start menu (app launcher), the desktop or both; one per folder of desks, run again "
                             "to replace it")
    parser.add_argument("--name", help="the entry's name with --shortcut (default: from the folder and its parent)")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)  # the detached server itself
    parser.add_argument("--current", help=argparse.SUPPRESS)
    parser.add_argument("--state", help=argparse.SUPPRESS)
    parser.add_argument("--replace", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.serve:
        return serve(Path(args.folder), args.current, Path(args.state) if args.state else None, args.replace)
    try:
        root, current = resolve_root(args.folder, Path.cwd())
        if args.shortcut:
            from .shortcut import create
            places = ["menu", "desktop"] if args.shortcut == "both" else [args.shortcut]
            created = "\n".join(f"Created {p}" for p in create(root, args.name, places))
            print(f"{created}\nIt opens the desks in {root} without Claude Code.")
            return 0
        url = start_server(root, current)[0]
    except (DeskError, OSError, ValueError) as e:
        print(f"adesk view: {e}", file=sys.stderr)
        return 1
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)
    print(f"Desk viewer: {url}\nIt keeps running in the background; `adesk view` reuses it, or brings it back "
          "at the same address if it was stopped (an open page then reconnects).")
    return 0


# ---------------------------------------------------------------- desks

def is_desk(folder: Path) -> bool:
    """A folder holding at least one task folder (a subfolder with state.md)."""
    try:
        return any(d.is_dir() and (d / "state.md").is_file() for d in folder.iterdir())
    except OSError:
        return False


def resolve_root(folder: str | None, cwd: Path) -> tuple[Path, str | None]:
    """The folder whose subfolders are the desks to show, and the name of the desk to show first."""
    if folder:
        p = Path(native_path(folder)).expanduser().resolve()
        if not p.is_dir():
            raise DeskError(f"no such folder: {p}")
        if is_desk(p):
            return p.parent, p.name
        if not any(d.is_dir() and is_desk(d) for d in p.iterdir()):
            raise DeskError(f"no desk folders in {p}")
        return p, None
    desk = desk_dir(cwd)
    if desk is None:
        raise DeskError("this repository has no desk registered (git config agent-desk.dir). "
                        "Give a folder instead: adesk view FOLDER")
    if not desk.is_dir():
        raise DeskError(f"the registered desk folder does not exist: {desk}")
    return desk.resolve().parent, desk.name


def desk_names(root: Path, current: str | None) -> list[str]:
    names = sorted((d.name for d in root.iterdir()
                    if d.is_dir() and not d.name.startswith(".") and (d.name == current or is_desk(d))),
                   key=str.casefold)
    if current in names:
        names.remove(current)
        names.insert(0, current)
    return names


def desks_json(root: Path, current: str | None) -> dict:
    desks = []
    for name in desk_names(root, current):
        folder = root / name
        if is_legacy(folder):
            desks.append({"name": name, "legacy": True, "tasks": []})
            continue
        desks.append({"name": name, "legacy": False, "tasks": [task_json(t) for t in load_tasks(folder)]})
    return {"root": root.as_posix(), "current": current, "desks": desks}


def task_json(task: Task) -> dict:
    return {"id": task.id, "title": task.title, "status": task.status, "open": task.is_open,
            "parent": task.parent, "branches": [str(b) for b in task.branches], "error": task.error}


def find_task(root: Path, current: str | None, desk: str, task_id: str) -> Path | None:
    """The task folder, only when both names are listed ones (no path from the request is joined as is)."""
    if desk not in desk_names(root, current):
        return None
    folder = root / desk
    return next((d for d in folder.iterdir() if d.is_dir() and d.name == task_id), None)


def task_files_json(task_dir: Path) -> dict:
    files = {}
    for name in TASK_FILES:
        path = task_dir / name
        try:
            text = read_text(path) if path.is_file() else None
        except (OSError, UnicodeDecodeError) as e:
            text = f"(cannot read {name}: {e})"
        if name == "state.md" and text is not None:
            try:
                text = split_frontmatter(text)[1]
            except DeskError:
                pass
        files[name] = text
    attachments_dir = task_dir / FILES_DIR
    attachments = sorted(p.name for p in attachments_dir.iterdir() if p.is_file()) if attachments_dir.is_dir() else []
    return {"files": files, "attachments": attachments, "strays": stray_files(Task(task_dir, {}))}


# ---------------------------------------------------------------- starting

def state_file(root: Path, current: str | None) -> Path:
    key = hashlib.sha1(f"{root.as_posix()}\n{current or ''}".encode("utf-8")).hexdigest()[:16]
    return STATE_DIR / f"{key}.json"


def read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def page_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/{token}/"


@functools.cache
def code_fingerprint() -> str:
    """A hash of the code the server runs, so that a server left running from before a plugin
    update is replaced instead of reused."""
    digest = hashlib.sha256()
    package = Path(__file__).parent
    for path in sorted([*package.glob("*.py"), PAGE_FILE]):
        digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def ask(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=PING_TIMEOUT) as r:
            return r.read().decode("utf-8", "replace") if r.status == 200 else None
    except (OSError, ValueError):
        return None


def start_server(root: Path, current: str | None) -> tuple[str, subprocess.Popen | None]:
    """The page address of the server already running these desks with this code, or of a newly
    started `adesk view --serve` detached from this process; and that new process (None when reused)."""
    state = state_file(root, current)
    previous = read_state(state)
    port, token = previous.get("port"), previous.get("token")
    replace = False
    if isinstance(port, int) and isinstance(token, str):
        url = page_url(port, token)
        fingerprint = ask(url + "api/ping")
        if fingerprint == code_fingerprint():
            return url, None
        if fingerprint is not None:
            ask(url + "api/stop")
            replace = True
    cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / "desk.py"), "view", "--serve", str(root),
           "--state", str(state)]
    if replace:
        cmd.append("--replace")
    if current:
        cmd += ["--current", current]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:  # leave the caller's job object too, or closing the caller could end the server
            proc = subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            proc = subprocess.Popen(cmd, creationflags=flags, **kwargs)
    else:
        proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
    result: list[str] = []
    reader = threading.Thread(target=lambda: result.append(proc.stdout.readline().decode("utf-8", "replace")),
                              daemon=True)
    reader.start()
    reader.join(START_TIMEOUT)
    line = result[0].strip() if result else ""
    if not result:
        proc.kill()
    proc.stdout.close()
    if line.startswith("URL "):
        return line[4:], proc
    raise DeskError(line.removeprefix("ERROR ") or "the viewer server did not start")


class ViewServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second socket take a port another process is listening on.
    allow_reuse_address = os.name != "nt"


def bind(root: Path, current: str | None, state: Path | None, replace: bool = False) -> ViewServer:
    """A server at the address of the previous one for these desks when it is free, so a page left
    open reconnects; otherwise at a new port and token. When replacing a server that was just asked
    to stop, wait a little for it to let go of the port."""
    previous = read_state(state) if state else {}
    token, port = previous.get("token"), previous.get("port")
    server = None
    if isinstance(token, str) and isinstance(port, int):
        deadline = time.monotonic() + (REPLACE_WAIT if replace else 0)
        while server is None:
            try:
                server = ViewServer(("127.0.0.1", port), make_handler(root, current, token))
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
    else:
        token = secrets.token_urlsafe(16)
    server = server or ViewServer(("127.0.0.1", 0), make_handler(root, current, token))
    server.token = token
    return server


def serve(root: Path, current: str | None, state: Path | None = None, replace: bool = False) -> int:
    out = sys.stdout
    try:
        server = bind(root, current, state, replace)
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}", file=out, flush=True)
        return 1
    url = page_url(server.server_port, server.token)
    if state:
        try:
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({"port": server.server_port, "token": server.token}), encoding="utf-8")
        except OSError:
            pass  # only reuse is lost
    print(f"URL {url}", file=out, flush=True)
    # Nobody reads further: point stdout and stderr at nothing so the starter's pipe closes.
    devnull = os.open(os.devnull, os.O_WRONLY)
    for fd in (1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass
    server.serve_forever()
    server.server_close()
    return 0


# ---------------------------------------------------------------- serving

def make_handler(root: Path, current: str | None, token: str):
    page = PAGE_FILE.read_text(encoding="utf-8").replace("__POLL_MS__", str(POLL_MS)).encode("utf-8")
    prefix = f"/{token}/"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == prefix[:-1]:
                self.send_response(301)
                self.send_header("Location", prefix)
                self.end_headers()
                return
            if not url.path.startswith(prefix):
                return self.send(404, b"not found", "text/plain")
            route, query = url.path[len(prefix):], parse_qs(url.query)
            arg = lambda k: (query.get(k) or [""])[0]
            try:
                if route == "":
                    return self.send(200, page, "text/html; charset=utf-8")
                if route == "api/ping":
                    return self.send(200, code_fingerprint().encode("ascii"), "text/plain")
                if route == "api/stop":
                    self.send(200, b"ok", "text/plain")
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if route == "api/desks":
                    return self.send_json(desks_json(root, current))
                task_dir = find_task(root, current, arg("desk"), arg("id"))
                if task_dir is None:
                    return self.send(404, b"no such task", "text/plain")
                if route == "api/task":
                    return self.send_json(task_files_json(task_dir))
                if route == "file":
                    folder = task_dir / FILES_DIR
                    path = next((p for p in folder.iterdir() if p.is_file() and p.name == arg("name")), None) \
                        if folder.is_dir() else None
                    if path is None:
                        return self.send(404, b"no such file", "text/plain")
                    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    # sandbox: an attached html or svg runs without access to this page's origin
                    return self.send(200, path.read_bytes(), kind, {"Content-Security-Policy": "sandbox"})
                return self.send(404, b"not found", "text/plain")
            except OSError as e:
                return self.send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")

        def send_json(self, data: dict):
            self.send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def send(self, code: int, body: bytes, kind: str, headers: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

    Handler.server_version = "adesk-view"
    return Handler
