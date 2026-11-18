#!/usr/bin/env python3
"""Take the README screenshots of the desk viewer from a demo desk.

Runs the viewer in this process and captures it with a headless Chrome or Edge, in light and dark.
Usage: python scripts/screenshot.py DESK_FOLDER --task TASK_ID [--browser PATH]
"""

import argparse
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))

from agent_desk.core import DeskError  # noqa: E402
from agent_desk.view import bind, page_url, resolve_root  # noqa: E402

OUT_DIR = REPO / "docs" / "images"
# Wide enough that GitHub barely scales it down, short enough to end right after the Next section.
SIZE = (1056, 755)


def find_browser() -> str | None:
    names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "msedge", "chrome"]
    for name in names:
        if path := shutil.which(name):
            return path
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates += [Path(base) / "Google/Chrome/Application/chrome.exe",
                               Path(base) / "Microsoft/Edge/Application/msedge.exe"]
    elif sys.platform == "darwin":
        candidates += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                       Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    return next((str(p) for p in candidates if p.is_file()), None)


def capture(browser: str, url: str, out: Path, dark: bool) -> None:
    out.unlink(missing_ok=True)
    cmd = [browser, "--headless", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
           f"--window-size={SIZE[0]},{SIZE[1]}",
           # The page fills itself from fetches after load; give them time before the shot.
           "--virtual-time-budget=5000",
           f"--blink-settings=preferredColorScheme={0 if dark else 1}",
           f"--screenshot={out}", url]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    if not out.is_file():
        raise SystemExit(f"screenshot failed: {out}")
    print(out.relative_to(REPO).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", help="the demo desk folder to show")
    parser.add_argument("--task", required=True, help="the task to show selected")
    parser.add_argument("--browser", help="path to Chrome, Edge or Chromium (found automatically otherwise)")
    args = parser.parse_args()
    browser = args.browser or find_browser()
    if not browser:
        print("no Chrome, Edge or Chromium found; pass --browser PATH", file=sys.stderr)
        return 1
    try:
        root, desk = resolve_root(args.folder, Path.cwd())
    except DeskError as e:
        print(e, file=sys.stderr)
        return 1
    if desk is None:
        print("give a desk folder, not a folder of desks", file=sys.stderr)
        return 1
    server = bind(root, desk, None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = page_url(server.server_port, server.token) + f"#{desk}/{args.task}"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        capture(browser, url, OUT_DIR / "viewer-light.png", dark=False)
        capture(browser, url, OUT_DIR / "viewer-dark.png", dark=True)
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
