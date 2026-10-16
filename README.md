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

## Architecture

```
cirana/wm/                         the window manager (no Qt widgets)
  identity.py    TrackedWindow (hwnd + pid + process creation time + exe + AUMID + class), MatchRule
  filters.py     which top-level windows are application windows (taskbar / Alt+Tab rules, shell surfaces)
  backend.py     every Win32 call, behind one object (tests substitute tests/fake_desktop.py)
  tracker.py     WindowTracker: WinEvent hook thread + probe thread + WindowInventory (GUI-thread signals)
  monitors.py    monitors, work areas, DPI; finding a saved monitor again
  layout.py      LayoutSpec and pure layout geometry (side by side, main + side, grid, BSP, automatic, saved positions)
  bsp.py         the BSP tree: insert next to the focused tile, remove, swap, resize, store (pure)
  navigation.py  which tile / monitor lies left, right, above, below (pure geometry)
  rules.py       window rules: float / ignore / belongs to a workspace (pure matching)
  controller.py  WindowController: place / restore / minimize / maximize / raise / focus, verified, per window
cirana/core/
  workspaces.py           workspace model v2, storage, v1 migration (WorkspaceManager)
  workspace_controller.py runtime: entry ↔ window bindings, activation, layouts, saved positions
  workspace_launcher.py   starts missing applications and attributes their new windows
  dynamic_tiling.py       DynamicTiler: re-tiles as windows come and go; tiles, stacks, monocle, keyboard commands
  window_commands.py      the commands by name (what a shortcut or a button does)
  hotkeys.py              global shortcuts (RegisterHotKey on CIRANA's own thread), conflict checks
  focus_follows_mouse.py  optional focus follows the mouse (Windows' own active window tracking, per session)
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
  applications, arrange the group in its layout, raise it (the window that had the focus last in it gets it again, else the first entry). Other workspaces'
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

### Dynamic tiling (`core/dynamic_tiling.py`)

**All windows (Windows page → *Tile all windows*).** Every application window on the desktop is tiled, workspace or
not, Komorebi-style: one layout per monitor (side by side, main + side panel, grid 2×2, dynamic grid; gap and main
width), windows in the order they appeared, each on the monitor it opened on. Windows without a sizing border (fixed
dialogs, borderless full-screen games and videos), minimized and maximized windows, windows Windows will not let
CIRANA move, and programs you mark *floating* (the unlink button on a window's row; stored as `tiling_exclusions`)
are left alone. Dragging a tile onto another swaps them; dropping it on another monitor moves it to that monitor's
layout. The switch, the layout and the floating programs are kept in `config.json`. While it is on it is in charge of
every window: a workspace's own tiling steps aside, and activating a workspace still opens and raises its
applications. Everything below (triggers, no loop, drags, refusals, cancellation) applies to both scopes.

**Focus borders and drop preview** (`ui/tiling_overlay.py`). Every window dynamic tiling manages (either scope) gets a
thin border: accent green `#C3E85B` for the focused window, grey `#2A2D34` for the others (2 px, scaled with the
monitor's DPI; *Borders* on the Windows page switches them off). Colours follow `WindowTracker.foreground_changed`;
borders follow moves and resizes, sit directly above their window in the z-order, and disappear for minimized,
maximized, full-screen, closed or no longer managed windows, on a workspace switch and when tiling is turned off.
While a tiled window is dragged, a translucent grey rectangle shows exactly where it will land (`DynamicTiler.
predict_drop`, the same rules the drop uses: swap with the tile under its centre, join another monitor's layout, or
back to its own tile); it hides when there is no target and when the drag ends or is cancelled. Both are CIRANA's
own frameless tool windows, transparent for mouse and keyboard, never activated, without taskbar button or Alt+Tab
entry, and invisible to the window tracker; only they are positioned (in physical pixels), never the applications.
At fractional scaling Qt may round an overlay's size by one pixel.

