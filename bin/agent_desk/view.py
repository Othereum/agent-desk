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
    page = PAGE.replace("__POLL_MS__", str(POLL_MS)).encode("utf-8")
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


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Desk</title>
<style>
:root {
  --bg: #fbfbfa; --panel: #f3f3f1; --line: #e2e2de; --text: #1d1d1b; --muted: #6b6b66;
  --accent: #2f6fdb; --sel: #e3ebfa; --code: #ecece8; --warn-bg: #fff4d6; --warn: #8a5a00;
  --s-progress: #2f6fdb; --s-todo: #6b6b66; --s-hold: #b7791f; --s-done: #2f855a; --s-cancel: #9a9a94;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #18181a; --panel: #202023; --line: #34343a; --text: #e6e6e3; --muted: #9a9a95;
    --accent: #7aa7f5; --sel: #26324a; --code: #2a2a2f; --warn-bg: #3a2f14; --warn: #f0c46b;
    --s-progress: #7aa7f5; --s-todo: #a5a5a0; --s-hold: #e0a84a; --s-done: #6cc28f; --s-cancel: #77776f;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { background: var(--bg); color: var(--text); font: 14px/1.55 system-ui, -apple-system, "Segoe UI", "Malgun Gothic", sans-serif; }
button { font: inherit; color: inherit; }
.app { display: grid; grid-template-columns: 340px 1fr; height: 100vh; }
aside { background: var(--panel); border-right: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; }
.tools { padding: 12px; border-bottom: 1px solid var(--line); display: grid; gap: 8px; }
.tools input { width: 100%; padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--text); font: inherit; }
.seg { display: flex; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.seg button { flex: 1; border: 0; background: transparent; padding: 4px 0; cursor: pointer; }
.seg button + button { border-left: 1px solid var(--line); }
.seg button.on { background: var(--sel); color: var(--accent); font-weight: 600; }
.tools select { width: 100%; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--text); font: inherit; font-weight: 600; }
.list { overflow-y: auto; flex: 1; padding: 6px 0 16px; }
.empty { color: var(--muted); padding: 8px 12px; font-size: 13px; }
.task { display: block; width: 100%; border: 0; background: transparent; text-align: left; padding: 5px 12px; cursor: pointer; }
.task.child { padding-left: 30px; }
.task:hover { background: color-mix(in srgb, var(--sel) 50%, transparent); }
.task.sel { background: var(--sel); }
.task .t { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task .m { display: flex; gap: 8px; align-items: center; font-size: 12px; color: var(--muted); }
.task.closed .t { color: var(--muted); }
.st { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; white-space: nowrap; }
.st::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.st.in_progress { color: var(--s-progress); } .st.todo { color: var(--s-todo); } .st.on_hold { color: var(--s-hold); }
.st.done { color: var(--s-done); } .st.cancelled { color: var(--s-cancel); } .st.bad { color: var(--warn); }
main { overflow-y: auto; min-width: 0; }
.banner { position: sticky; top: 0; z-index: 2; background: var(--warn-bg); color: var(--warn); padding: 8px 24px; font-size: 13px; }
.doc { max-width: 900px; padding: 20px 32px 64px; }
.placeholder { color: var(--muted); padding: 48px 32px; }
.head h1 { font-size: 22px; margin: 0 0 6px; line-height: 1.3; }
.props { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 8px 0 0; font-size: 13px; }
.props dt { color: var(--muted); }
.props dd { margin: 0; min-width: 0; }
.props dd > div + div { margin-top: 2px; }
.props a { color: var(--accent); text-decoration: none; }
.props a:hover { text-decoration: underline; }
.props .st { margin-left: 6px; }
.props dd > .st:first-child { margin-left: 0; }
.props code { font-size: 12px; }
.copy { margin-left: 6px; padding: 0 6px; font-size: 11px; line-height: 18px; border: 1px solid var(--line); border-radius: 4px; background: var(--bg); color: var(--muted); cursor: pointer; vertical-align: 1px; }
.copy:hover { color: var(--accent); border-color: var(--accent); }.tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); margin: 16px 0 4px; }
.tabs button { border: 0; background: transparent; padding: 6px 12px; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px; color: var(--muted); }
.tabs button.on { color: var(--text); border-bottom-color: var(--accent); font-weight: 600; }
.tabs button:disabled { opacity: .45; cursor: default; }
.warn { background: var(--warn-bg); color: var(--warn); padding: 8px 12px; border-radius: 6px; margin: 12px 0; font-size: 13px; }
.md h1, .md h2, .md h3, .md h4 { line-height: 1.3; margin: 1.4em 0 .5em; }
.md h1 { font-size: 20px; } .md h2 { font-size: 17px; padding-bottom: 4px; border-bottom: 1px solid var(--line); } .md h3 { font-size: 15px; } .md h4 { font-size: 14px; }
.md p, .md ul, .md ol, .md pre, .md table, .md blockquote { margin: .5em 0; }
.md ul, .md ol { padding-left: 1.5em; }
.md li { margin: .15em 0; }
.md code { background: var(--code); padding: 1px 5px; border-radius: 4px; font: 12.5px/1.5 ui-monospace, Consolas, monospace; }
.md pre { background: var(--code); padding: 10px 12px; border-radius: 6px; overflow-x: auto; }
.md pre code { background: none; padding: 0; }
.md table { border-collapse: collapse; display: block; overflow-x: auto; }
.md th, .md td { border: 1px solid var(--line); padding: 4px 10px; text-align: left; vertical-align: top; }
.md blockquote { border-left: 3px solid var(--line); padding-left: 12px; color: var(--muted); }
.md a { color: var(--accent); }
.md hr { border: 0; border-top: 1px solid var(--line); }
.files a { display: block; padding: 3px 0; color: var(--accent); }
.dochead { display: flex; gap: 12px; align-items: center; margin: 12px 0; font-size: 13px; }
.dochead .back { border: 0; background: transparent; padding: 0; cursor: pointer; color: var(--accent); font: inherit; }
.dochead a { color: var(--muted); margin-left: auto; }
@media (max-width: 760px) {
  .app { grid-template-columns: 1fr; grid-template-rows: 45vh 1fr; }
  aside { border-right: 0; border-bottom: 1px solid var(--line); }
  .doc { padding: 16px; }
}
</style>
</head>
<body>
<div class="app">
  <aside>
    <div class="tools">
      <select id="desk" title="Desk" hidden></select>
      <input id="q" type="search" placeholder="Search titles and IDs" autocomplete="off">
      <div class="seg" id="filter">
        <button data-f="open">Open</button><button data-f="closed">Closed</button><button data-f="all">All</button>
      </div>
    </div>
    <div class="list" id="list"></div>
  </aside>
  <main id="main"><div class="placeholder">Loading…</div></main>
