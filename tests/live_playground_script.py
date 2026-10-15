"""Live Playground checks against REAL Windows applications. Run by tests/test_playground_live.py in a subprocess.

    python -m tests.live_playground_script

Needs an interactive desktop session. Opens Character Map and Remote Desktop Connection (classic Win32 apps) in
tabs, and Notepad, which on Windows 11 is a packaged app that must never be re-parented: it is docked over its tab.
Every application the script starts is closed again with a polite WM_CLOSE.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

HOME = tempfile.mkdtemp(prefix="cirana-live-")
os.environ["CIRANA_HOME"] = HOME
os.environ.pop("QT_QPA_PLATFORM", None)  # a fake (offscreen) platform would give hosts fake HWNDs

from PySide6.QtWidgets import QApplication  # noqa: E402

from cirana.core.playground import SessionState  # noqa: E402
from cirana.main import build_app  # noqa: E402
from cirana.native import win32_api as w  # noqa: E402

SYSTEM32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32")
CHECKS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label} {detail}", flush=True)
    return ok


def pump(app: QApplication, seconds: float = 0.2, until=None, timeout: float = 20.0) -> bool:
    end = time.monotonic() + (timeout if until else seconds)
    while time.monotonic() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.01)
    return bool(until and until())


def parent_of(hwnd: int) -> int:
    return int(w.GetParent(hwnd) or 0)


def close_politely(app: QApplication, hwnd: int) -> None:
    if w.IsWindow(hwnd):
        w.PostMessageW(hwnd, w.WM_CLOSE, 0, 0)
        pump(app, until=lambda: not w.IsWindow(hwnd), timeout=5)


def main() -> int:
    app, window, ctx = build_app([])
    window.show()
    window.show_page("playground", save=False)
    pump(app, 0.6)
    manager, view, reg = ctx.playground, window.pages["playground"], ctx.registry
    charmap = reg.add_custom_app("Character Map", os.path.join(SYSTEM32, "charmap.exe"), "executable")
    mstsc = reg.add_custom_app("Remote Desktop", os.path.join(SYSTEM32, "mstsc.exe"), "executable")
    notepad = reg.add_custom_app("Notepad", os.path.join(SYSTEM32, "notepad.exe"), "executable")
    opened: list[int] = []

    def settle(sid: str, timeout: float = 25.0) -> bool:
        return pump(app, until=lambda: (s := manager.get(sid)) is not None and s.state is not SessionState.LAUNCHING,
                    timeout=timeout)

    print("Playground live checks")
    # -- 1. first application in a tab -------------------------------------------------------
    outcome = manager.open_app(charmap.id)
    check("open_app starts a session", outcome.kind == "started", outcome.message)
    check("session settles", settle(outcome.session_id))
    s1 = manager.get(outcome.session_id)
    page1 = view._pages[s1.id]
    check("Character Map is embedded", s1.state is SessionState.EMBEDDED, s1.message)
    if s1.state is not SessionState.EMBEDDED:
        return finish(app, opened)
    opened.append(s1.hwnd)
    pump(app, 1.0)
    check("window re-parented into the tab's host", parent_of(s1.hwnd) == int(page1.host.winId()))
    style = int(w.GetWindowLongPtrW(s1.hwnd, w.GWL_STYLE)) & 0xFFFFFFFF
    check("frame removed, WS_CHILD set", bool(style & w.WS_CHILD) and not style & w.WS_CAPTION)
    check("session file records the hosted pid", any(e["pid"] == s1.pid for e in ctx.settings.playground_store.load()["embedded"]))

    # -- 2. a second tab and switching between tabs ---------------------------------------------
    outcome2 = manager.open_app(mstsc.id)
    check("second session settles", settle(outcome2.session_id))
    s2 = manager.get(outcome2.session_id)
    check("Remote Desktop is embedded", s2.state is SessionState.EMBEDDED, s2.message)
    if s2.state is SessionState.EMBEDDED:
        opened.append(s2.hwnd)
    pump(app, 0.8)
    check("two tabs exist", view.tabs.count() == 2)
    check("new tab is current and its window visible", view.current_session_id() == s2.id and bool(w.IsWindowVisible(s2.hwnd)))
    view.tabs.setCurrentIndex(view._index_of(s1.id))
    pump(app, 0.5)
    check("switching tabs shows the first window", bool(w.IsWindowVisible(s1.hwnd)))
    check("switching tabs hides the second window", not w.IsWindowVisible(s2.hwnd))

    # -- 2b. other pages and overlays hide hosted windows (a native child always paints above Qt widgets) -------
    window.show_page("home", save=False)
    pump(app, 0.5)
    check("leaving the Playground page hides hosted windows", not w.IsWindowVisible(s1.hwnd))
    window.show_page("playground", save=False)
    pump(app, 0.6)
    check("returning shows them again", bool(w.IsWindowVisible(s1.hwnd)))
    view.set_suspended(True)
    pump(app, 0.3)
    check("an open overlay suspends the hosted window", not w.IsWindowVisible(s1.hwnd))
    view.set_suspended(False)
    pump(app, 0.4)
    check("closing the overlay restores it", bool(w.IsWindowVisible(s1.hwnd)))

    # -- 3. duplicate prevention --------------------------------------------------------------------
    again = manager.open_app(charmap.id)
    check("opening the same app again activates its tab", again.kind == "activated" and view.tabs.count() == 2, again.message)

    # -- 4. resize follows the window ---------------------------------------------------------------
    from ctypes import byref, wintypes

    rect = wintypes.RECT()
    w.GetWindowRect(s1.hwnd, byref(rect))
    before = rect.right - rect.left
    window.resize(window.width() + 120, window.height() + 80)
    pump(app, 1.0)
    w.GetWindowRect(s1.hwnd, byref(rect))
    check("hosted window follows the Playground size", (rect.right - rect.left) > before + 60, f"{before} -> {rect.right - rect.left}")

    # -- 5. detach / re-attach ------------------------------------------------------------------------
    view.detach(s1.id)
    pump(app, 0.5)
    check("detach gives the window back to the desktop", parent_of(s1.hwnd) == 0 and bool(w.IsWindowVisible(s1.hwnd)))
    check("detached window kept its frame", bool(int(w.GetWindowLongPtrW(s1.hwnd, w.GWL_STYLE)) & w.WS_CAPTION))
    check("session state is DETACHED", manager.get(s1.id).state is SessionState.DETACHED)
    view.reattach(s1.id)
    pump(app, 0.8)
    check("re-attach hosts it again", manager.get(s1.id).state is SessionState.EMBEDDED and parent_of(s1.hwnd) == int(page1.host.winId()))

    # -- 6. closing a tab does NOT close the application ---------------------------------------------------
    view.close_tab(s2.id)
    pump(app, 0.6)
    check("closing a tab removes it", view.tabs.count() == 1)
    check("the application is still running and top-level", bool(w.IsWindow(s2.hwnd)) and parent_of(s2.hwnd) == 0)

    # -- 7. Notepad: first test requested by the spec; packaged -> docked over the tab, never re-parented ----------
    out3 = manager.open_app(notepad.id)
    check("notepad session settles", settle(out3.session_id))
    s3 = manager.get(out3.session_id)
    check("packaged Notepad is docked over its tab", s3.state is SessionState.EMBEDDED and s3.docked,
          f"{s3.state.value}: {s3.message}")
    if s3.hwnd:
        opened.append(s3.hwnd)
        pump(app, 3.0)  # the real WinUI Notepad crashes ~2 s after SetParent; prove it was never re-parented
        check("Notepad is never re-parented and still alive", bool(w.IsWindow(s3.hwnd)) and parent_of(s3.hwnd) == 0)
    view.close_tab(s3.id)
    if s3.hwnd:
        pump(app, 0.5)
        check("after closing the tab Notepad is still a normal, living window",
              bool(w.IsWindow(s3.hwnd)) and parent_of(s3.hwnd) == 0)

    # -- 8. workspaces with the same real applications -------------------------------------------------------
    ctx.workspaces.set_active("")
    ws = ctx.workspaces.create("Live", [charmap.id, mstsc.id, notepad.id])
    ctx.workspaces.set_mode(ws.id, charmap.id, "embedded")
    ctx.workspaces.set_mode(ws.id, mstsc.id, "embedded")
    ctx.workspaces.set_mode(ws.id, notepad.id, "external")
    from cirana.core.workspaces import SavedLayout

    ctx.workspaces.save_layout(ws.id, SavedLayout(tab_order=[mstsc.id, charmap.id], active=charmap.id))
    # Character Map is still hosted (as a no-workspace tab), Remote Desktop and Notepad run as normal windows.
    windows_before = {i.hwnd for i in __import__("cirana.native.window_discovery", fromlist=["x"]).list_top_level_windows()}
    summaries = []
    ctx.launcher.finished.connect(summaries.append)
    check("workspace opens", ctx.launcher.open(ws.id))
    check("workspace open finishes", pump(app, until=lambda: bool(summaries), timeout=60))
    summary = summaries[0] if summaries else None
    if summary is not None:
        print("   ", summary.message())
        windows_after = {i.hwnd for i in __import__("cirana.native.window_discovery", fromlist=["x"]).list_top_level_windows()}
        new_windows = windows_after - windows_before
        check("no application was started a second time", not new_windows, str(new_windows))
        check("Remote Desktop's running window moved into a tab", "Remote Desktop" in summary.in_tabs, summary.message())
        check("Notepad was brought forward, not duplicated", "Notepad" in summary.already_open, summary.message())
        pump(app, 0.8)
        order = [manager.get(sid).app_id for sid in view.visible_session_ids()]
        check("saved tab order was applied", order == [mstsc.id, charmap.id], str(order))
        check("saved active tab was selected", manager.get(view.current_session_id()).app_id == charmap.id)
        s_mstsc = manager.session_for_app(mstsc.id)
        check("adopted window is hosted and not CIRANA's to close", s_mstsc is not None and s_mstsc.state is SessionState.EMBEDDED
              and not s_mstsc.launched_by_cirana and parent_of(s_mstsc.hwnd) == int(view._pages[s_mstsc.id].host.winId()))
        # switching workspace hides the tabs but keeps both applications alive and hosted
        ctx.workspaces.set_active("")
        pump(app, 0.5)
        check("switching workspace hides its tabs without closing anything", not view.visible_session_ids()
              and view.hosted_count() == 2 and not w.IsWindowVisible(s_mstsc.hwnd))
        ctx.workspaces.set_active(ws.id)
        pump(app, 0.5)
        check("switching back shows them again", len(view.visible_session_ids()) == 2)

    # -- 9. window state restored when CIRANA closes ---------------------------------------------------------------
    hosted = [s for s in manager.sessions() if s.state is SessionState.EMBEDDED]
    check("two windows are hosted before shutdown", len(hosted) == 2, str(len(hosted)))
    window.close()
    pump(app, 0.8)
    check("shutdown returned every hosted window to Windows",
          all(parent_of(s.hwnd) == 0 and w.IsWindow(s.hwnd) for s in hosted))
    check("session file is cleared after a clean shutdown", ctx.settings.playground_store.load()["embedded"] == [])
    opened.extend(s.hwnd for s in hosted)
    return finish(app, opened)


def finish(app: QApplication, opened: list[int]) -> int:
    for hwnd in opened:
        close_politely(app, hwnd)
    ok = all(c[1] for c in CHECKS)
    print("Result:", "ALL PASSED" if ok else "FAILURES", flush=True)
    shutil.rmtree(HOME, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