**Minimum sizes.** Many applications refuse to become smaller than a minimum (they answer `WM_GETMINMAXINFO`).
Windows then keeps the left / top edge CIRANA asked for and lets the window grow to the right and down, so part of
it ends up under its neighbour or off the screen and its content looks cut off on the right or bottom. CIRANA never
asks another application for its minimum (no message is sent to it); it learns it from what the controller reads back
(`Status.CONSTRAINED`: same position, larger frame), remembers it per window and DPI, and lays the monitor out again
with every tile at least that large (`wm/layout.py`: `tile(..., minimums)`, the others share the rest by weight).
When the minimums cannot fit in the layout you chose, **another layout is tried before any window floats**
(`layout.fit_layout`: main + side panel, a grid, side by side, a single column or row stack, a fresh BSP tree; the
one that floats the fewest windows wins, the first on a tie, so it depends on the sizes only and never flips back
and forth). The chosen layout is never changed or overwritten (not its kind, not its stored BSP tree); the
substitute is shown on the Windows page ("layout adjusted to minimum sizes", and on every row) and is dropped as soon
as the windows fit the chosen layout again. Only when no layout can hold a window, the newest window with a minimum
*floats*: it is moved (never shrunk) so that it is completely on the screen, or with its top-left corner on the screen
when it is larger than the monitor, and the reason ("does not fit beside the other tiled windows in any layout")
is shown on its row and reported once. The drop preview uses the same planning. What an
application draws inside a window of the size it accepted is up to the application.

**Adaptive layout engine (the default).** With nothing stored, all-window tiling uses *Automatic (adaptive)*
(`wm/adaptive.py`); a layout you chose before is kept as it is (records are never rewritten, "By window count" stays
what it was). Per monitor (or workspace) it lays out BSP, main + side panel, grid, a single column and side by side
for real, and scores them: a window that cannot get its learned minimum size costs 100 (so a layout that holds
everybody always wins), then awkward tile proportions (which includes the monitor's shape and orientation: three
windows get side by side on an ultrawide and a column on a portrait monitor), slivers, tiny tiles, unbalanced areas
and how far the windows already tiled would have to move. The layout in use is kept unless another one is better by
more than a threshold (hysteresis) and holds as many windows, and it is held outright while the same windows are
there after you resized, swapped or stacked (the proportions you set live in the layout's own fields and in the
tree, so they come back whenever the engine returns to that layout). Choosing any layout by hand stops the engine
for that scope - dynamic tiling goes on inside your layout - and choosing *Automatic (adaptive)* again starts it.
The Windows page shows what it uses. Windows that fit no layout float, with the reason, as before.

**Smart BSP.** A new window still splits the focused tile along its longer side when that is a good tiling. When it
would break a minimum size, make a sliver or an awkward tile, every tile and both directions (and a ratio from the
two minimums) are compared (`bsp.insert`) and the cheapest wins; nothing else in the tree changes, so ratios you set
survive and a closing window only gives its space to its sibling.

**One workspace.** A per-workspace switch (*Dynamic tiling* in the layout panel, stored with the layout as `"dynamic"`; records written
before it existed read as off). While it is on **and** the workspace is the active one, its tiled layout (side by
side, main + side panel, grids) follows its windows by itself:

| Event | What happens |
|---|---|
| A window of the workspace opens (launched, recognised by its rule, chosen by you, or re-created by its app) | It gets a tile; the others make room. |
| One closes | The others fill the gap. |
| One is minimized or maximized | It leaves the layout until it is restored; CIRANA never changes its state. |
| You drag a tiled window | Nothing is re-tiled until you let go (a running re-tile is cancelled). Dropped onto another tile: the two swap places (the order is saved). Otherwise it goes back to its tile. |
| You resize a tiled window with the mouse | Told from a move by its edges: a resize keeps the opposite edge, a move shifts both. The new size becomes the layout's proportions (see *Resize with the mouse*) and the neighbours follow. |
| Layout settings, entry order or monitors change | Re-tiled once. |
| Another workspace becomes active | Only the active one is managed; the others' windows are left where they are. |