</div>
<script>
const POLL_MS = __POLL_MS__;
const STATUS = { in_progress: "in progress", todo: "todo", on_hold: "on hold", done: "done", cancelled: "cancelled" };
const TABS = [["state.md", "State"], ["decisions.md", "Decisions"], ["log.md", "Log"]];
const store = {
  get(k, d) { try { const v = localStorage.getItem("adesk-view." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("adesk-view." + k, JSON.stringify(v)); } catch (e) {} },
};
let data = null, desk = null, filter = store.get("filter", "open"), query = "";
let sel = null, tab = "state.md", taskData = null, desksText = "", taskText = "", offline = false, busy = false;
// doc: the Markdown attachment shown in the Files tab; docText: null while loading, false if it is gone
let doc = null, docText = null, inDoc = false;

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = encodeURIComponent;
const statusHtml = (t) => t.error ? '<span class="st bad">invalid</span>' : `<span class="st ${esc(t.status)}">${esc(STATUS[t.status] || t.status)}</span>`;
const findDesk = (name) => data && data.desks.find((d) => d.name === name);
const findTask = (desk, id) => { const d = findDesk(desk); return d && d.tasks.find((t) => t.id === id); };

// ---------------------------------------------------------------- polling

async function tick() {
  if (busy) return;
  busy = true;
  try {
    const r = await fetch("api/desks", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    const text = await r.text();
    if (text !== desksText) {
      desksText = text;
      data = JSON.parse(text);
      if (!sel) pickInitial();
      if (!findDesk(desk)) desk = data.desks.length ? data.desks[0].name : null;
      renderDesks();
      renderList();
      if (sel) renderTask();
    }
    if (sel) {
      const rt = await fetch(`api/task?desk=${enc(sel.desk)}&id=${enc(sel.id)}`, { cache: "no-store" });
      const tt = rt.ok ? await rt.text() : "";
      if (tt !== taskText) { taskText = tt; taskData = tt ? JSON.parse(tt) : null; renderTask(); }
    }
    if (sel && doc) {
      const name = doc;
      const rd = await fetch(fileUrl(name), { cache: "no-store" });
      const dt = rd.ok ? await rd.text() : false;
      if (name === doc && dt !== docText) { docText = dt; renderTask(); }
    }
    setOffline(false);
  } catch (e) {
    setOffline(true);
  } finally {
    busy = false;
  }
}

function setOffline(v) {
  if (v === offline) return;
  offline = v;
  renderTask();
}

// ---------------------------------------------------------------- selection

// #<desk>/<task id>, or #<desk>/<task id>/files/<name> while an attachment is open
function parseHash(h) {
  const m = decodeURIComponent(h).match(/^([^/]+)\/([^/]+)(?:\/files\/(.+))?$/);
  return m && { desk: m[1], id: m[2], doc: m[3] || null };
}

function taskHash() {
  return "#" + enc(sel.desk) + "/" + enc(sel.id) + (doc ? "/files/" + enc(doc) : "");
}

const docHash = (name) => "#" + enc(sel.desk) + "/" + enc(sel.id) + "/files/" + enc(name);
const isMd = (name) => /\.(md|markdown)$/i.test(name);

function pickInitial() {
  const h = parseHash(location.hash.slice(1));
  if (h && findTask(h.desk, h.id)) {
    desk = h.desk;
    sel = { desk: h.desk, id: h.id };
    if (h.doc) { tab = "files"; doc = h.doc; }
    return;
  }
  showDesk(data.desks.length ? data.desks[0].name : null, true);
}

// Switch to a desk and show its latest task in progress (or open task), if any.
function showDesk(name, replace) {
  desk = name;
  const d = findDesk(name);
  const t = d && (d.tasks.filter((t) => t.status === "in_progress").pop() || d.tasks.filter((t) => t.open).pop());
  if (t) { select(name, t.id, replace); return; }
  sel = null;
  history.replaceState(null, "", location.pathname);
  renderDesks();
  renderList();
  renderTask();
}

// The tab is kept in the history entry being left, so that Back returns to the tab a link was followed from.
function navigate(replace) {
  if (replace) history.replaceState(null, "", taskHash());
  else history.pushState(null, "", taskHash());
}

function select(deskName, id, replace, name = null) {
  if (sel && sel.desk === deskName && sel.id === id) { if (name) openDoc(name, replace); return; }
  if (sel && !replace) history.replaceState({ tab }, "");
  desk = deskName;
  sel = { desk: deskName, id };
  tab = name ? "files" : "state.md";
  doc = name;
  docText = null;
  taskData = null;
  taskText = "";
  navigate(replace);
  renderDesks();
  renderList();
  renderTask();
  $("#main").scrollTop = 0;
  tick();
}

function openDoc(name, replace) {
  if (!replace) history.replaceState({ tab }, "");
  tab = "files";
  doc = name;
  docText = null;
  navigate(replace);
  renderTask();
  $("#main").scrollTop = 0;
  tick();
}

function closeDoc() {
  history.replaceState({ tab }, "");
  doc = null;
  docText = null;
  history.pushState({ tab: "files" }, "", taskHash());
  renderTask();
}

window.addEventListener("popstate", (e) => {
  const h = parseHash(location.hash.slice(1));
  if (!h) return;
  if (sel && sel.desk === h.desk && sel.id === h.id) {
    doc = h.doc;
    docText = null;
    tab = doc ? "files" : "state.md";
  } else {
    sel = null;
    select(h.desk, h.id, true, h.doc);
  }
  if (!h.doc && e.state && e.state.tab) tab = e.state.tab;
  renderTask();
  tick();
});

// ---------------------------------------------------------------- list

function visible(t) {
  if (filter === "open" && !t.open && !t.error) return false;
  if (filter === "closed" && (t.open || t.error)) return false;
  return !query || (t.id + " " + t.title).toLowerCase().includes(query);
}

const RANK = { in_progress: 0, todo: 1, on_hold: 2, done: 4, cancelled: 5 };
const rank = (t) => (t.error ? 3 : RANK[t.status] ?? 3);
function order(a, b) {
  return (rank(a) - rank(b)) || (a.id < b.id ? 1 : a.id > b.id ? -1 : 0);
}

// The desk picker appears only when there is another desk to switch to.
function renderDesks() {
  const box = $("#desk");
  box.hidden = !data || data.desks.length < 2;
  if (box.hidden) return;
  box.innerHTML = data.desks.map((d) => `<option value="${esc(d.name)}"${d.name === desk ? " selected" : ""}>${esc(d.name)}</option>`).join("");
}
$("#desk").addEventListener("change", (e) => showDesk(e.target.value));

function renderList() {
  const d = data && findDesk(desk);
  if (!d) { $("#list").innerHTML = data ? '<div class="empty">No desks</div>' : ""; return; }
  if (d.legacy) { $("#list").innerHTML = '<div class="empty">Older data format: run adesk migrate</div>'; return; }
  const shown = d.tasks.filter(visible).sort(order);
  const ids = new Set(shown.map((t) => t.id));
  let html = "";
  for (const t of shown) {
    if (t.parent && ids.has(t.parent)) continue;
    html += taskItem(d.name, t, false);
    for (const c of shown) if (c.parent === t.id) html += taskItem(d.name, c, true);
  }
  $("#list").innerHTML = html || `<div class="empty">${query ? "No matches" : `No ${filter === "all" ? "" : filter + " "}tasks`}</div>`;
}

function taskItem(desk, t, child) {
  const on = sel && sel.desk === desk && sel.id === t.id;
  return `<button class="task${child ? " child" : ""}${on ? " sel" : ""}${t.open ? "" : " closed"}" data-desk="${esc(desk)}" data-id="${esc(t.id)}">`
    + `<span class="t">${esc(t.title || t.id)}</span><span class="m">${statusHtml(t)}<span>${esc(t.id)}</span></span></button>`;
}

$("#list").addEventListener("click", (e) => {
  const item = e.target.closest(".task");
  if (item) select(item.dataset.desk, item.dataset.id);
});

$("#q").addEventListener("input", (e) => { query = e.target.value.trim().toLowerCase(); renderList(); });

function renderFilter() {
  for (const b of document.querySelectorAll("#filter button")) b.classList.toggle("on", b.dataset.f === filter);
}
$("#filter").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  filter = b.dataset.f;
  store.set("filter", filter);
  renderFilter();
  renderList();
});

