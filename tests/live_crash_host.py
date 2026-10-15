"""Helper for test_playground_live: embeds Character Map, reports its pid, then idles until it is killed.

    python -m tests.live_crash_host <data dir>
"""
import os
import sys
import time

os.environ["CIRANA_HOME"] = sys.argv[1]
os.environ.pop("QT_QPA_PLATFORM", None)

from cirana.core.playground import SessionState  # noqa: E402
from cirana.main import build_app  # noqa: E402

app, window, ctx = build_app([])
window.show()
window.show_page("playground", save=False)
entry = ctx.registry.add_custom_app(
    "Character Map", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "charmap.exe"), "executable")
outcome = ctx.playground.open_app(entry.id)
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    app.processEvents()
    session = ctx.playground.get(outcome.session_id)
    if session.state is not SessionState.LAUNCHING:
        break
    time.sleep(0.01)
print(f"STATE {session.state.value} PID {session.pid}", flush=True)
while True:  # wait to be killed
    app.processEvents()
    time.sleep(0.05)