* Triggered by the WinEvent hooks through the tracker (open / close / show / hide / cloak, minimize start / end,
  move-size start / end). Bursts are coalesced (150 ms), so several windows opening at once or a re-created window
  (removed + added) give one re-tile.
* **No loop:** a re-tile only runs when the tile set's signature changes (entries, their window keys, the layout
  settings). Window *movements*, including CIRANA's own, are never a trigger. A rate limit (8 re-tiles per 4 s, then
  a 2 s pause) is a second safeguard against applications that react to being moved.
* **Only reliable windows** take part: those bound to an entry unambiguously (or by your choice). Unrelated windows
  and windows several entries could claim are never touched. A recycled handle is never moved.
* Windows Windows will not let CIRANA move (higher rights) are left out so the others fill the screen, a hung window
  is skipped for that round, fixed-size windows keep their size, minimum-size windows get room or float (see
  *Minimum sizes*); each problem shows on its row and is reported once. Multi-monitor and DPI handling are the same as for *Apply layout*.
* Every re-tile is a cancellable worker job with a generation; switching workspace, a drag, a newer re-tile or
  quitting drops it and its late result. With tiling off, *Apply layout* and activation behave exactly as before;
  with it on, *Apply layout* re-tiles over the windows that are actually there. When CIRANA closes, windows stay put.

### Keyboard control and monocle

Global shortcuts (Settings → *Keyboard and focus* → *Edit shortcuts…*) act on the window that has the keyboard focus.
The focused window is read back from Windows (`GetForegroundWindow`), falling back to the tracker's
`foreground_key` when the foreground is CIRANA's own window (a button was clicked).

| Default | Command |
|---|---|
| Ctrl+Alt+←/→/↑/↓ | focus the neighbouring window (tiles of every monitor; without tiling: any visible window) |
| Ctrl+Alt+Shift+←/→/↑/↓ | swap with the neighbouring tile; at a monitor's edge: move to the next monitor |
| Ctrl+Alt+Win+→ / ← | make the tile wider / narrower |
| Ctrl+Alt+Win+↓ / ↑ | make the tile taller / lower |
| Ctrl+Alt+Enter | monocle on / off |
| Ctrl+Alt+Space | float the window / tile it again |
| Ctrl+Alt+PageDown / PageUp | next / previous window of the stack |
| Ctrl+Alt+Backspace | take the window out of its stack |
| Ctrl+Alt+Shift+PageDown / PageUp | move the window to the next / previous workspace |
| (none) | stack with the tile left / right / above / below; activate the next / previous workspace |

* **Registration.** `RegisterHotKey(NULL, …, MOD_NOREPEAT)` on a thread of CIRANA's own with its message loop; no
  keyboard hook. `WM_HOTKEY` reaches the GUI thread through a queued signal. A shortcut another program holds (error
  1409) is reported for that shortcut (Settings and the log); the others keep working. Shortcuts are registered only
  while CIRANA runs (`main()`), never by the tests' `build_app`.
* **Conflicts.** The editor refuses shortcuts Windows (Win+…, Alt+Tab, Alt+F4, Ctrl+Esc, Game Bar …), almost every
  application (Ctrl+C/V/S/Z …, Ctrl+Tab, Alt+←/→, Alt+letter menus) or CIRANA's own window (Ctrl+K, Ctrl+B, Ctrl+1–6)
  already use, and duplicates. It warns about Ctrl+Alt+letter / digit / punctuation: on keyboards with AltGr
  (Turkish Q, German …) that is AltGr and types characters such as @ € [ ]. The defaults avoid all of these; the
  editor uses modifier boxes and a key list, so keys the shell swallows can still be chosen.
* **Focus.** One `SetForegroundWindow` per command. Windows lets the process that received the last input (the
  hotkey) hand the focus on; when it refuses, that is reported once and nothing is retried or forced (no input
  injection, no attached input queues). Focus changes never start a re-tile.