// ---------------------------------------------------------------- task

function renderTask() {
  const main = $("#main");
  const banner = offline ? '<div class="banner">The viewer has stopped. Run <code>adesk view</code> again, or ask Claude to open the desk.</div>' : "";
  if (!sel) {
    main.innerHTML = banner + `<div class="placeholder">${data ? "Select a task." : "Loading…"}</div>`;
    return;
  }
  const t = findTask(sel.desk, sel.id);
  if (!t) {
    main.innerHTML = banner + `<div class="placeholder">${esc(sel.id)} is no longer in ${esc(sel.desk)}.</div>`;
    return;
  }
  const scroll = main.scrollTop;
  const taskLink = (x) => `<a href="#${enc(sel.desk)}/${enc(x.id)}" title="${esc(x.id)}">${esc(x.title || x.id)}</a>`;
  const rows = [["Status", statusHtml(t)], ["ID", `<code>${esc(t.id)}</code><button class="copy" data-copy="${esc(t.id)}" title="Copy the task ID">Copy</button>`]];
  if (t.parent) { const p = findTask(sel.desk, t.parent); rows.push(["Parent", p ? taskLink(p) : esc(t.parent)]); }
  if (t.branches.length) rows.push([t.branches.length > 1 ? "Branches" : "Branch", t.branches.map((b) => `<div><code>${esc(b)}</code></div>`).join("")]);
  const children = findDesk(sel.desk).tasks.filter((c) => c.parent === t.id).sort(order);
  if (children.length) rows.push(["Subtasks", children.map((c) => `<div>${taskLink(c)} ${statusHtml(c)}</div>`).join("")]);
  const props = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

  let body = "";
  if (t.error) body += `<div class="warn">${esc(t.error)}</div>`;
  if (taskData && taskData.strays.length) body += `<div class="warn">Unexpected files in this task folder (possibly sync conflict copies): ${esc(taskData.strays.join(", "))}</div>`;
  const att = taskData ? taskData.attachments : [];
  const tabs = TABS.map(([f, label]) => {
    const missing = taskData && taskData.files[f] === null;
    return `<button data-tab="${f}" class="${tab === f ? "on" : ""}"${missing ? " disabled" : ""}>${label}</button>`;
  }).join("") + (att.length ? `<button data-tab="files" class="${tab === "files" ? "on" : ""}">Files (${att.length})</button>` : "");
  if (!taskData) body += '<div class="placeholder">Loading…</div>';
  else if (tab === "files" && doc) body += docView();
  else if (tab === "files") body += '<div class="files">' + att.map((n) => attachmentLink(n, esc(n))).join("") + "</div>";
  else if (taskData.files[tab] === null) body += `<div class="placeholder">No ${esc(tab)}.</div>`;
  else body += `<div class="md">${md(taskData.files[tab])}</div>`;

  main.innerHTML = banner + `<div class="doc"><div class="head"><h1>${esc(t.title || t.id)}</h1><dl class="props">${props}</dl></div>`
    + `<div class="tabs">${tabs}</div>${body}</div>`;
  main.scrollTop = scroll;
}

