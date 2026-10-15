# CIRANA — application launcher and workspace window manager

A Windows desktop tool that discovers your installed applications, launches them, and arranges their **real
windows** into workspaces: groups such as "Development" (VS Code, Brave, PowerShell, Discord) that one click opens,
places side by side or in a grid, and brings to the front. Python 3.11+, PySide6, pywin32, psutil. Fully offline:
no server, database, telemetry or accounts. All data stays in `%LOCALAPPDATA%\CIRANA\`.

CIRANA never embeds, re-parents or restyles another application's window. Every application keeps its own title
bar, taskbar button and Alt+Tab entry, and keeps running untouched when CIRANA closes or crashes.

## Install and run

```powershell
py -3.12 -m venv .venv                       # any Python 3.11+; `py -0p` lists what is installed
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m cirana
```

## Tests

```powershell
python -m unittest discover -s tests -t .
$env:CIRANA_LIVE = "1"; python -m unittest tests.test_wm_live    # optional: real windows on the desktop
```

The regular suite never touches a real window: the window manager runs against a simulated desktop
(`tests/fake_desktop.py`: monitors with different DPI and work areas, minimum sizes, elevated / hung / closed
windows, recycled handles, terminals hosting consoles). It covers the inventory and its threads, identity and match
rules, the application-window filter, layouts, monitor fallback, placement verification, refusals, workspace
activation, concurrent and cancelled launches, ambiguous windows, rediscovery after a restart, the version 1 →
2 data migration, the legacy clean-up, the pages and drag and drop. `tests/test_forbidden_apis.py` fails if
`SetParent`, `SetWindowLong*`, `AttachThreadInput`, `SendInput`, code injection, taskbar-button hiding or process
termination (outside the legacy clean-up) ever appear in the code.

The **live** tests (opt-in) start a real Win32 window in another process (`tests/hang_window_helper.py`) and check on
the real desktop that the tracker sees it, that it is moved and sized exactly with its styles, owner and parent
unchanged, that a window that stops responding is reported in under a second instead of freezing CIRANA, and that a
workspace starts a real program, finds its window through the process tree, arranges and raises it.

**Not covered by automated tests:** real mouse drag and drop (synthetic input is refused in the development
environment; drops are tested by delivering the events to the widgets) and the specific applications named in the
goals (Brave, Discord, Armoury Crate, PowerShell, VS Code), which were not installed / driven during the tests: their
behaviour is covered by the generic mechanisms below (process tree, console hosts, packages, learned programs).

## Architecture

```
cirana/wm/                         the window manager (no Qt widgets)
  identity.py    TrackedWindow (hwnd + pid + process creation time + exe + AUMID + class), MatchRule
  filters.py     which top-level windows are application windows (taskbar / Alt+Tab rules, shell surfaces)
  backend.py     every Win32 call, behind one object (tests substitute tests/fake_desktop.py)
  tracker.py     WindowTracker: WinEvent hook thread + probe thread + WindowInventory (GUI-thread signals)
  monitors.py    monitors, work areas, DPI; finding a saved monitor again
  layout.py      LayoutSpec and pure layout geometry (side by side, main + side, grid, saved positions)
  controller.py  WindowController: place / restore / minimize / maximize / raise, verified, per-window results
cirana/core/
  workspaces.py           workspace model v2, storage, v1 migration (WorkspaceManager)
  workspace_controller.py runtime: entry ↔ window bindings, activation, layouts, saved positions
  workspace_launcher.py   starts missing applications and attributes their new windows
  legacy_recovery.py      one-time clean-up after the old embedding versions
