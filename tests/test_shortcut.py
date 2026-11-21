"""python -m unittest discover -s tests"""

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from agent_desk import shortcut
from helpers import child_env, session_start


class Shortcut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.root = self.base / "OneDrive - Company" / "desk"
        patches = [mock.patch.object(Path, "home", return_value=self.home),
                   mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}),
                   mock.patch.object(shortcut, "place_folder", lambda place: self.home / place)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_name_tells_same_named_roots_apart(self):
        self.assertEqual(shortcut.default_name(self.root), "Agent Desk (OneDrive - Company · desk)")

    def test_launcher_runs_the_viewer_of_the_plugin_it_names(self):
        launcher = shortcut.write_launcher()
        r = subprocess.run([sys.executable, str(launcher), "--help"], capture_output=True, text=True,
                           encoding="utf-8", env=child_env(self.base))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--shortcut", r.stdout)

    def test_session_start_repoints_only_an_existing_launcher(self):
        env = child_env(self.base)
        session_start(env, self.base)
        self.assertFalse(shortcut.launcher_path().exists())
        launcher = shortcut.launcher_path()
        launcher.parent.mkdir(parents=True)
        launcher.write_text(shortcut.launcher_text(self.base / "old-version" / "bin"), encoding="utf-8")
        session_start(env, self.base)
        self.assertEqual(launcher.read_text(encoding="utf-8"), shortcut.launcher_text(shortcut.PLUGIN_BIN))

    def test_rejects_names_unusable_as_file_names(self):
        with self.assertRaises(ValueError):
            shortcut.create(self.root, "a/b", ["menu"])

    @unittest.skipUnless(os.name == "nt", "Windows shortcut")
    def test_windows_shortcuts_in_each_place(self):
        paths = shortcut.create(self.root, None, ["menu", "desktop"])
        self.assertEqual([p.parent.name for p in paths], ["menu", "desktop"])
        self.assertEqual(paths[0].name, "Agent Desk (OneDrive - Company · desk).lnk")
        self.assertTrue(all(p.is_file() for p in paths))

    def test_mac_app_bundle(self):
        app = shortcut.mac_app(self.home, "Agent Desk (x)", "/usr/bin/python3", PurePosixPath("/l/view-launcher.py"),
                               PurePosixPath("/d/my desk"))
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        exe = app / "Contents" / "MacOS" / info["CFBundleExecutable"]
        self.assertIn("exec /usr/bin/python3 /l/view-launcher.py '/d/my desk'", exe.read_text(encoding="utf-8"))

    def test_linux_desktop_entry_quotes_exec_arguments(self):
        path = shortcut.linux_desktop_entry(self.home, "Agent Desk (x)", "/usr/bin/python3",
                                            PurePosixPath("/l/view-launcher.py"), PurePosixPath('/d/100% "a"$b'))
        self.assertEqual(path.name, "agent-desk-agent-desk-x.desktop")
        text = path.read_text(encoding="utf-8")
        self.assertIn('Exec="/usr/bin/python3" "/l/view-launcher.py" "/d/100%% \\\\"a\\\\"\\\\$b"\n', text)


if __name__ == "__main__":
    unittest.main()
