# Testing

```powershell
python -m unittest discover -s tests -t .
$env:CIRANA_LIVE = "1"; python -m unittest tests.test_wm_live tests.test_stacks_live tests.test_hotkeys_live
```

The regular suite never touches a real window: the window manager runs against a simulated desktop
(`tests/fake_desktop.py`: monitors with different DPI and work areas, minimum sizes, elevated / hung / closed
windows, recycled handles, terminals hosting consoles). It covers the inventory and its threads, identity and match
rules, the application-window filter, layouts, monitor fallback, placement verification, refusals, workspace
activation, concurrent and cancelled launches, ambiguous windows, rediscovery after a restart, the version 1 →
2 data migration, the legacy clean-up, the pages and drag and drop. `tests/test_forbidden_apis.py` fails if
`SetParent`, `SetWindowLong*`, `AttachThreadInput`, `SendInput`, code injection, taskbar-button hiding, `SW_HIDE`,
synchronous `ShowWindow`, foreground-lock tricks (`AllowSetForegroundWindow`, `LockSetForegroundWindow`,
`BlockInput`), profile-writing `SystemParametersInfo` flags or process termination (outside the legacy clean-up) ever
appear in the code, and that `RegisterHotKey` / `SystemParametersInfoW` are used only by their own modules.

The **live** tests (opt-in) start a real Win32 window in another process (`tests/hang_window_helper.py`) and check on
the real desktop that the tracker sees it, that it is moved and sized exactly with its styles, owner and parent
unchanged, that a window that stops responding is reported in under a second instead of freezing CIRANA, and that a
workspace starts a real program, finds its window through the process tree, arranges and raises it.

**Not covered by automated tests:** real mouse drag and drop (synthetic input is refused in the development
environment; drops are tested by delivering the events to the widgets) and the specific applications named in the
goals (Brave, Discord, Armoury Crate, PowerShell, VS Code), which were not installed / driven during the tests: their
behaviour is covered by the generic mechanisms below (process tree, console hosts, packages, learned programs).
