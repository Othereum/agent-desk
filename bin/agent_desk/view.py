"""`adesk view`: a read-only page in the browser listing the tasks of every desk next to this
repository's desk, with each task's files, refreshed live.

`adesk view` starts the server as a detached process and returns once the page is up, so no
terminal has to stay open. The server reads the files on every request (nothing is cached or
watched), listens on 127.0.0.1 only, answers only under a random token, and stops by itself when
no page has asked for anything for a while (the tab was closed).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .core import FILES_DIR, TASK_FILES, DeskError, desk_dir, native_path, read_text
from .desk import Task, is_legacy, load_tasks, stray_files
from .frontmatter import split_frontmatter

IDLE_SECONDS = 300  # longer than a hidden tab's throttled polling (about once a minute)
POLL_MS = 2000
START_TIMEOUT = 10
PAGE_FILE = Path(__file__).with_name("view.html")


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="adesk view",
        description="Open a live, read-only view of the desks in the browser. Without FOLDER, shows "
                    "every desk in the folder that holds this repository's desk.")
    parser.add_argument("folder", nargs="?", metavar="FOLDER",
                        help="a desk folder, or a folder holding desk folders")
    parser.add_argument("--no-open", action="store_true", help="print the address without opening a browser")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)  # the detached server itself
    parser.add_argument("--current", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.serve:
        return serve(Path(args.folder), args.current)
    try:
        root, current = resolve_root(args.folder, Path.cwd())
        url = start_server(root, current)[0]
    except DeskError as e:
        print(f"adesk view: {e}", file=sys.stderr)
        return 1
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)
    print(f"Desk viewer: {url}\nIt runs in the background and stops by itself a few minutes after the page is closed.")
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

def start_server(root: Path, current: str | None) -> tuple[str, subprocess.Popen]:
    """Start `adesk view --serve` detached from this process; the page address it reports, and the process."""
    cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / "desk.py"), "view", "--serve", str(root)]
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


def serve(root: Path, current: str | None) -> int:
    out = sys.stdout
    try:
        token = secrets.token_urlsafe(16)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, current, token))
        server.last_request = time.monotonic()
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}", file=out, flush=True)
        return 1
    print(f"URL http://127.0.0.1:{server.server_port}/{token}/", file=out, flush=True)
    # Nobody reads further: point stdout and stderr at nothing so the starter's pipe closes.
    devnull = os.open(os.devnull, os.O_WRONLY)
    for fd in (1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass
    threading.Thread(target=stop_when_idle, args=(server,), daemon=True).start()
    server.serve_forever()
    return 0


def stop_when_idle(server) -> None:
    while True:
        time.sleep(5)
        if time.monotonic() - server.last_request > IDLE_SECONDS:
            server.shutdown()
            return


# ---------------------------------------------------------------- serving

def make_handler(root: Path, current: str | None, token: str):
    page = PAGE_FILE.read_text(encoding="utf-8").replace("__POLL_MS__", str(POLL_MS)).encode("utf-8")
    prefix = f"/{token}/"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.server.last_request = time.monotonic()
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