function fileUrl(name) {
  return `file?desk=${enc(sel.desk)}&id=${enc(sel.id)}&name=${enc(name)}`;
}

// Markdown attachments open inside the viewer; anything else opens raw in a new tab.
function attachmentLink(name, text) {
  return isMd(name) ? `<a href="${docHash(name)}">${text}</a>` : `<a href="${fileUrl(name)}" target="_blank" rel="noopener">${text}</a>`;
}

function docView() {
  const head = `<div class="dochead"><button class="back">← Files</button><code>${esc(doc)}</code>`
    + `<a href="${fileUrl(doc)}" target="_blank" rel="noopener">Open raw</a></div>`;
  if (docText === null) return head + '<div class="placeholder">Loading…</div>';
  if (docText === false) return head + `<div class="placeholder">No files/${esc(doc)}.</div>`;
  inDoc = true;
  try { return head + `<div class="md">${md(docText)}</div>`; } finally { inDoc = false; }
}

async function copy(button) {
  const text = button.dataset.copy;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const area = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = "Copy"; }, 1200);
}

$("#main").addEventListener("click", (e) => {
  const c = e.target.closest("button.copy");
  if (c) { copy(c); return; }
  const b = e.target.closest(".tabs button");
  if (b && !b.disabled) {
    tab = b.dataset.tab;
    if (doc) { doc = null; docText = null; history.replaceState(null, "", taskHash()); }
    renderTask();
    $("#main").scrollTop = 0;
    return;
  }
  if (e.target.closest("button.back")) { closeDoc(); return; }
  const a = e.target.closest('a[href^="#"]');
  if (a) {
    e.preventDefault();
    const h = parseHash(a.getAttribute("href").slice(1));
    if (h) select(h.desk, h.id, false, h.doc);
  }
});

