"""Phase 3: workspaces at runtime on a simulated desktop - binding windows, activating, launching, layouts.

Covers: reusing open windows, starting missing applications, concurrent launches of one program, choosing between
several windows, restoring after a CIRANA restart, recycled handles, refused (elevated) windows, cancelled sessions
with late results, and that nothing outside the activated group is touched or ended.
"""
import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cirana.core.application_registry import ApplicationRegistry  # noqa: E402
from cirana.core.models import AppEntry, LaunchResult, stable_app_id  # noqa: E402
from cirana.core.workspace_controller import EntryState, WorkspaceController, plan_layout  # noqa: E402
from cirana.core import workspace_launcher  # noqa: E402
from cirana.core.workspace_launcher import WorkspaceLauncher  # noqa: E402
from cirana.core.workspaces import WorkspaceManager  # noqa: E402
from cirana.storage.settings_manager import SettingsManager  # noqa: E402
from cirana.wm.controller import WindowController  # noqa: E402
from cirana.wm.layout import FREE, GRID, SIDE_BY_SIDE, LayoutSpec, tile  # noqa: E402
from cirana.wm.monitors import Monitor  # noqa: E402
from cirana.wm.tracker import WindowTracker  # noqa: E402
from tests.fake_desktop import FakeDesktop  # noqa: E402
from tests.support import TempHomeTestCase  # noqa: E402

app = QApplication.instance() or QApplication([])
LEFT = Monitor("\\\\.\\DISPLAY1", (0, 0, 2000, 1000), (0, 0, 2000, 960), True, 96)
RIGHT = Monitor("\\\\.\\DISPLAY2", (2000, 0, 4000, 1000), (2000, 0, 4000, 1000), False, 96)


def exe_app(name, exe=None):
    path = exe or f"C:\\Apps\\{name}.exe"
    return AppEntry(id=stable_app_id(name), name=name, launch_type="executable", target=path, source="user",
                    exe_path=path)


