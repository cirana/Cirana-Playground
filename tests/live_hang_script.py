"""Live check of the not-responding path: a hosted REAL window stops pumping messages; CIRANA must stay responsive.

    python -m tests.live_hang_script

The hosted window belongs to tests/hang_window_helper.py (a plain Win32 window that blocks its message loop on demand).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

HOME = tempfile.mkdtemp(prefix="cirana-hang-")
os.environ["CIRANA_HOME"] = HOME
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QTimer  # noqa: E402

from cirana.core.playground import SessionState  # noqa: E402
from cirana.main import build_app  # noqa: E402
from cirana.native import win32_api as w  # noqa: E402
from cirana.native.window_discovery import describe_window  # noqa: E402

HANG_SECONDS = 12
CHECKS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label} {detail}", flush=True)
    return ok


def main() -> int:
    app, window, ctx = build_app([])
    window.show()
    window.show_page("playground", save=False)
    trigger = os.path.join(HOME, "hang.trigger")
    helper = subprocess.Popen([sys.executable, "-m", "tests.hang_window_helper", trigger, str(HANG_SECONDS)],
                              stdout=subprocess.PIPE, text=True)
    ticks: list[float] = []
    clock = QTimer()
    clock.setInterval(40)
    clock.timeout.connect(lambda: ticks.append(time.monotonic()))
    clock.start()

    def pump(seconds=0.2, until=None, timeout=30.0) -> bool:
        end = time.monotonic() + (timeout if until else seconds)
        while time.monotonic() < end:
            app.processEvents()
            if until and until():
                return True
            time.sleep(0.01)
        return bool(until and until())

    try:
        hwnd = int(helper.stdout.readline().split()[1])
        pump(1.0)
        manager, view = ctx.playground, window.pages["playground"]
        outcome = manager.adopt_window(describe_window(hwnd))
        pump(until=lambda: manager.get(outcome.session_id).state is not SessionState.LAUNCHING)
        session = manager.get(outcome.session_id)
        page = view._pages[session.id]
        print("Hang checks")
        check("a plain Win32 window is hosted", session.state is SessionState.EMBEDDED, session.message)
        pump(0.8)

        def hang_and_wait() -> float:
            open(trigger, "w").close()  # the helper's message loop blocks now
            started = time.monotonic()
            ok = pump(until=lambda: page.host.hung, timeout=20)
            return time.monotonic() - started if ok else -1.0

        # -- scenario 1: hang, navigate around, wait for the application to recover on its own ---------------------
        ticks.clear()
        noticed = hang_and_wait()
        check("CIRANA notices the hosted application stopped responding", noticed > 0, f"after {noticed:.1f}s")
        pump(0.3)
        check("the tab hides the window behind a not-responding panel",
              page.body.currentWidget() is page.panel and "not responding" in page.panel_title.text())
        t0 = time.monotonic()
        window.show_page("home", save=False)
        pump(0.3)
        window.show_page("playground", save=False)
        pump(0.3)
        view.set_suspended(True)
        pump(0.2)
        view.set_suspended(False)
        pump(0.3)
        switch_time = time.monotonic() - t0
        check("page switches and overlays while the app is hung do not freeze CIRANA", switch_time < 3.0, f"{switch_time:.2f}s")
        check("the hung window stays hidden", not w.IsWindowVisible(hwnd))
        check("it recovers by itself and comes back into the tab",
              pump(until=lambda: not page.host.hung and w.IsWindowVisible(hwnd), timeout=30)
              and page.body.currentWidget() is page.host)
        check("the application was never killed", helper.poll() is None)

        # -- scenario 2: hang, then detach while it is still not responding ----------------------------------------------
        noticed = hang_and_wait()
        check("a second hang is noticed too", noticed > 0, f"after {noticed:.1f}s")
        t0 = time.monotonic()
        view.detach(session.id)
        pump(0.3)
        detach_time = time.monotonic() - t0
        check("detaching a hung window does not freeze CIRANA", detach_time < 4.0, f"{detach_time:.2f}s")
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        check("the UI event loop never stalled for long", bool(gaps) and max(gaps) < 2.5, f"longest stall {max(gaps, default=0):.2f}s")
        recovered = pump(until=lambda: int(w.GetParent(hwnd) or 0) == 0 and not w.IsHungAppWindow(hwnd), timeout=40)
        check("once the application recovers its window is a normal top-level window", recovered)
        pump(0.5)
        check("the application was never killed", helper.poll() is None and bool(w.IsWindow(hwnd)))
        style = int(w.GetWindowLongPtrW(hwnd, w.GWL_STYLE)) & 0xFFFFFFFF
        check("its frame came back", bool(style & w.WS_CAPTION) and not style & w.WS_CHILD)
    finally:
        if helper.poll() is None:
            if "hwnd" in locals() and w.IsWindow(hwnd):
                w.PostMessageW(hwnd, w.WM_CLOSE, 0, 0)
            try:
                helper.wait(timeout=5)
            except subprocess.TimeoutExpired:
                helper.kill()
        helper.stdout.close()
        window.close()
    ok = bool(CHECKS) and all(c[1] for c in CHECKS)
    print("Result:", "ALL PASSED" if ok else "FAILURES", flush=True)
    shutil.rmtree(HOME, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