// ---------------------------------------------------------------- markdown (the subset the records use)

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

function isBlockStart(lines, i) {
  const l = lines[i];
  return /^\s*(```|~~~)/.test(l) || /^#{1,6}\s/.test(l) || /^\s*>/.test(l) || LIST_ITEM.test(l)
    || /^\s*([-*_])(\s*\1){2,}\s*$/.test(l) || (/^\s*\|/.test(l) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]));
}

function md(src) {
  const lines = src.replace(/\r\n?/g, "\n").replace(/<!--[\s\S]*?-->/g, "").split("\n");
  const out = [];
  let i = 0, m;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    if ((m = l.match(/^\s*(```|~~~)/))) {
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(m[1])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
    } else if ((m = l.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/))) {
      out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`);
      i++;
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) {
      out.push("<hr>");
      i++;
    } else if (/^\s*>/.test(l)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${md(buf.join("\n"))}</blockquote>`);
    } else if (/^\s*\|/.test(l) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const row = (s) => s.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
      let h = "<table><thead><tr>" + row(l).map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>";
      i += 2;
      while (i < lines.length && /^\s*\|/.test(lines[i])) h += "<tr>" + row(lines[i++]).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>";
      out.push(h + "</tbody></table>");
    } else if (LIST_ITEM.test(l)) {
      const items = [];
      while (i < lines.length) {
        const x = lines[i];
        if ((m = x.match(LIST_ITEM))) { items.push({ ind: m[1].replace(/\t/g, "    ").length, ord: /\d/.test(m[2]), text: m[3] }); i++; }
        else if (!x.trim() && i + 1 < lines.length && LIST_ITEM.test(lines[i + 1])) i++;
        else if (/^\s+\S/.test(x)) { items[items.length - 1].text += " " + x.trim(); i++; }
        else break;
      }
      out.push(listHtml(items));
    } else {
      const buf = [l];
      i++;
      while (i < lines.length && lines[i].trim() && !isBlockStart(lines, i)) buf.push(lines[i++]);
      out.push(`<p>${buf.map(inline).join("<br>")}</p>`);
    }
  }
  return out.join("\n");
}