class Env(TempHomeTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(app.processEvents)  # deferred saves run before the data directory is removed
        self.desktop = FakeDesktop(monitors_=[LEFT, RIGHT])
        self.launches: list[str] = []
        self.on_launch = {}  # app name -> callable(pid) run when it is "started"
        self.alive: set[int] = set()
        self.next_pid = 5000
        self.build()

    def build(self):
        self.settings = SettingsManager(self.home)
        self.settings.set("animation_ms", 0)  # windows jump to their place: tests read positions (see test_animation)
        self.registry = ApplicationRegistry(self.settings)
        self.registry.apply_scan([exe_app("Code"), exe_app("Brave"), exe_app("Notes"),
                                  exe_app("Shell", "C:\\Windows\\System32\\powershell.exe")])
        self.tracker = WindowTracker(self.desktop, hooks=False)  # fed directly, see sync()
        self.manager = WorkspaceManager(self.settings, self.registry, monitors=self.desktop.monitors)
        self.launcher = WorkspaceLauncher(self.registry, self.tracker, launcher=self.fake_launch,
                                          process_tree=lambda tree: bool(tree & self.alive))
        self.wm = WorkspaceController(self.manager, self.registry, self.tracker,
                                      WindowController(self.desktop, settle=0.0), self.launcher)
        self.messages, self.choices = [], []
        self.wm.message.connect(self.messages.append)
        self.wm.choice_needed.connect(lambda w, e: self.choices.append((w, e)))
        self.sync()

    def fake_launch(self, app_entry):
        self.launches.append(app_entry.name)
        if app_entry.name == "Broken":
            return LaunchResult(False, "Executable not found")
        self.next_pid += 1
        self.alive.add(self.next_pid)
        hook = self.on_launch.get(app_entry.name)
        if hook:
            hook(self.next_pid)
        return LaunchResult(True, pid=self.next_pid)

    def sync(self):
        self.tracker.feed_snapshot([w for w in (self.desktop.describe(h) for h in self.desktop.enum_windows()) if w])

    def pump(self, until=None, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if until is not None and until():
                return True
            time.sleep(0.005)
        app.processEvents()
        return until is None

    def idle(self, workspace_id):
        return self.pump(lambda: not self.wm.busy(workspace_id))

    def states(self, workspace_id):
        return [s.state for s in self.wm.statuses(workspace_id)]

    def workspace(self, *names, layout=None):
        w = self.manager.create("Dev", [stable_app_id(n) for n in names])
        if layout is not None:
            self.manager.set_layout(w.id, layout)
        return w


class ActivationTests(Env):
    def test_open_windows_are_reused_and_missing_ones_started(self):
        code = self.desktop.add("c:\\apps\\code.exe", "main.py - Code")
        self.sync()
        w = self.workspace("Code", "Notes", layout=LayoutSpec(SIDE_BY_SIDE, gap=0))
        self.on_launch["Notes"] = lambda pid: self.desktop.add("c:\\apps\\notes.exe", "Notes", pid=pid)
        self.assertEqual(self.states(w.id), [EntryState.RUNNING, EntryState.MISSING])
        self.wm.activate(w.id)
        self.assertTrue(self.pump(lambda: self.launcher.pending(w.id) and
                                  all(p.launched for p in self.launcher._pending)))
        self.sync()  # the tracker sees Notes' new window
        self.assertTrue(self.idle(w.id))
        self.assertEqual(self.launches, ["Notes"])  # Code was open: never started again
        self.assertEqual(self.states(w.id), [EntryState.RUNNING, EntryState.RUNNING])
        left, right = tile(LayoutSpec(SIDE_BY_SIDE, gap=0), LEFT.work, 2)
        notes = next(x for x in self.desktop.windows.values() if x.title == "Notes")
        self.assertEqual((code.rect, notes.rect), (left, right))
        self.assertEqual(self.desktop.foreground, code.hwnd)  # the group was raised, the first window on top
        self.assertEqual(self.manager.active_id, w.id)

    def test_other_windows_and_workspaces_are_left_alone(self):
        code = self.desktop.add("c:\\apps\\code.exe", "Code")
        stranger = self.desktop.add("c:\\apps\\game.exe", "Game", rect=(5, 5, 300, 300))
        other = self.manager.create("Other", [stable_app_id("Brave")])
        brave = self.desktop.add("c:\\apps\\brave.exe", "Brave", rect=(50, 50, 400, 400))
        self.sync()
        w = self.workspace("Code")
        self.wm.activate(other.id)
        self.idle(other.id)
        brave_place = brave.rect
        self.desktop.calls.clear()
        self.wm.activate(w.id)
        self.assertTrue(self.idle(w.id))
        touched = {call[1] for call in self.desktop.calls}
        self.assertEqual(touched, {code.hwnd})  # not the game, not the other workspace's Brave
        self.assertEqual((stranger.rect, brave.rect, brave.minimized), ((5, 5, 300, 300), brave_place, False))

    def test_windows_moved_by_the_user_are_not_pulled_back(self):
        code = self.desktop.add("c:\\apps\\code.exe", "Code")
        self.sync()
        w = self.workspace("Code")
        self.wm.activate(w.id)
        self.idle(w.id)
        code.rect = (300, 300, 800, 800)  # the user drags it away
        self.sync()
        self.pump(timeout=0.2)
        self.assertEqual(code.rect, (300, 300, 800, 800))
        self.wm.apply_layout(w.id)  # only an explicit request arranges it again
        self.idle(w.id)
        self.assertNotEqual(code.rect, (300, 300, 800, 800))

    def test_a_refused_window_is_reported_and_the_others_are_arranged(self):
        code = self.desktop.add("c:\\apps\\code.exe", "Code")
        self.desktop.add("c:\\apps\\notes.exe", "Notes (admin)", elevated=True)
        self.sync()
        w = self.workspace("Notes", "Code", layout=LayoutSpec(SIDE_BY_SIDE, gap=0))
        self.wm.activate(w.id)
        self.idle(w.id)
        self.assertEqual(code.rect, tile(LayoutSpec(SIDE_BY_SIDE, gap=0), LEFT.work, 2)[1])
        self.assertTrue(any("higher rights" in m for m in self.messages))
        status = self.wm.statuses(w.id)[0]
        self.assertIn("higher rights", status.message)  # shown on the entry's row

    def test_a_failing_launch_does_not_stop_the_rest(self):
        self.registry.apply_scan(self.registry.discovered_apps() + [exe_app("Broken")])
        self.on_launch["Notes"] = lambda pid: self.desktop.add("c:\\apps\\notes.exe", "Notes", pid=pid)
        w = self.workspace("Broken", "Notes")
        self.wm.activate(w.id)
        self.assertTrue(self.pump(lambda: any("not found" in m for m in self.messages)))
        self.pump(lambda: all(p.launched for p in self.launcher._pending))
        self.sync()
        self.assertTrue(self.pump(lambda: self.states(w.id)[1] is EntryState.RUNNING))
        self.assertIn("not found", self.wm.statuses(w.id)[0].message)

    def test_closing_a_workspace_ends_nothing_and_moves_nothing(self):
        code = self.desktop.add("c:\\apps\\code.exe", "Code", rect=(10, 10, 500, 500))
        self.sync()
        w = self.workspace("Code")
        self.manager.set_active(w.id)
        self.desktop.calls.clear()
        self.wm.close(w.id)
        self.assertEqual(self.desktop.calls, [])
        self.assertIn(code.hwnd, self.desktop.windows)
        self.assertEqual(self.manager.active_id, "")

    def test_saved_positions_put_windows_back(self):
        code = self.desktop.add("c:\\apps\\code.exe", "Code", rect=(2100, 100, 3100, 900))  # on the right monitor
        notes = self.desktop.add("c:\\apps\\notes.exe", "Notes", maximized=True, rect=LEFT.work)
        self.sync()
        w = self.workspace("Code", "Notes")
        self.wm.save_positions(w.id)
        self.assertTrue(self.pump(lambda: self.manager.get(w.id).layout.kind == FREE))
        code.rect, notes.maximized, notes.rect = (0, 0, 400, 400), False, (0, 0, 400, 400)
        self.sync()
        self.wm.apply_layout(w.id)
        self.idle(w.id)
        self.assertEqual(code.rect, (2100, 100, 3100, 900))
        self.assertTrue(notes.maximized)

    def test_saved_monitor_unplugged_falls_back_to_the_primary(self):
        code = self.desktop.add("c:\\apps\\code.exe", "Code")
        self.sync()
        w = self.workspace("Code", layout=LayoutSpec(GRID, gap=0, monitor=RIGHT.ref()))
        self.desktop.monitors_ = [LEFT]  # the right monitor was unplugged
        self.wm.apply_layout(w.id)
        self.idle(w.id)
        self.assertEqual(code.rect, LEFT.work)


class BindingTests(Env):
    def test_several_fitting_windows_ask_the_user(self):
        one = self.desktop.add("c:\\apps\\brave.exe", "Mail - Brave")
        two = self.desktop.add("c:\\apps\\brave.exe", "Docs - Brave")
        self.sync()
        w = self.workspace("Brave")
        self.assertEqual(self.states(w.id), [EntryState.AMBIGUOUS])
        self.wm.activate(w.id)
        self.idle(w.id)
        self.assertEqual(self.launches, [])  # running already: never started a third copy
        self.assertEqual(self.choices, [(w.id, w.apps[0].id)])
        self.assertTrue(self.wm.choose(w.id, w.apps[0].id, self.desktop.tracked(two.hwnd).key))
        self.idle(w.id)
        self.assertEqual(self.wm.statuses(w.id)[0].window.hwnd, two.hwnd)
        self.assertEqual(two.rect, tile(LayoutSpec(), LEFT.work, 1)[0])  # placed right after the choice
        self.assertEqual(one.rect, (100, 100, 900, 700))

    def test_two_entries_of_one_program_get_two_different_windows(self):
        self.desktop.add("c:\\apps\\code.exe", "a.py - Code")
        self.desktop.add("c:\\apps\\code.exe", "b.py - Code")
        self.sync()
        w = self.manager.create("Two editors")
        windows = self.tracker.windows()
        first = self.manager.add_window(w.id, windows[0])
        second = self.manager.add_window(w.id, windows[1])
        self.wm.choose(w.id, first.id, windows[0].key, place=False)
        statuses = self.wm.statuses(w.id)  # the other window settles the second entry by itself
        self.assertEqual([s.state for s in statuses], [EntryState.RUNNING, EntryState.RUNNING])
        self.assertNotEqual(statuses[0].window.key, statuses[1].window.key)
        self.assertEqual(statuses[1].entry.id, second.id)

    def test_windows_are_found_again_after_a_restart(self):
        self.desktop.add("c:\\apps\\brave.exe", "Mail - Brave")
        docs = self.desktop.add("c:\\apps\\brave.exe", "Docs - Brave")
        self.desktop.add("c:\\apps\\code.exe", "Code")
        self.sync()
        w = self.workspace("Brave", "Code")
        self.wm.choose(w.id, w.apps[0].id, self.desktop.tracked(docs.hwnd).key, place=False)
        self.pump(timeout=0.1)  # the title hint is saved
        self.build()  # CIRANA restarts: nothing but the workspace file survives
        statuses = self.wm.statuses(w.id)
        self.assertEqual([s.state for s in statuses], [EntryState.RUNNING, EntryState.RUNNING])
        self.assertEqual(statuses[0].window.hwnd, docs.hwnd)  # picked again by its title

    def test_closed_window_and_recycled_handle(self):
        code = self.desktop.add("c:\\apps\\code.exe", "Code")
        self.sync()
        w = self.workspace("Code")
        self.assertEqual(self.states(w.id), [EntryState.RUNNING])
        self.desktop.close(code.hwnd)
        self.desktop.add("c:\\apps\\other.exe", "Someone else", hwnd=code.hwnd, pid=777)  # same handle value
        self.sync()
        self.assertEqual(self.states(w.id), [EntryState.MISSING])
        self.desktop.calls.clear()
        self.wm.apply_layout(w.id)
        self.idle(w.id)
        self.assertEqual(self.desktop.calls, [])  # the stranger behind the old handle is never moved

    def test_entries_without_start_and_unavailable_apps(self):
        w = self.workspace("Code", "Notes")
        self.manager.set_launch(w.id, w.apps[0].id, False)
        self.registry.apply_scan([exe_app("Code")])  # Notes was uninstalled
        self.assertEqual(self.states(w.id), [EntryState.STOPPED, EntryState.UNAVAILABLE])
        self.wm.activate(w.id)
        self.idle(w.id)
        self.assertEqual(self.launches, [])


class LaunchTests(Env):
    def start(self, w):
        self.wm.activate(w.id)
        self.assertTrue(self.pump(lambda: self.launcher._pending and all(p.launched for p in self.launcher._pending)))

    def test_concurrent_launches_of_one_program_get_one_window_each(self):
        w = self.manager.create("Two")
        brave = stable_app_id("Brave")
        self.manager.add_app(w.id, brave)
        entry = self.manager.get(w.id).apps[0]
        copy = self.manager.add_window(w.id, self.desktop.tracked(self.desktop.add("c:\\apps\\brave.exe", "x").hwnd))
        self.assertEqual(copy.app_id, brave)  # a second Brave entry
        self.desktop.windows.clear()
        self.sync()
        self.assertEqual(self.states(w.id), [EntryState.MISSING, EntryState.MISSING])
        self.start(w)
        self.assertEqual(self.launches, ["Brave", "Brave"])
        first = self.desktop.add("c:\\apps\\brave.exe", "Window 1")
        second = self.desktop.add("c:\\apps\\brave.exe", "Window 2")
        self.sync()
        self.assertTrue(self.idle(w.id))
        statuses = self.wm.statuses(w.id)
        self.assertEqual({s.window.hwnd for s in statuses}, {first.hwnd, second.hwnd})
        self.assertEqual(statuses[0].entry.id, entry.id)

    def test_window_is_attributed_through_the_process_tree(self):
        # A launcher stub (Discord's Update.exe) starts the real program and exits; its window is the program's.
        discord = AppEntry(id=stable_app_id("Discord"), name="Discord", launch_type="shortcut",
                           target="C:\\x\\Discord.lnk", source="start_menu_user",
                           exe_path="C:\\Users\\u\\AppData\\Local\\Discord\\Update.exe")
        self.registry.apply_scan(self.registry.discovered_apps() + [discord])
        w = self.manager.create("Chat", [discord.id])
        self.start(w)
        stub = self.launcher._pending[0].pid
        self.launcher._pending[0].tree.add(stub + 1000)  # its child, Discord.exe
        self.desktop.add("c:\\users\\u\\appdata\\local\\discord\\app-1.0\\discord.exe", "Discord", pid=stub + 1000)
        self.sync()
        self.assertTrue(self.pump(lambda: self.states(w.id) == [EntryState.RUNNING]))
        self.assertIn("discord.exe", self.manager.get(w.id).apps[0].rule.alt_exes)  # recognised after a restart

    def test_powershell_inside_windows_terminal(self):
        w = self.manager.create("Shell", [stable_app_id("Shell")])
        self.start(w)
        shell_pid = self.launcher._pending[0].pid
        terminal = self.desktop.add("c:\\program files\\windowsapps\\microsoft.windowsterminal_1_x64__8wekyb3d8bbwe"
                                    "\\windowsterminal.exe", "Windows PowerShell", pid=42,
                                    shows_pids=frozenset({shell_pid}))
        self.sync()
        self.launcher._tick()  # the periodic scan finds the terminal showing the shell's console
        self.assertTrue(self.pump(lambda: self.states(w.id) == [EntryState.RUNNING]))
        self.assertEqual(self.wm.statuses(w.id)[0].window.hwnd, terminal.hwnd)

    def test_reactivating_cancels_the_old_session_and_late_windows_are_dropped(self):
        w = self.workspace("Notes")
        self.start(w)
        old = list(self.launcher._pending)
        self.wm.close(w.id)  # cancelled: Notes keeps starting, but nothing will wait for it
        self.assertEqual(self.launcher.pending(w.id), set())
        late = self.desktop.add("c:\\apps\\notes.exe", "Notes")
        self.sync()
        self.pump(timeout=0.2)
        self.assertEqual(late.rect, (100, 100, 900, 700))  # never placed by the dead session
        self.assertTrue(old)
        # a late launch result of a cancelled session changes nothing either
        self.launcher._launched(old[0], self.registry.get(stable_app_id("Notes")), LaunchResult(True, pid=1))
        self.assertEqual(self.launcher.pending(w.id), set())

    def test_activating_again_while_starting_starts_nothing_twice(self):
        w = self.workspace("Notes")
        self.start(w)
        self.wm.activate(w.id)  # the user clicks again while Notes is still starting
        self.assertEqual(self.launches, ["Notes"])
        self.assertEqual(len(self.launcher._pending), 1)

    def test_late_launch_result_of_a_cancelled_session_is_ignored(self):
        w = self.workspace("Notes")
        gate = []
        original = self.fake_launch

        def slow(app_entry):
            while not gate:
                time.sleep(0.01)
            return original(app_entry)

        self.launcher._launch = slow
        self.addCleanup(gate.append, True)  # never leave a worker thread waiting
        self.wm.activate(w.id)
        self.wm.close(w.id)  # cancelled while the launch call is still running
        self.wm.activate(w.id)  # and activated again: a new launch
        self.assertEqual(len(self.launcher._pending), 1)
        current = self.launcher._pending[0]
        gate.append(True)
        self.assertTrue(self.pump(lambda: current.launched))
        self.pump(timeout=0.1)  # the cancelled session's result arrives as well, and changes nothing
        self.assertEqual(self.launcher._pending, [current])

    def test_handed_over_to_a_running_copy(self):
        w = self.workspace("Notes")
        self.start(w)
        pending = self.launcher._pending[0]
        self.alive.discard(pending.pid)  # the started process exited without a new window ...
        hidden = self.desktop.add("c:\\apps\\notes.exe", "Notes")  # ... the running copy showed its window
        self.tracker.feed_snapshot([])  # (it existed before the launch, so it is not a "new" window)
        pending.before = frozenset({self.desktop.tracked(hidden.hwnd).key})
        self.sync()
        self.launcher._tick()
        pending.exited_at = time.monotonic() - workspace_launcher.HANDOVER_AFTER - 1
        self.launcher._tick()
        self.assertTrue(self.pump(lambda: self.states(w.id) == [EntryState.RUNNING]))

    def test_no_window_ever_gives_up_without_touching_the_process(self):
        w = self.workspace("Notes")
        self.start(w)
        pending = self.launcher._pending[0]
        pending.started -= workspace_launcher.ALIVE_TIMEOUT + 1
        self.launcher._tick()
        self.assertTrue(any("no window of it appeared" in m for m in self.messages))
        self.assertIn(pending.pid, self.alive)  # still running: CIRANA never ends what it started

    def test_plan_layout_keeps_places_for_windows_still_starting(self):
        code = self.desktop.add("c:\\apps\\code.exe", "Code")
        window = self.desktop.tracked(code.hwnd)
        planned = plan_layout(LayoutSpec(SIDE_BY_SIDE, gap=0), [("e-1", None), ("e-2", window)], [LEFT])
        self.assertEqual([(e, p.rect) for e, p in planned], [("e-2", tile(LayoutSpec(SIDE_BY_SIDE, gap=0),
                                                                          LEFT.work, 2)[1])])


if __name__ == "__main__":
    unittest.main()
