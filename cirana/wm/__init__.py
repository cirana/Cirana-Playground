"""CIRANA's window manager: finds, identifies, arranges and groups the real top-level windows on the desktop.

Nothing here re-parents a window or changes its styles; windows stay ordinary Windows windows that keep working
whether CIRANA runs or not. Layers, from the bottom:

* ``identity``  - who a window belongs to (pid + process creation time + image + AppUserModelID) and match rules
* ``filters``   - which top-level windows a user would call "an application window"
* ``backend``   - the Win32 calls, behind one object so tests can substitute a simulated desktop
* ``tracker``   - live inventory from WinEvent hooks plus a periodic EnumWindows reconcile (off the GUI thread)
* ``monitors`` / ``layout`` - monitor work areas and pure layout geometry
* ``controller`` - moves, sizes, restores and raises windows, verifying the result and reporting refusals
"""
