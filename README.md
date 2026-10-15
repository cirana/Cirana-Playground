# CIRANA — Desktop Playground

A Windows desktop environment that discovers your installed applications, organizes them, and launches them
instantly. Python 3.11+, PySide6, pywin32, psutil. Fully offline: no server, database, telemetry or accounts.
All data stays in `%LOCALAPPDATA%\CIRANA\`.

**Status: Phases 1-3 complete - discovery and launching, the native Playground (applications in tabs), and
Workspaces (one-click groups with saved sessions). Phase 4 (polish) is not started.**

## Install and run

```powershell
py -3.12 -m venv .venv                       # any Python 3.11+; `py -0p` lists what is installed
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m cirana
```

Startup command: `python -m cirana` from the project folder, using the interpreter that has the requirements
installed (with several Python versions installed, call the venv's `python.exe` explicitly as above).

## Tests

```powershell
python -m unittest discover -s tests -t .
```

Offscreen UI tests (a fake Qt platform with every Win32 call mocked) cover the pages, the Playground tab logic,
workspaces, drag and drop and the crash-recovery logic. The **live** tests (`tests/test_playground_live.py`,
`tests/test_native_embedding.py`) drive real applications - Character Map, Remote Desktop, Notepad, and a
purpose-built Win32 window that stops responding on demand (`tests/hang_window_helper.py`) - and need an
interactive desktop session; they open real windows for a couple of minutes and close them again (politely, with
`WM_CLOSE`). One of them kills a hosting CIRANA process on purpose to prove the crash-recovery path.

**Not covered by automated tests:** real mouse drag and drop. The environment the tests were developed in refuses
synthetic input (`SetCursorPos`/`SendInput` fail with access denied) and the offscreen Qt platform cannot run a drag,
so drops are tested by delivering the drag/drop events directly to the widgets, and the drag-threshold logic by
synthetic mouse events. Qt's real `QDrag` / OLE drag loop was not driven by a test - try it by hand.

## What works (Phase 1)

| Area | Behavior |
|---|---|
| Discovery | Start Menu shortcuts (user + system), packaged apps via `Get-StartApps`, `App Paths` registrations. No disk-wide scan. Runs on a worker thread; cached in `applications.json`; manual refresh. |
| Launch types | Executable, Windows shortcut, AppUserModelID, custom command (separate code paths, never a shell). |
| Dedup | By normalized name; shortcut beats AUMID, user scope beats system scope. Ids are derived from the name so favorites survive a change of launch method. |
| Categories | Rule-based (known executables, then keywords incl. the English package name of packaged apps and common Turkish Windows names) behind a `Classifier` protocol, so a local model can be plugged in later. On the development machine (Turkish Windows) 20 of 227 apps (9 %) end up in "Other", down from 53 % with English-only rules. Rename/add/delete categories, per-app category, hide, favorite. |
| User data | Overrides live in separate files from discovered data, so a rescan never loses manual changes (including for apps that temporarily vanish). |
| UI | Collapsible sidebar, top bar (search, workspace pill, quick launch, grid/list toggle, settings), dashboard (running, favorites, recents, categories), Applications, Favorites, Settings. Ctrl+K search, Ctrl+B sidebar, Ctrl+1…6 pages. |
| Icons | Shell icons (up to 256 px, also for packaged apps), cached on disk, loaded lazily in time-boxed batches; letter tile fallback. |
| Running apps | psutil poll every 5 s on a worker thread, paused while minimized. Only apps with a known exe path can be detected. CIRANA's own process is excluded, and apps that share an executable or are launched with arguments (Chrome PWAs, pydoc, ...) are never reported as running because they cannot be told apart. Packaged apps cannot be detected. |
| Storage | Versioned JSON, atomic temp-file + `os.replace` writes, `.bak` backups, corrupt files quarantined as `*.corrupt-<ts>`, defaults regenerated. |

Files: `config.json`, `applications.json` (discovered), `categories.json`, `favorites.json`
(favorites + recents), `overrides.json` (hidden, custom apps, launch modes), `workspaces.json` (workspaces, their applications, modes and saved sessions), `playground_session.json` (windows currently hosted, for crash recovery),
`cache/icons/`, `logs/cirana.log`.

## Playground (Phase 2)

Sidebar > Playground. Open an application with **Open application**, the **+** button, an application's context
menu (*Open in a Playground tab*), or by setting an application's *Default launch mode* to Playground tab.
**Running window...** moves a window that is already open into a tab.

| Behavior | Detail |
|---|---|
| Layout | With tabs open the page is one 42 px strip (tabs, status line, search, workspace switch, Detach, Close application, full-window toggle) above an edge-to-edge host; the top bar is hidden on this page. Messages appear in the strip, whose height is fixed, so they never resize the hosted window. Embedding warnings sit behind a warning icon instead of a permanent banner. **Full-window mode** (strip button, `F11`, or double-click a tab) also hides the sidebar; it ends by itself when the last tab closes. |
| Tabs | Draggable (reorder), closable, middle-click closes, per-tab icon, right-click menu, `Ctrl+W`, `Ctrl+Tab` / `Ctrl+Shift+Tab`. Switching tabs shows/hides the hosted windows; they keep running. Keyboard shortcuts only work while CIRANA itself (not the hosted app) has focus. |
| Sizing | The hosted window fills the tab and follows window resizes and DPI/monitor changes (device pixels). Resizes are posted asynchronously (`SWP_ASYNCWINDOWPOS`), so a busy application cannot block CIRANA. |
| Closing a tab | **Detaches** the window (original frame, style, position, maximized state restored) and leaves the application running. It never closes the application. |
| Detach / Re-attach | Toolbar button. A detached tab stays and can re-attach the same window. |
| Close application... | Only for applications CIRANA started itself; asks for confirmation, then sends a polite `WM_CLOSE` (the app may prompt to save). Nothing is ever force-killed. |
| CIRANA exits | Every hosted window is detached first (`closeEvent`, `aboutToQuit`, Ctrl+C in a console run). |
| Docked apps | Store / UWP / WinUI packages (Notepad, Calculator, Paint, Teams, Outlook, Settings...), console windows, Windows Terminal and Explorer crash or break when re-parented, so they are **docked** instead: the window stays an ordinary top-level window and CIRANA keeps its visible frame exactly on the tab (follows moves, resizes and maximize; snaps back if dragged), raises it over CIRANA while the tab shows, puts it behind CIRANA for other tabs/pages/the search overlay, and minimizes it with CIRANA. Only position, size and show state are changed, always with posted / async calls (`SWP_ASYNCWINDOWPOS`, `ShowWindowAsync`); owner, parent and styles are never touched, and a handle is only moved while it still belongs to the same process. A docked app keeps its own title bar, taskbar button and Alt+Tab entry, survives a CIRANA crash with its window intact (so it is not recorded for crash recovery), and is not re-ordered while one of CIRANA's own menus or dialogs is open. Packaged apps launched by AUMID are found by their package folder (`Name_..._PublisherId`, including the CoreWindow inside a UWP frame), then by window title; single-instance apps (Settings) fall back to their existing window after 4 s. |
| Unsupported apps | Opened as a normal window, untouched: scripts, unknown executables and shell items without a package (Control Panel, MMC consoles) are refused **before** launch; elevated windows are refused when they appear. Start-menu entries whose AUMID is just a program path (Character Map, msinfo32...) are treated as the classic programs they are. The tab explains why. |
| Duplicates | Opening an application that already has a tab switches to it. *Open another instance* in the tab menu starts a second one on purpose. |
| Lifecycle | The hosted window is watched twice a second with calls that send no messages (`IsWindow`, `GetParent`, `IsHungAppWindow`). Closed windows and windows that re-parent themselves away are shown on the tab. **A non-responding application is hidden behind a status panel** (measured: *showing* a host whose child is hung blocks the UI thread until the app recovers, hiding does not), comes back by itself when it recovers, and can be detached at any time; a detach that Windows cannot complete yet finishes on a helper thread, and the tab's host is kept alive until it has. |
| Focus | Clicking a tab, moving the pointer into the hosted window (or clicking it), or re-activating CIRANA gives keyboard focus back to the hosted window. Focus moves when the pointer *enters* the app, before the click: handing it over after the click made browsers restore their previous focus (the page) over the field just clicked (the address bar). A click inside the hosted window never reaches Qt, so a 50 ms check (only message-free calls: `GetForegroundWindow`, `GetAsyncKeyState`, `GetCursorPos`, `GetFocus`) notices it. Browsers (Chromium, Firefox) also need to believe their window is *active* before they accept typing, and a child window never receives `WM_ACTIVATE` again, so CIRANA posts `WM_NCACTIVATE`/`WM_ACTIVATE` to the hosted window when it gains or loses the foreground (the app's own menus and popups count as staying inside it). Qt shortcuts do not fire while the hosted application has focus. |
| Overlays | A native child window always paints above Qt widgets, so hosted windows are hidden while the search overlay is open; messages are shown in the strip's status line instead of a floating toast. |
| Crash recovery | While windows are hosted, `playground_session.json` records them (pid, creation time, host pid). If CIRANA is killed, the apps survive without a window (measured). On the next start CIRANA lists them (only if still running, same creation time, no window, host gone) and the user may end selected ones after two explicit confirmations. Nothing is ended automatically. |

### Notepad (the requested first test)

Windows 11 Notepad is a packaged WinUI 3 app (`System32\notepad.exe` is only a stub that starts the Store package).
Re-parenting it crashes it about two seconds later, so CIRANA never re-parents it (verified live: the window stays
alive, untouched, top-level); it is **docked** over its tab instead (see Docked apps; docking is not live-verified yet). The real embedding proofs are Character Map and
Remote Desktop (live-tested in tabs, switching, detach/re-attach, tab closing, resize, shutdown restore) and 7-Zip
(PoC). A classic Win32 editor would work the same way.

## Workspaces (Phase 3)

Sidebar > Workspaces (or the workspace pill in the top bar).

| Feature | Detail |
|---|---|
| Manage | Create, rename (unique names), delete (the applications and anything running are untouched). |
| Fill | Drag an application card onto a workspace - onto the page, onto a list entry, or onto the floating *Drop on a workspace* strip that appears at the bottom of any page while dragging. Also *Add applications...* and the context menu *Add to workspace*. Duplicates are ignored. |
| Per-application mode | *Playground tab* or *Own window*, stored per workspace entry. New entries start from the application's *Default launch mode* (default: own window - hosting is opt-in). Applications that cannot be hosted always open in their own window. |
| One-click open | *Open workspace* (also on the Home dashboard): makes it active, opens its applications one after another, reports a summary. Failures and missing applications never stop the rest. |
| No duplicates | An application that already has a tab is just activated; a running window of a tab-mode application is adopted into a tab instead of starting another copy; a running normal window is brought to the front. |
| Switching | The active workspace owns its tabs. Switching hides the other workspace's tabs (applications keep running inside their hidden hosts) and shows the new one's. |
| Sessions | *Save session* stores, per workspace: the open tabs in order, the active tab, window positions of the workspace's running normal windows, and adds open applications that were not listed yet. Opening the workspace re-applies tab order and active tab; positions are applied to windows CIRANA starts (never moved on a window that was already open). |
| Storage | Everything is in `workspaces.json` (versioned, atomic writes, `.bak`, corrupt-file quarantine); damaged entries are dropped individually. |

## Native embedding proof of concept

```powershell
python -m cirana.native.poc_embed                       # interactive
python -m cirana.native.poc_embed --selftest charmap    # scripted checks
python -m cirana.native.poc_embed --selftest 7zip --screenshot out.png
```

Mechanism: the foreign top-level window is turned into a `WS_CHILD` (frame styles removed) and re-parented with
`SetParent` onto the HWND of a native Qt widget (`cirana/widgets/native_host.py`). Original parent, style,
extended style, placement and maximized state are captured and restored on detach. Closing the host detaches
first. CIRANA never terminates a process; "close application" is a polite `WM_CLOSE`, only for processes CIRANA
started. All Win32 calls have explicit 64-bit-safe signatures (`cirana/native/win32_api.py`).

### Tested applications (this machine: Windows 11 26100, Python 3.12, PySide6 6.12, 125 % display scaling)

| Application | Result |
|---|---|
| Character Map (`charmap.exe`, classic Win32 dialog) | **Works.** Embeds, fills host, follows host resizing (fixed dialog does not re-layout), detach restores original style exactly, app keeps running. Screen capture verified. Repeated runs pass. |
| 7-Zip File Manager (`7zFM.exe`) | **Works.** Same checks pass; the UI re-lays out when the host resizes. CIRANA warns about a DPI-awareness mismatch. Screen capture verified. |
| Remote Desktop Connection (`mstsc.exe`) | **Works** in the PoC and in live Playground tabs (embed, switch, resize, detach, adopt a running window, restore). Not used interactively beyond that. |
| Windows 11 Notepad 11.2607 (Store/WinUI 3) | **Does not work: crashes.** Embedding "succeeds", then Notepad terminates ~2 s later (`Application Error`, `CoreMessagingXP.dll`, `0xc000027b`). CIRANA now refuses all packaged apps (`\WindowsApps\` image path) before touching them. Notepad is hosted as an external window. |
| Calculator (UWP) | **Refused** by the `ApplicationFrameWindow` class check (the refusal was exercised; embedding was not attempted). |
| Anything else (VS Code, Chrome, Discord, games, Explorer, elevated apps…) | **Untested.** Chromium/Electron, SDL and Unity/Unreal windows are flagged with warnings; elevated apps, console windows, Explorer and the taskbar are refused. |

What the automated checks do **not** prove: typing/mouse input into an embedded app, popups/dialogs/menus of
embedded apps, multi-monitor DPI changes, or behavior over long sessions. Treat embedding as experimental.

### Embedding hazards (read before building on this)

- **If CIRANA dies hard, the embedded app is orphaned without a window.** Measured: embed Character Map, then
  `kill` the host process. The `charmap.exe` process kept running but its window was destroyed with the host
  window (no visible window to recover). A normal close detaches first and is safe; a crash or Task Manager
  kill is not. Unsaved work in an embedded app can be lost this way. The Playground now records hosted pids and
  reports/cleans up such orphans on the next start (see Playground > Crash recovery). A watchdog that detaches
  windows when CIRANA disappears cannot help: the window is already destroyed with the host.
- **Shared input queues (documented Windows behavior, not tested here).** Re-parenting a window across processes
  attaches both threads' input queues, so a hung embedded application can stall CIRANA's UI and vice versa.
  The Playground therefore checks `IsHungAppWindow`, never calls message-sending APIs on the UI thread for
  hosted windows (posted resizes, guarded focus) and restores hung windows on a helper thread with a timeout.
- Applications can re-parent themselves away; the host notices (`escaped`) and stops managing the window.
- A window must be stable for 0.5 s before embedding; elevated, packaged, console, Explorer and taskbar
  windows are refused up front.

Notes from the work: Windows 11's Notepad launcher process exits immediately and the real window belongs to a
different pid, so windows are found by diffing top-level windows (and matching the executable name), not by the
launched pid. A found window must also stay stable for 0.5 s before embedding, because an early attempt hit
`ERROR_INVALID_WINDOW_HANDLE` on an app that rebuilt its window while starting.

## Known limitations

- **Hosting foreign windows is inherently fragile.** Only Character Map, Remote Desktop and 7-Zip were verified
  end to end. Chromium/Electron apps (Chrome, VS Code, Discord...), SDL and Unity/Unreal windows are allowed with a
  warning but are untested; use *Detach* if one misbehaves. Menus, popups, dialogs, drag and drop and text input
  inside hosted applications are not covered by automated tests.
- If CIRANA is **killed hard** (Task Manager, crash), hosted applications lose their window (they keep running).
  CIRANA reports them at the next start; unsaved work in them cannot be recovered.
- A hosted application shares an input queue with CIRANA (Windows attaches both threads). A hang is only noticed
  after Windows declares the window not responding (about 5 s); until then, showing/hiding that window can block
  CIRANA for those seconds. After that CIRANA hides the window and stays responsive (live-tested).
- Elevated (administrator) applications cannot be hosted by a normal CIRANA (Windows UIPI). Packaged apps cannot be
  hosted at all. Console programs run in their own console window.
- Windows decides which window an application shows; apps that reuse an existing window (single-instance apps,
  launcher stubs) are only hosted when a matching window appears within 15 s - otherwise they stay normal windows.
  Windows are matched by executable path or file name, so two different programs with the same file name could be confused.
- Window positions are saved only for ordinary windows CIRANA can find by executable name (first match). Hosted
  tabs have no position; their order and active tab are saved.
- Only one workspace opens at a time. Hosted tabs of an inactive workspace are hidden, not suspended.
- AUMID launches go through `explorer.exe shell:AppsFolder\...`, so there is no process handle and packaged apps
  are not shown as "running".
- Discovery names come from shortcut names; localized Windows tools keep their localized names. Categorization is
  heuristic and tuned on one machine - recategorize any app from its context menu.
- Custom commands, `.bat`/`.cmd` and console programs start in their own console window (`CREATE_NEW_CONSOLE`).
- Frameless chrome uses the native title bar tinted via DWM (Windows 11); on older Windows it is the default frame.
- Single-instance enforcement is not implemented (a second CIRANA works, and does not mistake the first one's hosted
  windows for orphans).

## Build an EXE

```powershell
pip install pyinstaller
pyinstaller cirana.spec --noconfirm
# output: dist\CIRANA\CIRANA.exe  (one-folder build, ~125 MB)
```

The build was verified: the packaged EXE starts, scans (COM + PowerShell) and caches 227 applications.

## Layout

```
cirana/
  main.py  paths.py
  core/      models, application_scanner, application_launcher, application_registry, category_engine,
             process_tracker, scan_controller, search, background, playground, orphans,
             workspaces, workspace_launcher
  native/    win32_api, window_discovery, window_embedder, window_restorer, shell_icons, poc_embed
  storage/   json_store, settings_manager
  ui/        main_window, sidebar, topbar, dashboard, applications_view, settings_view, search_overlay,
             playground_view, workspace_view, dialogs, ...
  widgets/   app_card, app_collection, flow_layout, native_host, session_page, workspace_drop_bar, dnd
  assets/    themes/dark.qss, icons/
tests/
```