* **Resize.** BSP: the split nearest to the tile in that direction moves by 5 % (10–90 %). Side by side: column
  weights. Main + side panel: the main width (from any tile) and the side windows' heights. Grid tiles cannot be
  resized alone with the keyboard (the message says so; the mouse can). Workspace layouts store the result with the workspace; all-window tiling per
  monitor (`global_adjust`). Keyboard commands are re-tiled at once and are not rate limited.
* **Resize with the mouse.** At `EVENT_SYSTEM_MOVESIZESTART` the window's frame is read, at `END` again; one edge
  per axis moved and the opposite one stayed = a resize (a window whose size did not change was moved; one whose both
  edges shifted was snapped: both are drops). The edge is mapped to the divider it belongs to by the pure
  `layout.resize_tile` (the same function draws the preview and commits the result): BSP: the nearest split on that side
  (`bsp.resize_edge`, ratio 10-90 %); side by side: the weights of the two neighbouring columns; main + side: the
  `ratio` (from either side of the divider) and the side windows' `heights`; grid: dedicated `grid_cols` / `grid_rows`
  (full rows only; the stretched last row has no vertical divider of its own, and the message says so). Only the two
  tiles at the divider change, nobody overlaps or leaves the work area, known minimum sizes and a small share of the
  area are kept, an edge on the border of the screen has no divider (the window goes back and the reason is shown). While the
  button is held the grey preview shows the resized tile and the other tiles at their real target sizes
  (`resize_preview` for the neighbours, `drop_preview` for the tile); borders follow. The result is stored like the
  keyboard's: in the workspace, or per monitor in `global_adjust` (BSP trees: runtime state), and survives
  workspace switches, monitors being unplugged and a change of the gap (a different layout kind or main width
  replaces it). New windows get an average share of stored weights. A layout substituted for minimum sizes cannot be
  resized.
* **Monocle** shows the focused tile over the whole work area (minus the gap) and raises it; the others keep their
  tiles behind it. Nothing in the layout changes, so switching it off puts every window back exactly where it was.
  It ends when its window closes, minimizes or maximizes, when it is dragged, and when a new window joins the layout.
  Its border is twice as thick.

### Smart tiling: BSP, automatic layouts, window rules

* **BSP** (`wm/bsp.py`): a new window splits the tile that had the focus last (else the last one) along its longer
  side; a closed window's sibling takes its space and nothing else moves. Windows that already are where they belong
  are not touched again during a re-tile. Workspace trees are stored with the workspace's layout, by entry id
  (`layout.bsp`), so they survive restarts; all-window tiling keeps one tree per monitor in memory (its leaves are
  runtime window keys). Swaps (keyboard or drag) exchange leaves, drops onto another monitor rename leaves across
  trees. Minimum sizes move the splits (`bsp.rects(…, minimums)`), and the overflow / floating rules are the same as
  for the other layouts. Every caller of `tile()` (re-tile, drop preview, *Apply layout*, the layout preview) passes
  the tile ids, so all of them use the same tree.
* **Automatic** picks a layout from the number of tiles (default: 2 side by side, 3 main + side panel, 4 or more a
  grid; each step can be side by side, main + side, grid or BSP). It is resolved once per round, before minimum sizes
  are considered, so a window that floats because of its minimum cannot flip the layout back and forth.
* **Window rules** (Settings → *Window rules*; `config.json` `window_rules`): executable (file name or full path),
  window class, AppUserModelID and an optional title filter; every field that is set must match (case-insensitive);
  a rule without executable, class or AUMID is refused, so a title can never catch unrelated windows. Actions:
  *float* (never tiled by either scope), *ignore* (not tiled, stacked, focused by the keyboard, bordered, and no
  workspace entry binds it automatically) and *belongs to a workspace* (while that workspace is active its dynamic
  tiling arranges every matching window, after its own entries, and activation raises them; no entries are added,
  nothing is started, a window already bound to one of its entries is not counted twice). First enabled match wins;
  the list can be reordered. The editor fills the fields from an open window and shows how many open windows match.

### Window stacks, moving windows, focus follows the mouse