function listHtml(items) {
  let idx = 0;
  function level(ind) {
    const tag = items[idx].ord ? "ol" : "ul";
    let h = `<${tag}>`;
    while (idx < items.length && items[idx].ind >= ind) {
      if (items[idx].ind > ind) { h += level(items[idx].ind); continue; }
      const text = items[idx++].text.replace(/^\[([ xX])\]\s+/, (_, c) => (c === " " ? "☐ " : "☑ "));
      h += "<li>" + inline(text);
      if (idx < items.length && items[idx].ind > ind) h += level(items[idx].ind);
      h += "</li>";
    }
    return h + `</${tag}>`;
  }
  let html = "";
  while (idx < items.length) html += level(items[idx].ind);
  return html;
}

function inline(s) {
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = esc(s);
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, text, url) => link(url, text));
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (_, pre, url) => pre + link(url, url));
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/~~(.+?)~~/g, "<del>$1</del>");
  s = taskLinks(s);
  return s.replace(/\u0000(\d+)\u0000/g, (_, n) => `<code>${esc(codes[n])}</code>`);
}

function link(url, text) {
  const raw = url.replace(/&amp;/g, "&");
  if (/^(https?:|mailto:)/i.test(raw)) return `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  const rel = raw.replace(/^\.\//, "");
  // Inside an attachment, a bare relative name points at a sibling in files/.
  let name = rel.startsWith("files/") ? rel.slice(6) : inDoc && /^[^:#/?]+$/.test(rel) ? rel : null;
  if (!sel || !name) return text;
  try { name = decodeURIComponent(name); } catch (e) {}
  return attachmentLink(name, text);
}

// Task IDs of the same desk written in the records become links, outside tags and existing links.
function taskLinks(s) {
  const desk = sel && findDesk(sel.desk);
  if (!desk) return s;
  const titles = new Map(desk.tasks.map((t) => [t.id, t.title]));
  let inLink = 0;
  return s.split(/(<[^>]+>)/).map((part) => {
    if (part.startsWith("<")) { if (/^<a\b/.test(part)) inLink++; else if (part === "</a>") inLink--; return part; }
    if (inLink) return part;
    return part.replace(/\b\d{6}-[a-z0-9][a-z0-9-]*/g, (id) => titles.has(id) && id !== sel.id
      ? `<a href="#${enc(sel.desk)}/${enc(id)}" title="${esc(titles.get(id))}">${id}</a>` : id);
  }).join("");
}

renderFilter();
tick();
setInterval(tick, POLL_MS);
</script>
</body>
</html>
"""
