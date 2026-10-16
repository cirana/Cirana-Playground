# Known limitations


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