* **Stacks**: several windows share one tile; one is shown. The windows stay independent top-level windows
  (no `SetParent`, no style change, taskbar buttons and Alt+Tab unchanged). By default the others lie under the
  shown one at the same place (nothing is hidden, so nothing can be lost even after a crash). With *Stacked windows
  … are minimized* (`stack_hide = "minimize"`) they are minimized with `SW_SHOWMINNOACTIVE` (never `SW_HIDE`): the
  taskbar button stays. Those windows are written to `stack_hidden.json` and shown again (`SW_SHOWNOACTIVATE`, only if
  the handle still belongs to the same process) when they leave the stack or it ends, when the scope is no longer
  managed (workspace switch, tiling off), when CIRANA closes, and after a crash at the next start. Restoring a hidden
  window from the taskbar, or Alt+Tab to it, makes it the shown one; closing the shown one shows the next; a stack with
  one window left ends. The shown window's border carries one tab per window (the shown one green).
* Workspace stacks are stored with the workspace by entry id (`layout.stacks`), with the shown window, so a workspace
  switch keeps them; the window that had the focus last in a workspace gets it back on activation, and the entry
  order is unchanged.
* **Moving**: swap at a monitor's edge moves the window to the next monitor (all-window tiling: into that monitor's
  layout; a workspace layout fills one monitor, so there the window is moved at the same relative place and floats).
  Windows that are not tiled are moved the same way. *Move to the next / previous workspace* removes the window's
  entry from the active workspace and adds it, bound, to the other one; the window stays open where it is.
* **Focus follows the mouse** (off by default): Windows does not let a background program take the focus because the
  mouse moved, and CIRANA does not get around that. It switches on Windows' own *active window tracking*
  (`SPI_SETACTIVEWINDOWTRACKING`, with a delay and optionally raising) for this session only (`fWinIni = 0`: nothing
  written to the profile), keeps the user's own values in `config.json` while it has changed them, and puts them back
  when it is switched off, when CIRANA closes, and after a crash at the next start. It applies to every window.

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
  The tiling panel offers BSP and automatic (with its per-count choices) and shows "n windows tiled · k stacks ·
  monocle"; each row says *tiled*, *monocle*, *stack 2/3 (shown / hidden)* or why it floats, and its ☰ menu has
  monocle, *Stack with…*, *Show in its stack*, *Take out of the stack*, *Float this window* and *Window rule for
  this program…*.
* **Workspaces:** the layout picker offers BSP and automatic too; the state line shows stacks and monocle, rows show
  how tiling treats their window, and the preview draws a stack as one tile.
* **Settings:** *Keyboard and focus* (shortcuts on / off, which work, the editor, how stacked windows are hidden,
  focus follows the mouse with delay and raise) and *Window rules* (new, edit, reorder, delete).
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
* Dynamic tiling reacts to the mouse move loop only: a window moved with the keyboard (Win+Arrow snapping) or by the
  application itself stays where it went until the next change of the workspace's windows re-tiles it. Dragging
  onto another tile swaps the two; resizing an edge changes the layout's proportions (see *Resize with the mouse*).
* Real mouse drags of tiled windows were tested with simulated move-loop events, not with a physical drag. The
  global shortcuts were tested by registering them on the real system (taken / released, error 1409) and by
  delivering the hotkey message; pressing them physically was not automated (synthetic input is refused).
* In a grid the mouse moves whole columns and rows; the tiles of a stretched last row have no vertical divider, so
  they can only change their height. Side by side has no heights to change and main + side no widths for the side
  windows alone: such a resize goes back to the layout and says why. Keyboard resize of a grid is not available.
* A workspace's layout fills one monitor: moving one of its windows to another monitor makes it float there.
  Swapping with a window a rule assigns to a workspace exchanges BSP tiles, but that order is not stored (such
  windows have no entry).
* Focus follows the mouse uses Windows' system-wide setting, so it applies to all windows while it is on.
* A window that is restored from the taskbar within a few milliseconds of a workspace switch can briefly be counted
  as minimized until the tracker reports it (the next event re-tiles it).
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
