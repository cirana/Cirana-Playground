<div align="center">

<img src="docs/assets/logo.png" width="112" alt="CIRANA logo">

# CIRANA

### Every window, in its place.

CIRANA launches your applications and arranges their real windows into workspaces and tiled layouts on Windows.<br>
Open something and it takes its place. No embedding, no restyling, nothing leaves your computer.

[Get started](#getting-started) · [See it in action](#see-it-in-action) · [Shortcuts](docs/shortcuts.md) · [Architecture](docs/architecture.md)

</div>

<!--
  SLOT hero  ->  docs/assets/hero.png  (1600 px wide; see docs/assets/README.md)
  Delete the comment markers to show it:

  <p align="center"><img src="docs/assets/hero.png" alt="CIRANA's Home page beside tiled application windows" width="960"></p>
-->

<br>

## Why CIRANA?

**One click to a working setup.** Save a group of applications as a workspace, for example *Development* with an editor, a
browser and a terminal. Activating it reuses the windows that are open, starts the ones that are missing, places
them side by side or in a grid, and brings them to the front.

**Windows arrange themselves.** With dynamic tiling, a new window gets a tile and the others make room. A closed window
gives its space back. A layout engine picks the arrangement that suits the number of windows, the shape of the
monitor and the minimum sizes the applications insist on.

**Your applications stay yours.** CIRANA never embeds, re-parents or restyles another window. Every application keeps
its own title bar, taskbar button and Alt+Tab entry, and keeps running if CIRANA closes. It is fully offline, with
no account, server or telemetry. Data lives in `%LOCALAPPDATA%\CIRANA\`.

<br>

## Features

### Find and launch anything

Home, Applications and Favorites show everything CIRANA found in the Start Menu, packaged apps and App Paths
registrations, sorted into categories you can rename. Press `Ctrl+K` to search applications, categories and workspaces from anywhere in the window.

<!--
  SLOT dashboard  ->  docs/assets/dashboard.png
  <p align="center"><img src="docs/assets/dashboard.png" alt="CIRANA Home with workspace cards and application categories" width="900"></p>
-->

### Workspaces

A workspace is a named group of applications or open windows with its own layout, monitor, gap and main-panel width.
Reorder entries by drag and drop, choose between several windows of one program, and see a live preview of the
layout before you apply it. Closing a workspace only stops managing its windows; every application keeps running.

<!--
  SLOT workspaces  ->  docs/assets/workspaces.png
  <p align="center"><img src="docs/assets/workspaces.png" alt="The Workspaces page with a layout preview" width="900"></p>
-->

### Adaptive tiling

*Automatic (adaptive)* tries BSP, main + side panel, grid, a single column and side by side, scores each one, and keeps
the best. It takes the monitor's shape into account (three windows go side by side on an ultrawide and into a column on a
portrait monitor), avoids slivers and tiny tiles, and keeps your layout steady instead of flipping between
arrangements. Applications that refuse to shrink below a minimum size are given room or float instead of being
cut off.

<!--
  SLOT adaptive-layout  ->  docs/assets/adaptive-layout.png
  <p align="center"><img src="docs/assets/adaptive-layout.png" alt="Adaptive layout with several tiled windows" width="900"></p>
-->

### Applications side by side

Every managed window gets a thin border: green for the focused one, grey for the others. Drag a window onto another to
swap them, and a translucent preview shows where it will land. Drag an edge to resize, and the neighbours follow.

<!--
  SLOT side-by-side  ->  docs/assets/side-by-side.png
  <p align="center"><img src="docs/assets/side-by-side.png" alt="Several applications tiled side by side with a focus border" width="900"></p>
-->

### BSP, stacks and monocle

In a BSP layout a new window splits the tile that has focus. Stack several windows into one tile and flip through
them, or use **monocle** to give the focused window the whole screen and put everything back exactly as it was.

<!--
  SLOT monocle  ->  docs/assets/monocle.png
  <p align="center"><img src="docs/assets/monocle.png" alt="A window in monocle mode filling the work area" width="900"></p>
-->

### Keyboard control

Move focus, swap tiles, resize, float, stack and send windows to another workspace with global shortcuts that
need no mouse and work in every application. See [Keyboard shortcuts](#keyboard-shortcuts).

### Smooth motion

Windows glide to their new places, focus borders fade and the drop preview moves between targets. It is purely
visual: the final positions are exactly the same with or without animation, and you can turn it off.

<br>

## See it in action

A short demo is planned. It will show:

- a window opening and settling into the layout
- two windows swapping places
- an animated re-tile when a window closes
- switching a window to monocle and back

<!--
  SLOT demo  ->  docs/assets/demo.gif  (1280 px wide, under 5 MB)
  <p align="center"><img src="docs/assets/demo.gif" alt="CIRANA tiling, swapping and monocle in action" width="900"></p>
-->

<br>

## Getting started

CIRANA is run from source for now. No installer or release is published yet.

**Prerequisites:** Windows 11 and Python 3.11 or newer (`py -0p` lists the versions you have).

```powershell
git clone https://github.com/cirana/Cirana-Playground.git
cd Cirana-Playground
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m cirana
```

**First run**

1. CIRANA scans for your applications once and caches the list. Use *Scan now* in Settings to refresh it.
2. On a **new installation, all-window tiling is on** and the layout is *Automatic (adaptive)*. Your open
   application windows are arranged straight away. To turn it off, clear *Tile all windows* on the Windows page.
3. Open **Workspaces**, create one, add applications (*Add applications…*) or windows that are already open
   (*Add open windows…*), choose a layout and press *Activate*.
4. Press `Ctrl+K` to search and launch, and use the shortcuts below to move things around.

If you used an earlier version, your settings are left exactly as they are. Layouts you chose stay chosen, and *Automatic
(adaptive)* is one more entry in the layout list.

**Build an executable.** `pip install pyinstaller` and `pyinstaller cirana.spec --noconfirm` produce a one-folder build
in `dist\CIRANA\`.

<br>

## Keyboard shortcuts

The defaults act on the window that has the focus.

| Shortcut | Action |
|---|---|
| `Ctrl+Alt+` arrows | Focus the neighbouring window |
| `Ctrl+Alt+Shift+` arrows | Swap with the neighbouring tile, or move to the next monitor at the edge |
| `Ctrl+Alt+Win+` arrows | Make the tile wider, narrower, taller or lower |
| `Ctrl+Alt+Enter` | Monocle on / off |
| `Ctrl+Alt+Space` | Float the window / tile it again |
| `Ctrl+Alt+PageDown` / `PageUp` | Next / previous window of the stack |
| `Ctrl+Alt+Backspace` | Take the window out of its stack |
| `Ctrl+Alt+Shift+PageDown` / `PageUp` | Move the window to the next / previous workspace |
| `Ctrl+K` | Search and launch (inside CIRANA) |

Every shortcut can be changed in **Settings → Keyboard and focus → Edit shortcuts…**. The full list, including actions
with no default shortcut, is in [docs/shortcuts.md](docs/shortcuts.md).

<br>

## Customization

**Layout.** On the Windows page, choose the layout for all-window tiling: *Automatic (adaptive)* (the default for new
installations), *By window count*, *Side by side*, *Main + side panel*, *Grid 2×2*, *Dynamic grid* or *BSP*, plus gap and
main-panel width. Picking a layout by hand switches the adaptive engine off for that scope. Choosing *Automatic
(adaptive)* again turns it back on.

**Floating windows.** Press `Ctrl+Alt+Space` to float the focused window, or use the ☰ menu on its row in the Windows page. Under
*Settings → Window rules* you can make a program always float, always be ignored, or belong to a workspace.
Windows without a sizing border, such as fixed dialogs and full-screen games, are never tiled.

**Workspaces.** Each workspace has its own layout, monitor, gap and main-panel width, and a *Dynamic tiling* switch that
makes its windows follow the layout as they open and close.

**Animation.** *Settings → Animations* controls window movement (on by default, 180 ms, up to 350 ms, or 0 for none),
the focus-border fade and the drop-preview glide. The *Borders* switch on the Windows page hides the focus borders.

**Focus follows the mouse.** Off by default. While on, it uses Windows' own setting for all windows, and your own value comes
back when CIRANA closes.

<br>

## Requirements

- **Windows 11.** CIRANA is developed and tested on Windows 11. The code has no version check, but Windows 10 has not
  been verified.
- **Python 3.11+** with PySide6 6.6+, pywin32 and psutil (all in `requirements.txt`) to run from source.
- Multiple monitors and per-monitor DPI are supported.
- Windows does not let a normal program move an elevated application's window. CIRANA reports it and leaves that window
  alone. Windows on other virtual desktops are not managed. More in [docs/limitations.md](docs/limitations.md).

<br>

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

The regular suite runs the window manager against a simulated desktop and never touches a real window. Opt-in live
tests (`CIRANA_LIVE=1`) exercise real Win32 windows. Set `CIRANA_HOME` to use a separate data folder.

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Modules, window tracking, layouts, tiling, shortcuts, data |
| [docs/testing.md](docs/testing.md) | Test strategy, live tests, what is not covered |
| [docs/limitations.md](docs/limitations.md) | Known limitations |
| [docs/shortcuts.md](docs/shortcuts.md) | Every keyboard shortcut |
| [docs/assets/README.md](docs/assets/README.md) | How the screenshots and demo are captured |
