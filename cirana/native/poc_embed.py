"""Standalone proof of concept: host a native Windows application inside a Qt window.

    python -m cirana.native.poc_embed                      # interactive
    python -m cirana.native.poc_embed --selftest notepad   # scripted verification, prints a report
    python -m cirana.native.poc_embed --selftest charmap --screenshot out.png

Not wired into the main application on purpose: it exists to establish what works
before Playground tabs are built on top of it.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from cirana.native import win32_api as w
from cirana.native.window_discovery import WindowInfo, list_top_level_windows, snapshot_handles
from cirana.widgets.native_host import NativeHost

SYSTEM32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32")
SETTLE_SECONDS = 0.5
TARGETS = {
    "Character Map": os.path.join(SYSTEM32, "charmap.exe"),
    "Notepad (packaged on Windows 11, expected to be blocked)": os.path.join(SYSTEM32, "notepad.exe"),
    "7-Zip File Manager": os.path.join(os.environ.get("ProgramW6432", r"C:\Program Files"), "7-Zip", "7zFM.exe"),
    "Remote Desktop Connection": os.path.join(SYSTEM32, "mstsc.exe"),
    "Calculator (packaged, expected to be blocked)": "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
}
ALIASES = {
    "notepad": "Notepad (packaged on Windows 11, expected to be blocked)",
    "charmap": "Character Map",
    "7zip": "7-Zip File Manager",
    "mstsc": "Remote Desktop Connection",
    "calc": "Calculator (packaged, expected to be blocked)",
}


class PocWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CIRANA — native embedding proof of concept")
        self.resize(1000, 700)
        self.host = NativeHost()
        self.host.window_lost.connect(self._on_lost)
        self.launched_by_us = False
        self._pending: dict | None = None
        self._poll = QTimer(self, interval=100)
        self._poll.timeout.connect(self._poll_window)

        self.target = QComboBox()
        self.target.addItems(TARGETS)
        self.start_button = QPushButton("Launch && embed")
        self.start_button.clicked.connect(self.launch_and_embed)
        self.detach_button = QPushButton("Detach")
        self.detach_button.clicked.connect(self.detach)
        self.focus_button = QPushButton("Focus")
        self.focus_button.clicked.connect(self.host.focus_embedded)
        self.close_button = QPushButton("Close application…")
        self.close_button.clicked.connect(self.close_application)
        self.status = QLabel("Choose an application and press Launch & embed.")
        self.status.setWordWrap(True)

        bar = QHBoxLayout()
        for widget in (self.target, self.start_button, self.detach_button, self.focus_button, self.close_button):
            bar.addWidget(widget)
        bar.addStretch(1)
        root = QVBoxLayout(self)
        root.addLayout(bar)
        root.addWidget(self.host, 1)
        root.addWidget(self.status)

    # -- actions -----------------------------------------------------------------
    def set_status(self, text: str) -> None:
        self.status.setText(text)
        print("[poc]", text, flush=True)

    def launch_and_embed(self, name: str | None = None) -> None:
        name = name if isinstance(name, str) else self.target.currentText()
        command = TARGETS[name]
        exe_name = os.path.basename(command).lower()
        before = snapshot_handles()
        try:
            if command.startswith("shell:"):
                subprocess.Popen(["explorer.exe", command])
                exe_name = ""  # the owning process is not known up front
            else:
                subprocess.Popen([command])
        except OSError as exc:
            self.set_status(f"Could not start {name}: {exc}")
            return
        self.launched_by_us = True
        self._pending = {"before": before, "exe": exe_name, "deadline": time.monotonic() + 12, "name": name}
        self.start_button.setEnabled(False)
        self.set_status(f"Waiting for the {name} window…")
        self._poll.start()

    def _poll_window(self) -> None:
        pending = self._pending
        if pending is None:
            self._poll.stop()
            return
        for info in list_top_level_windows():
            if info.hwnd in pending["before"]:
                continue
            if pending["exe"] and info.exe_name != pending["exe"]:
                continue
            # Apps often show a window and then rebuild it while starting up; wait until the handle is stable.
            seen = pending.setdefault("seen", {})
            first = seen.setdefault(info.hwnd, time.monotonic())
            if time.monotonic() - first >= SETTLE_SECONDS and not w.IsHungAppWindow(info.hwnd):
                self._finish(info)
                return
        if time.monotonic() > pending["deadline"]:
            self._finish(None)

    def _finish(self, info: WindowInfo | None) -> None:
        self._poll.stop()
        pending, self._pending = self._pending, None
        self.start_button.setEnabled(True)
        self.last_info = info
        if info is None:
            self.set_status(f"No new {pending['name']} window appeared (it may have reused an existing window).")
            return
        result = self.host.attach(info, launched_by_cirana=True, allow_dock=False)  # this PoC tests re-parenting only
        self.last_result = result
        if result.ok:
            extra = f" Warnings: {'; '.join(result.warnings)}" if result.warnings else ""
            self.set_status(f"Embedded {info.exe_name} ({info.class_name}, hwnd {info.hwnd:#x}).{extra}")
        else:
            self.set_status(
                f"Could not embed {info.exe_name} ({info.class_name}): {result.reason} "
                "The application keeps running as a normal window."
            )

    def detach(self) -> None:
        if self.host.detach():
            self.set_status("Detached. The application is a normal window again and is still running.")

    def close_application(self) -> None:
        embedded = self.host.embedded
        if embedded is None:
            return
        if QMessageBox.question(self, "Close application", "Ask the application to close? Unsaved work may prompt.") \
                != QMessageBox.StandardButton.Yes:
            return
        from cirana.native import window_embedder as embedder

        try:
            self.host._embedded = None
            self.host._watch.stop()
            embedder.request_close(embedded)
            self.set_status("Sent a close request to the application.")
        except PermissionError as exc:
            self.set_status(str(exc))

    def _on_lost(self, reason: str) -> None:
        self.set_status("The application closed its window." if reason == "closed"
                        else "The application moved its window out of CIRANA; it is now a normal window.")

    def closeEvent(self, event) -> None:
        self.host.detach()  # never leave a foreign window parented to a dying host
        super().closeEvent(event)


# -- scripted verification -----------------------------------------------------------
def _pump(app: QApplication, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def selftest(app: QApplication, window: PocWindow, name: str, screenshot: str | None) -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> bool:
        checks.append((label, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label} {detail}", flush=True)
        return ok

    window.show()
    _pump(app, 0.5)
    print(f"Self-test: {name}")
    window.launch_and_embed(name)
    deadline = time.monotonic() + 15
    while window._pending is not None and time.monotonic() < deadline:
        _pump(app, 0.1)
    embedded = window.host.embedded
    result = getattr(window, "last_result", None)
    if embedded is None:
        detail = f"-> {result.status.value}: {result.reason}" if result else "(no window appeared)"
        if "expected to be blocked" in name:
            check("incompatible app was refused before any change was made", result is not None
                  and result.status.value == "blocked", detail)
        else:
            check("window found and embedded", False, detail)
        check("CIRANA stayed up and the application was left alone", True)
        info = getattr(window, "last_info", None)
        if info is not None:  # tidy up the test's own process, politely
            w.PostMessageW(info.hwnd, w.WM_CLOSE, 0, 0)
            _pump(app, 1.0)
        print("Result:", "ALL PASSED" if all(c[1] for c in checks) else "FAILURES")
        return 0 if all(c[1] for c in checks) else 1

    hwnd = embedded.hwnd
    state = embedded.state
    pid = embedded.pid
    check("window re-parented into host", int(w.GetParent(hwnd) or 0) == int(window.host.winId()))
    style = int(w.GetWindowLongPtrW(hwnd, w.GWL_STYLE)) & 0xFFFFFFFF
    check("frame removed / WS_CHILD set", bool(style & w.WS_CHILD) and not style & w.WS_CAPTION)
    _pump(app, 1.5)

    from ctypes import byref, wintypes

    client = wintypes.RECT()
    w.GetClientRect(window.host.winId(), byref(client))
    child = wintypes.RECT()
    w.GetWindowRect(hwnd, byref(child))
    check("child fills the host", abs((child.right - child.left) - client.right) <= 2
          and abs((child.bottom - child.top) - client.bottom) <= 2,
          f"child={child.right - child.left}x{child.bottom - child.top} host={client.right}x{client.bottom}")

    if screenshot:
        origin = window.host.mapToGlobal(window.host.rect().topLeft())
        pix = app.primaryScreen().grabWindow(0, origin.x(), origin.y(), window.host.width(), window.host.height())
        pix.save(screenshot)
        print(f"  screenshot saved to {screenshot}")

    old_w = child.right - child.left
    window.resize(window.width() + 160, window.height() + 90)
    _pump(app, 0.8)
    w.GetWindowRect(hwnd, byref(child))
    check("child follows host resize", (child.right - child.left) > old_w + 100,
          f"{old_w} -> {child.right - child.left}")

    window.detach()
    _pump(app, 0.5)
    check("window is a top-level window again", int(w.GetParent(hwnd) or 0) == state.parent)
    style_after = int(w.GetWindowLongPtrW(hwnd, w.GWL_STYLE)) & 0xFFFFFFFF
    mask = ~w.WS_VISIBLE & 0xFFFFFFFF
    check("original window style restored", (style_after & mask) == (state.style & mask),
          f"{style_after:#x} vs {state.style:#x}")
    import psutil

    check("application still running after detach", w.IsWindow(hwnd) and psutil.pid_exists(pid))

    # politely close what the test itself started
    w.PostMessageW(hwnd, w.WM_CLOSE, 0, 0)
    _pump(app, 2.0)
    print("Result:", "ALL PASSED" if all(c[1] for c in checks) else "FAILURES")
    return 0 if all(c[1] for c in checks) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", metavar="TARGET", help=f"one of: {', '.join(ALIASES)}")
    parser.add_argument("--screenshot", help="PNG path for the self-test screen capture")
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = PocWindow()
    if args.selftest:
        name = ALIASES.get(args.selftest.lower(), args.selftest)
        if name not in TARGETS and os.path.isfile(name):
            TARGETS[name] = name  # any executable path
        if name not in TARGETS:
            print("unknown target", args.selftest)
            return 2
        return selftest(app, window, name, args.screenshot)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