cirana/ui/                pages: Home, Applications, Workspaces, Windows, Favorites, Settings
cirana/widgets/           workspace_card, layout_preview, app cards, drag and drop payloads
```

### WindowTracker and WindowIdentity (phase 1)

* **Events.** A dedicated thread installs `SetWinEventHook` hooks with `WINEVENT_OUTOFCONTEXT |
  WINEVENT_SKIPOWNPROCESS` (delivered to CIRANA's own message loop; nothing is loaded into other processes) for
  create / destroy / show / hide, location and name changes, cloak / uncloak (UWP), minimize start / end, move-size end
  and foreground changes. The callback only notes the handle (child-window events are discarded cheaply).
* **Probing.** A second thread coalesces the notes (a dragged window reports dozens of moves a second), describes the
  windows and every 3 s re-enumerates everything with `EnumWindows`, which catches missed events. Results reach the
  GUI thread through a queued signal; the inventory emits `window_added / window_changed / window_removed /
  foreground_changed`. Nothing on the GUI thread waits for another application.
* **Identity.** A window is (HWND, pid, process creation time). Reading a window opens its process once with limited
  rights (`GetProcessTimes`, `QueryFullProcessImageNameW`, `GetApplicationUserModelId`). A recycled handle or pid gives
  a different key, so it is reported as the old window removed and a new one added, and the controller refuses to
  touch a handle whose owner changed (`still_same`). UWP windows (`ApplicationFrameWindow` of
  ApplicationFrameHost.exe) are identified by their `CoreWindow` child's process and AppUserModelID.
* **Filter.** Visible, not cloaked, not CIRANA, not a child, not a shell surface (desktop, taskbars, Start / Search /
  notification hosts, menus, tooltips), not a tool or no-activate popup, owned windows only with `WS_EX_APPWINDOW` or a
  hidden owner (Delphi-style main windows), untitled popups only with a frame, and a real size.

### WindowController and LayoutEngine (phase 2)

* Moves use `SetWindowPos` with `SWP_ASYNCWINDOWPOS | SWP_NOACTIVATE | SWP_NOZORDER` and show states use
  `ShowWindowAsync`: requests are posted to the window's own thread, so a hung application cannot block CIRANA.
  Before touching a window the controller checks that the handle still belongs to the same process, that Windows
  will allow it (integrity level: an elevated application such as Armoury Crate is reported, not attacked) and that it
  is responding.
* **Verification.** After a short settle time each window is read back. Results per window: placed, *constrained*
  (the application kept a minimum or fixed size: reported, never fought), denied (with the reason), hung, gone, or
  failed. One window's problem never stops the others.
* Targets describe the **visible frame** (`DWMWA_EXTENDED_FRAME_BOUNDS`); the invisible resize borders are added
  back, so gaps between windows are even. Minimized and maximized windows are restored first (`GetWindowPlacement`
  tells whether a minimized one would come back maximized). Moving to a monitor with another scale is followed by the
  same request again, because per-monitor aware applications resize themselves after the move (`WM_DPICHANGED`).
* **Layouts** (pure functions, also used by the preview): side by side (optional weights), main + side panel (main
  width 20–80 %), grid 2×2 and dynamic grid (as square as possible, last row stretched), and saved positions.
  Gaps are logical pixels scaled by the monitor's DPI. Coordinates are physical pixels from `EnumDisplayMonitors` /
  `GetMonitorInfoW` work areas (CIRANA runs per-monitor DPI aware, logged at start).
* **Monitors.** A layout remembers its monitor as {device, rectangle, primary}; when it is gone, the same output, then
  the same geometry, then the primary monitor is used. Saved positions are fractions of a work area, so they survive
  resolution changes; rectangles are always clamped into a work area.
* **Never automatic.** Layouts are applied only when the user activates a workspace or presses *Apply layout*. A
  window the user moves afterwards stays where it was put; CIRANA never snaps it back.

### WorkspaceController and WorkspaceLauncher (phase 3)

* An entry is an application from the catalog or a window picked from the open windows (several windows of one
  program are separate entries). It stores a match rule (executable, AppUserModelID, optional class / title, programs
  learned while launching), whether to start it when missing, and the title of the window last bound to it.
  **No HWND or PID is ever stored.**
* **Binding.** At runtime each entry is bound to a window key. One fitting window is bound by itself; with several
  (two Brave windows) the one whose title matches the remembered title is taken, otherwise the user chooses (a
  dialog during activation, or *Choose window* on the row). After a CIRANA restart the bindings are rebuilt the
  same way from the windows that are still open.
* **Activate** = make it the active workspace, reuse every open window (nothing is restarted), start the missing
  applications, arrange the group in its layout, raise it (first entry on top, with the focus). Other workspaces'
  windows are not hidden, minimized or closed. *Close workspace* only stops managing: every application keeps running.
* **Launching.** Several applications start at once. A new window is attributed to the launch whose process tree
  shows it (launchers and stubs such as Discord's `Update.exe` start the real program as a child; shortcuts are
  started with `ShellExecuteEx` so their process is known), then to the launch whose tree it hosts (a terminal that
  owns the console of the shell it shows: PowerShell inside Windows Terminal), then by match rule (packaged apps,
  single-instance programs). With several candidates the earliest launch wins; each window goes to one launch only.
  A program that hands the request to a running copy and exits is finished as "handed over" and the existing window
  is used (or offered). When nothing is recognised at all (a program already running in the tray that answers
  through another process), *Choose window* on the row lists every open window. What was learned (the terminal,
  `Discord.exe`, the window chosen by hand) is stored in the rule, so the window is recognised after restarts. Activating again while applications are still starting starts nothing twice;
  closing the workspace cancels the session and its late launch results and windows are dropped (generation token).
  No window after 30 s (60 s while the started process runs): reported, the process is left alone.

### Interface (phase 4)

* **Home:** workspace cards with the applications' icons, a dot per open window, "n of m open" and *Activate*.
* **Workspaces:** card list (drop application cards on a card to add them), and for the selected workspace:
  *Activate*, *Apply layout*, *Bring to front*, a ⋯ menu (save current positions, minimize its windows, close the
  workspace, rename, delete), the layout picker (layout, monitor, gap, main width) with a live preview drawn from the
  same geometry, and one row per window with its state (Open, Starting…, Choose a window, Starts on activate, Not open,
  Not available), the bound window's title, the last problem (e.g. "runs with higher rights"), *Start* checkbox,
  *Choose window*, *Bring to front* and remove. Rows reorder by drag and drop (their place in tiled layouts),
  application cards dropped on a row are inserted there, a row dragged onto the area below is removed. *Add
  applications…* (catalog) and *Add open windows…* (a picker that follows windows as they open and close).
* **Windows:** every open application window, live, with bring to front, minimize, maximize / restore, and "add to
  a workspace"; it shows which workspaces a window belongs to and, with several monitors, which monitor it is on.
* The top bar's workspace pill activates a workspace, raises or closes the active one.

### Data (phase 5)

Files: `config.json`, `applications.json` (discovered), `categories.json`, `favorites.json` (favorites + recents),
`overrides.json` (hidden, custom apps; the old `launch_modes` key is kept but no longer used), `workspaces.json`
(version 2: workspaces, entries, rules, layouts), `cache/icons/`, `logs/cirana.log`. Versioned JSON, atomic writes,
`.bak` backups, corrupt files quarantined, damaged records dropped one by one.

* **Version 1 → 2.** The Playground-era file is migrated on first load: the per-application "tab / own window" mode
  is dropped, the saved tab order becomes the entry order, saved window rectangles become saved positions relative
  to the monitor they were on (rectangles on no monitor are dropped; if monitors cannot be read the rectangles are
  kept for the next start), each workspace is migrated on its own, and the original is kept as
  `workspaces.v1.json`.
* **Old crash-recovery file.** `playground_session.json` from the embedding versions is read once at start: windows
  whose taskbar button the old version had hidden get it back (only if the handle still belongs to the same process);
  applications that lost their window with the old CIRANA are listed and can be ended only after the user ticks them
  and confirms twice. The file is then removed (kept while the CIRANA that wrote it still runs).

## Removed

The window-embedding architecture is gone: `core/playground.py` (sessions, window search, reconnecting),
`ui/playground_view.py` (tabs, full-window mode, focus hand-over), `widgets/native_host.py` and
`widgets/session_page.py` (native child hosts), `native/window_embedder.py` (`SetParent`, style changes, docking),
`native/window_restorer.py` (style / parent restore), `native/taskbar.py` (hiding taskbar buttons),
`native/window_discovery.py` (replaced by `cirana/wm`), `native/poc_embed.py`, `core/orphans.py` (crash recovery
for embedded windows; the one-time part lives in `core/legacy_recovery.py`), the Playground page and its launch
modes, and the `SetParent` / `SetWindowLongPtr` bindings. With nothing embedded there is nothing to hand back on exit
and nothing to recover after a crash.

## Known limitations

* Windows (UIPI) does not let a normal-rights program move an elevated application's window; CIRANA reports it.
  Run CIRANA elevated only if you need to arrange elevated windows.
* Matching is by program, package and learned programs; two different programs with the same file name are
  treated alike, and windows of a generic host (`javaw.exe`, `python.exe`) need a choice when several are open.
* Applications that restore their own window position after starting (some browsers, Electron apps) may move after
  CIRANA placed them; *Apply layout* arranges them again.
* Windows on other virtual desktops are cloaked and therefore not listed.
* Discovery names come from shortcut names; categorization is heuristic.

## Build an EXE

```powershell
pip install pyinstaller
pyinstaller cirana.spec --noconfirm
# output: dist\CIRANA\CIRANA.exe  (one-folder build)
```

## Application catalog (unchanged)

| Area | Behavior |
|---|---|
| Discovery | Start Menu shortcuts (user + system), packaged apps via `Get-StartApps`, `App Paths` registrations. No disk-wide scan. Runs on a worker thread; cached in `applications.json`; manual refresh. |
| Launch types | Executable, Windows shortcut (`ShellExecuteEx`, so the started process is known), AppUserModelID, custom command (separate code paths, never a shell). |
| Dedup | By normalized name; shortcut beats AUMID, user scope beats system scope. Ids are derived from the name so favorites survive a change of launch method. |
| Categories | Rule-based behind a `Classifier` protocol. Rename/add/delete categories, per-app category, hide, favorite. |
| User data | Overrides live in separate files from discovered data, so a rescan never loses manual changes. |
| UI | Collapsible sidebar, top bar (search, workspace pill, quick launch, grid/list toggle, settings). Ctrl+K search, Ctrl+B sidebar, Ctrl+1…6 pages. |
| Icons | Shell icons (up to 256 px, also for packaged apps), cached on disk, loaded lazily; letter tile fallback. |
| Running apps | psutil poll every 5 s on a worker thread, paused while minimized. |
