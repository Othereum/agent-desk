"""`adesk view --shortcut`: start menu (app launcher) or desktop entries that open the viewer without Claude Code.

The plugin folder is named after its version, so it changes with every update and the entry does
not point into it. The entry runs a launcher kept in the user's data folder, which names the plugin
folder; the SessionStart hook rewrites it when the folder changes. An update leaves the previous
version's folder in place for a while, so until the next session the entry still opens the viewer,
on the previous code.
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import subprocess
import sys
from pathlib import Path

PLUGIN_BIN = Path(__file__).resolve().parents[1]
APP_NAME = "Agent Desk"

LAUNCHER_TEMPLATE = '''\
# Written by agent-desk (adesk view --shortcut). The SessionStart hook keeps BIN current.
import contextlib, io, sys
from pathlib import Path

BIN = __BIN__


def show(message):
    if sys.platform == "win32":  # started with pythonw: there is no console to print to
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Agent Desk", 0x10)
    else:
        print(message, file=sys.stderr)


if not (Path(BIN) / "agent_desk" / "view.py").is_file():
    show("The agent-desk plugin folder this shortcut used is gone (" + BIN + "). "
         "Start Claude Code once with the plugin installed, then try again.")
    sys.exit(1)
sys.path.insert(0, BIN)
from agent_desk.view import run

errors = io.StringIO()
with contextlib.redirect_stderr(errors):
    code = run(sys.argv[1:])
if code:
    show(errors.getvalue().strip() or "The desk viewer did not start.")
sys.exit(code)
'''


def data_dir() -> Path:
    # From the home folder rather than LOCALAPPDATA, so that tests pointing HOME elsewhere stay off the real one.
    home = Path.home()
    if os.name == "nt":
        return home / "AppData" / "Local" / "agent-desk"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "agent-desk"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "agent-desk"


def launcher_path() -> Path:
    return data_dir() / "view-launcher.py"


def launcher_text(bin_dir: Path) -> str:
    return LAUNCHER_TEMPLATE.replace("__BIN__", repr(str(bin_dir)))


def write_launcher() -> Path:
    path = launcher_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(launcher_text(PLUGIN_BIN), encoding="utf-8")
    return path


def refresh_launcher() -> None:
    """Point a launcher the user has (by creating a shortcut) at this plugin folder."""
    path = launcher_path()
    if not path.is_file():
        return
    text = launcher_text(PLUGIN_BIN)
    if path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def default_name(root: Path) -> str:
    """Two roots are often both named `desk` (one per synced drive), so the parent tells them apart."""
    return f"{APP_NAME} ({root.parent.name} · {root.name})" if root.parent.name else f"{APP_NAME} ({root.name})"


def python_for_shortcut() -> str:
    exe = Path(sys.executable)
    if os.name == "nt":
        windowed = exe.with_name("pythonw.exe")
        if windowed.is_file():
            return str(windowed)
    return str(exe)


def create(root: Path, name: str | None, places: list[str]) -> list[Path]:
    """Create or replace the entries opening the desks in root, in each of places
    ("menu": start menu or app launcher, "desktop"); returns their paths."""
    name = name or default_name(root)
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or not name.strip(" ."):
        raise ValueError(f"not usable as a shortcut name: {name!r}")
    launcher = write_launcher()
    python = python_for_shortcut()
    paths = []
    for place in places:
        folder = place_folder(place)
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            paths.append(windows_shortcut(folder, name, python, launcher, root))
        elif sys.platform == "darwin":
            paths.append(mac_app(folder, name, python, launcher, root))
        else:
            paths.append(linux_desktop_entry(folder, name, python, launcher, root, executable=place == "desktop"))
    return paths


def place_folder(place: str) -> Path:
    home = Path.home()
    if os.name == "nt":
        return windows_shell_folder("Desktop" if place == "desktop" else "Programs")
    if place == "desktop":
        if sys.platform != "darwin":
            try:  # the desktop folder is localized on many Linux desktops
                out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True).stdout.strip()
                if out and Path(out) != home:
                    return Path(out)
            except OSError:
                pass
        return home / "Desktop"
    if sys.platform == "darwin":
        return home / "Applications"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "applications"


def windows_shell_folder(key: str) -> Path:
    """The desktop is often redirected into a synced folder, so it is read from the user's shell folders."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return Path(os.path.expandvars(winreg.QueryValueEx(k, key)[0]))
    except OSError:
        home = Path.home()
        return home / "Desktop" if key == "Desktop" else \
            home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def windows_shortcut(folder: Path, name: str, python: str, launcher: Path, root: Path) -> Path:
    path = folder / f"{name}.lnk"
    # Values go through the environment so that no quoting of paths into the script is needed.
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ADESK_LNK); "
              "$s.TargetPath = $env:ADESK_TARGET; $s.Arguments = $env:ADESK_ARGS; "
              "$s.WorkingDirectory = $env:ADESK_CWD; $s.Description = $env:ADESK_DESC; $s.Save()")
    env = {**os.environ, "ADESK_LNK": str(path), "ADESK_TARGET": python,
           "ADESK_ARGS": subprocess.list2cmdline([str(launcher), str(root)]),
           "ADESK_CWD": str(root), "ADESK_DESC": f"Open the desks in {root}"}
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0 or not path.is_file():
        raise OSError(f"could not create {path}: {(r.stderr or r.stdout).strip()}")
    return path


def mac_app(folder: Path, name: str, python: str, launcher: Path, root: Path) -> Path:
    app = folder / f"{name}.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    exe = macos / "agent-desk-view"
    exe.write_text(f"#!/bin/sh\nexec {shlex.join([python, str(launcher), str(root)])}\n", encoding="utf-8")
    exe.chmod(0o755)
    info = {"CFBundleName": name, "CFBundleDisplayName": name, "CFBundleExecutable": exe.name,
            "CFBundleIdentifier": "agent-desk.view." + re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower(),
            "CFBundlePackageType": "APPL", "LSBackgroundOnly": True}
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    return app


def linux_desktop_entry(folder: Path, name: str, python: str, launcher: Path, root: Path,
                        executable: bool = False) -> Path:
    path = folder / ("agent-desk-" + (re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "view") + ".desktop")
    exec_line = " ".join(desktop_quote(a) for a in (python, str(launcher), str(root)))
    path.write_text("[Desktop Entry]\nType=Application\n"
                    f"Name={name}\nComment=Open the desks in {root}\nExec={exec_line}\n"
                    "Terminal=false\nCategories=Utility;\n", encoding="utf-8")
    if executable:  # file managers launch a .desktop file on the desktop only when it is executable
        path.chmod(0o755)
    return path


def desktop_quote(arg: str) -> str:
    """An Exec argument per the Desktop Entry spec: quoted with \\ " ` $ escaped, then each backslash
    doubled again (the string escape is undone before the quoting), and % doubled."""
    escaped = re.sub(r'([\\"`$])', r"\\\1", arg).replace("\\", "\\\\").replace("%", "%%")
    return f'"{escaped}"'
