"""Reflow coordination for DynamicTiler: when a re-tile runs (events coalesced, rate limited, generations), the
worker job that places the windows (with the optional glide of ``cirana.wm.animation``) and what is done with
its result.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from cirana.core.background import run_async
from cirana.core.tiling_model import (BEHIND, GLOBAL, Group, RATE_BACKOFF_MS, RATE_LIMIT, RATE_WINDOW, RULE_SLOT,
                                      off_by)
from cirana.wm import animation, bsp
from cirana.wm.controller import TOLERANCE, Placement, Result, Status
from cirana.wm.identity import Rect, TrackedWindow
from cirana.wm.layout import keep_visible
from cirana.wm.monitors import resolve

log = logging.getLogger(__name__)


@dataclass
class ReflowJob:
    """One re-tile round as the worker thread gets it: everything decided on the GUI thread, nothing mutable."""

    groups: list[Group]
    behind: bool  # stack_hide = "behind": the hidden windows of a stack lie under the shown one
    glide: int  # milliseconds of animation (0: place at once)
    raise_group: bool | list[TrackedWindow]
    focus_after: TrackedWindow | None
    to_hide: list[TrackedWindow]
    to_show: list[TrackedWindow]
    on_top: list[TrackedWindow]
    out_of_sight: set[str]  # window keys of stack members that are not the shown one


def run_reflow(job: ReflowJob, backend, controller, plan, animated: dict, cancel):  # noqa: ANN001 - worker thread
    """Place the windows of ``job`` and report what happened: ``(targets, tiled results, floating results)``.

    Runs off the GUI thread and touches no tiler state except ``animated`` (the registry of windows a glide has moved
    and the placement has not confirmed yet). ``plan(group, units, monitors)`` gives each unit its rectangle. The
    optional glide only moves windows towards the rectangles the placement then sets exactly; a cancelled round
    places nothing more (the round that cancelled it does)."""
    monitors = backend.monitors()
    planned, refused, usable_all, floats = [], [], [], []
    for group in job.groups:
        usable = []
        for unit in group.units:
            problem = controller.check(unit.window)
            if problem is None:
                usable.append(unit)
            else:
                refused.append((unit.active, problem))
        for unit, rect in plan(group, usable, monitors):
            planned.append((group.id, unit.id, unit.active, Placement(unit.window, rect), True))
            usable_all.append(unit.window)
            if job.behind:  # the hidden windows of a stack lie under the shown one, at the same place
                for slot, window in unit.hidden():
                    if controller.check(window) is None:
                        planned.append((group.id, unit.id, slot, Placement(window, rect), False))
        monitor = resolve(group.spec.monitor, monitors)
        for unit, _why in group.floating:  # too large to tile: only made completely visible
            window = unit.window
            if monitor is None or controller.check(window) is not None or backend.is_minimized(window.hwnd):
                continue
            frame = backend.frame_rect(window.hwnd)
            if keep_visible(monitor.work, frame) != frame:
                floats.append((unit.active, Placement(window, keep_visible(monitor.work, frame))))
    placements = [p for *_ids, p, _primary in planned] + [p for _s, p in floats]

    def settled(placement: Placement) -> Result | None:
        """The window is exactly where it belongs: it is not touched again (fewer moves, no flicker)."""
        hwnd = placement.window.hwnd
        if backend.is_minimized(hwnd) or backend.is_maximized(hwnd):
            return None
        frame = backend.frame_rect(hwnd)
        if off_by(frame, placement.rect) > (0 if hwnd in animated else TOLERANCE):
            return None  # (a window left in mid-glide by a cancelled animation is never "close enough")
        return Result(placement.window.key, placement.window.display_title, Status.PLACED, actual=frame)

    results: list[Result | None] = []
    for placement in placements:
        results.append(None if controller.check(placement.window) else settled(placement))
    moving = [p for p, r in zip(placements, results) if r is None]
    if cancel.is_set():
        return [], [], []  # a newer layout (or a drag, or quitting) took over: it places the windows
    if job.glide and moving:
        animation.glide_placements(backend, controller, moving, cancel, job.glide, animated)
        if cancel.is_set():
            return [], [], []
    moved = iter(controller.place_all(moving, cancel) if moving else [])
    results = [r if r is not None else next(moved) for r in results]
    if not cancel.is_set():
        planned_keys = {p.window.key for p in placements}
        for window in job.to_show:
            if window.key not in planned_keys:
                controller.show(window)  # it left its stack: shown again, without the focus
        for window in job.to_hide:
            controller.minimize(window, quiet=True)  # a hidden window of a stack (stack_hide = minimize)
        if job.raise_group:  # (a stack's hidden windows stay hidden: only its shown one is raised)
            front = job.raise_group if isinstance(job.raise_group, list) else usable_all
            front = [window for window in front if window.key not in job.out_of_sight]
            if front:
                controller.bring_to_front(front)
        for window in job.on_top:  # a stack's shown window and the monocle window on top of their tile
            if window in usable_all:
                backend.raise_window(window.hwnd)
        if job.focus_after is not None:
            controller.focus(job.focus_after)
        animated.clear()  # every window is where the placement put it
    targets = [(unit_id, (group_id, p.rect)) for group_id, unit_id, _s, p, primary in planned if primary]
    tiled = [(slot, result) for (_g, _u, slot, _p, _primary), result in zip(planned, results)] + refused
    return targets, tiled, list(zip([slot for slot, _p in floats], results[len(planned):]))


class ReflowMixin:
    """When a re-tile runs and what happens with its result. Methods use the state ``DynamicTiler.__init__`` sets up."""

    def schedule(self, force: bool = False, user: bool = False) -> None:
        if self._stopped:
            return
        self._force = self._force or force
        self._user = self._user or user
        if not self.in_charge():
            if self._stack_hidden or self._scope_seen:  # e.g. the workspace's tiling was switched off: nothing
                self._release_hidden()  # manages its stacks any more, so their hidden windows are shown again
                self._scope_seen, self._signature = "", None
            return
        if user:
            self._timer.start(0)  # the user is waiting for it: no debounce, no back-off
        elif not (self._timer.isActive() and self._timer.interval() in (RATE_BACKOFF_MS, 0)):
            self._timer.start(self._debounce)

    def reflow(self, workspace_id: str = GLOBAL, raise_group: bool | list[TrackedWindow] = False) -> bool:
        """Re-tile now (Apply layout, activation), even if nothing changed. ``raise_group``: raise the tiled windows
        afterwards (True) or these windows (a workspace being activated under all-window tiling). False when no
        dynamic tiling is in charge of it."""
        if not (self.global_on or self.manages(workspace_id)):
            return False
        if raise_group:
            self._raise = raise_group if isinstance(raise_group, list) else True
        self.schedule(force=True)
        return True

    def _cancel(self) -> None:
        self._generation += 1
        if self._job is not None:
            self._job.cancel()
            self._job = None

    def _rate_limited(self) -> bool:
        now = time.monotonic()
        while self._history and now - self._history[0] > RATE_WINDOW:
            self._history.popleft()
        if len(self._history) >= RATE_LIMIT:
            log.warning("Dynamic tiling: %d re-tiles within %.0f s; waiting before the next one", RATE_LIMIT,
                        RATE_WINDOW)
            self._timer.start(RATE_BACKOFF_MS)
            return True
        self._history.append(now)
        return False

    def _evaluate(self) -> None:
        if not self.in_charge():
            self._force, self._raise, self._user, self._focus_after = False, False, False, None
            if self._animated:
                self._cancel()
                animation.snap_to_targets(self.wm.windows, self._animated)  # switched off in mid-glide
            return
        if not self.wm.tracker.ready:
            return  # the first window inventory is not in yet; its arrival schedules this again
        if self._dragging:
            return  # the end of the drag schedules the re-tile
        groups = self._groups()
        for group in groups:  # monocle ends when its window leaves, or another one joins the layout
            monocle = self._monocle.get(group.id)
            if monocle is not None and (not group.monocle or not {u.id for u in group.units} <= monocle[1]):
                del self._monocle[group.id]
                group.monocle = ""
        for group_id in [g for g in self._monocle if g not in {group.id for group in groups}]:
            del self._monocle[group_id]
        signature = self._signature_of(groups)
        if signature == self._signature and not self._force:
            self._user = False
            return
        if not self._user and self._rate_limited():
            return
        raise_group, self._force, self._raise, self._user = self._raise, False, False, False
        focus_after, self._focus_after = self._focus_after, None
        for group in groups:  # BSP trees with the windows that came and went: kept for the next round
            if group.bsp_active and group.spec.bsp != (
                    bsp.to_dict(self._trees.get(group.id)) if group.scope == GLOBAL
                    else self.wm.workspaces.get(group.scope).layout.bsp):
                self._set_tree(group, bsp.from_dict(group.spec.bsp))
        self._signature = signature
        self._remember_choices(groups)
        self._last_groups = groups
        self._scope_seen = signature[0]
        wanted = self._wanted_hidden(groups)
        to_hide = [window for key, window in wanted.items() if key not in self._stack_hidden]
        to_show = [window for window in (self.wm.tracker.get(k) for k in self._stack_hidden - set(wanted))
                   if window is not None]
        if to_hide or to_show:
            self._stack_hidden = set(wanted)
            self._save_hidden()
        on_top, raised = [], {}
        for group in groups:  # a stack's shown window / the monocle window goes on top when that changes
            for unit in group.units:
                if unit.stacked or unit.id == group.monocle:
                    raised[unit.id] = (unit.active, unit.id == group.monocle, tuple(s for s, _w in unit.members))
                    if self._on_top.get(unit.id) != raised[unit.id]:
                        on_top.append(unit.window)
        self._on_top = raised
        self._start(signature[0], groups, raise_group, focus_after, to_hide, to_show, on_top)

    def _start(self, scope: str, groups: list[Group], raise_group: bool | list[TrackedWindow],
               focus_after: TrackedWindow | None = None, to_hide: list[TrackedWindow] | None = None,
               to_show: list[TrackedWindow] | None = None, on_top: list[TrackedWindow] | None = None) -> None:
        """Hand one re-tile round to a worker thread (the GUI thread only decides and, later, takes the result)."""
        self._cancel()
        generation = self._generation
        glide = 0 if self._dragging else animation.duration_ms(self.settings)
        out_of_sight = {window.key for group in groups for unit in group.units + [u for u, _ in group.floating]
                        for _slot, window in unit.hidden()}
        self.count += 1
        if glide:  # the targets are known now: commands that come before the glide is over (a second swap) use them
            monitors = self._monitors()
            self._targets = {unit.id: (group.id, rect) for group in groups
                             for unit, rect in self.plan(group, group.units, monitors)}
        job = ReflowJob(groups, self.stack_mode == BEHIND, glide, raise_group, focus_after, to_hide or [],
                        to_show or [], on_top or [], out_of_sight)
        backend, controller, plan, animated = self.wm.tracker.backend, self.wm.windows, self.plan, self._animated
        self._job = run_async(lambda cancel: run_reflow(job, backend, controller, plan, animated, cancel),
                              lambda outcome: self._done(scope, generation, outcome),
                              lambda error: self._failed(generation, error))

    def _failed(self, generation: int, error: str) -> None:
        if generation == self._generation:
            self._job = None
            log.warning("Dynamic tiling failed: %s", error)

    def _done(self, scope: str, generation: int,
              outcome: tuple[list[tuple[str, tuple[str, Rect]]], list[tuple[str, Result]],
                             list[tuple[str, Result]]]) -> None:
        if generation != self._generation or not self.in_charge() or scope != self._scope():
            return  # superseded, cancelled, or the scope changed meanwhile
        self._job = None
        targets, results, floated = outcome
        self._targets = dict(targets)
        rect_of = {}  # window key -> where it was meant to go
        for group in self._last_groups:
            for unit in group.units:
                if unit.id in self._targets:
                    for _slot, window in unit.members:
                        rect_of[window.key] = self._targets[unit.id][1]
        changed, problems, learned = False, [], False
        monitors = self._monitors()
        for slot, result in results:
            if result.status is Status.DENIED and result.key not in self._denied:
                self._denied.add(result.key)  # left out from now on: the others take its space
                self.schedule(force=True)
            if result.status is Status.CONSTRAINED and result.actual is not None and result.key in rect_of:
                # larger than its tile: its minimum size. The next layout makes room for it (or floats it).
                learned = self._learn(result.key, rect_of[result.key], result.actual, monitors) or learned
            if scope != GLOBAL and not slot.startswith(RULE_SLOT):
                message = "" if result.status is Status.PLACED else result.message
                changed = self.wm.set_entry_message(scope, slot, message) or changed
            if result.status not in (Status.PLACED, Status.GONE) and (result.key, result.status.value) \
                    not in self._reported:
                self._reported.add((result.key, result.status.value))
                if not result.ok:
                    problems.append(result.message)
        for group in self._last_groups:
            for unit, why in group.floating:
                window = unit.window
                if scope != GLOBAL and not unit.active.startswith(RULE_SLOT):
                    changed = self.wm.set_entry_message(scope, unit.active, f"{window.display_title}: {why}.") \
                        or changed
                if (window.key, why) not in self._reported:
                    self._reported.add((window.key, why))
                    problems.append(f"{window.display_title}: {why}.")
        for _slot, result in floated:
            if not result.ok and (result.key, result.status.value) not in self._reported:
                self._reported.add((result.key, result.status.value))
                problems.append(result.message)
        if learned:
            self.schedule()  # the minimums are part of the signature: one more round, with room for them
        if problems:
            self.wm.message.emit(" ".join(problems))
        if changed:
            self.wm.statuses_changed.emit(scope)
        self.reflowed.emit(scope)

    def stop(self) -> None:
        """CIRANA is closing: stop arranging. The windows stay exactly where they are; windows hidden in a stack
        are shown again."""
        self._stopped = True
        self._timer.stop()
        self._display_timer.stop()
        self._cancel()
        animation.snap_to_targets(self.wm.windows, self._animated)
        self._release_hidden()
