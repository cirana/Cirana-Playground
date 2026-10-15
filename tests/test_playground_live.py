import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import psutil

CHARMAP = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "charmap.exe")


@unittest.skipUnless(os.path.exists(CHARMAP), "charmap.exe not available")
class LivePlaygroundTests(unittest.TestCase):
    """Real applications in real tabs (Character Map, Remote Desktop, Notepad). Needs an interactive desktop."""

    def test_playground_round_trip(self):
        env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM", "CIRANA_HOME")}
        proc = subprocess.run(
            [sys.executable, "-m", "tests.live_playground_script"],
            capture_output=True, text=True, timeout=240, encoding="utf-8", errors="replace", env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ALL PASSED", proc.stdout)

    def test_hung_hosted_application_never_freezes_cirana(self):
        env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM", "CIRANA_HOME")}
        proc = subprocess.run(
            [sys.executable, "-m", "tests.live_hang_script"],
            capture_output=True, text=True, timeout=240, encoding="utf-8", errors="replace", env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ALL PASSED", proc.stdout)

    def test_hard_killed_cirana_leaves_a_recoverable_trace(self):
        """Kill CIRANA while it hosts Character Map: the app survives without a window and is reported on the next start."""
        from cirana.core.orphans import end_orphan, find_orphans
        from cirana.storage.settings_manager import SettingsManager

        home = tempfile.mkdtemp(prefix="cirana-crash-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM", "CIRANA_HOME")}
        host = subprocess.Popen([sys.executable, "-m", "tests.live_crash_host", home], stdout=subprocess.PIPE,
                                text=True, env=env)
        pid = 0
        try:
            line = host.stdout.readline()
            self.assertIn("STATE embedded", line)
            pid = int(line.split("PID")[1])
            host.kill()  # no chance to detach: the worst case
            host.wait(timeout=10)
            deadline = time.monotonic() + 10
            orphans = []
            while time.monotonic() < deadline and not orphans:
                time.sleep(0.3)
                entries = SettingsManager(home).playground_store.load()["embedded"]
                orphans = find_orphans(entries)
            self.assertEqual([o.pid for o in orphans], [pid], "the hosted process must be reported")
            self.assertTrue(psutil.pid_exists(pid), "finding an orphan must not end it")
            self.assertIn("Ended", end_orphan(orphans[0]))
            deadline = time.monotonic() + 5
            while psutil.pid_exists(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(psutil.pid_exists(pid))
        finally:
            host.stdout.close()
            if host.poll() is None:
                host.kill()
            if pid and psutil.pid_exists(pid):
                psutil.Process(pid).terminate()


if __name__ == "__main__":
    unittest.main()
