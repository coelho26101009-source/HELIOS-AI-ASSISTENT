"""``core.pc_control.windows.close`` -- the verification mechanism, deterministic.

WHY THIS FILE EXISTS
---------------------
``tests/test_pc_control.py::test_the_full_calculator_lifecycle_through_the_executor``
drives a REAL window on a REAL desktop, and that is exactly right for proving
the tool works end to end -- but it cannot prove anything about TIMING on
purpose, because the timing on any one run depends on a live OS and a live
GPU compositor that this test suite does not control. Two Windows CI runs
failed at that test's final assertion, both recovered on an unchanged rerun,
and every step before close -- including minimize and restore, which exercise
the identical handle -- passed every time. That pattern is a slow, genuine
close racing a fixed verification deadline, not a broken close: see the
comment above ``_CLOSE_OBSERVE_SECONDS`` in ``core/pc_control/windows.py`` for
the full mechanism (a packaged/UWP window's close crosses a process boundary
and, on a GPU-less CI host, that crossing is measurably slower).

So the mechanism is proved HERE instead, against a scripted Win32 layer with a
fake clock: no real window, no real sleep, and a result that is the same on
every run regardless of what machine executes it. ``winapi.post_close``,
``winapi.is_window``, ``winapi.window_pid`` and ``time.monotonic``/``time.sleep``
are the only things replaced; ``close()`` itself is the real, unmodified
production function.
"""
from __future__ import annotations

import pytest

from core.pc_control import windows
from core.pc_control.results import PCControlError

ORIGINAL_PID = 4242
OTHER_PID = 9999
POLL = windows._CLOSE_POLL_SECONDS
DEADLINE = windows._CLOSE_OBSERVE_SECONDS


class FakeClock:
    """A monotonic clock that only moves when the code under test sleeps.

    No test in this file waits in real time, including the one that proves the
    genuine-refusal case runs out its full deadline: advancing a fake clock is
    what makes that assertion fast AND exact, rather than merely fast because
    the wait was shortened.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ScriptedWindow:
    """A Win32 layer that answers ``is_window``/``window_pid`` from a script.

    Each sequence is indexed by call count and holds its last value once
    exhausted, so a short script ("alive, alive, gone") reads naturally as
    "and then stays gone" without repeating the tail by hand.
    """

    def __init__(self, *, alive: list[bool], pid: list[int], title: str = "Alvo",
                post_close_ok: bool = True) -> None:
        self.alive_script = alive
        self.pid_script = pid
        self.title = title
        self.post_close_ok = post_close_ok
        self.alive_calls = 0
        self.pid_calls = 0
        self.post_close_calls = 0

    def is_window(self, _hwnd: int) -> bool:
        index = min(self.alive_calls, len(self.alive_script) - 1)
        self.alive_calls += 1
        return self.alive_script[index]

    def window_pid(self, _hwnd: int) -> int:
        index = min(self.pid_calls, len(self.pid_script) - 1)
        self.pid_calls += 1
        return self.pid_script[index]

    def window_title(self, _hwnd: int) -> str:
        return self.title

    def post_close(self, _hwnd: int) -> bool:
        self.post_close_calls += 1
        return self.post_close_ok


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(windows.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(windows.time, "sleep", fake.sleep)
    return fake


def _wire(monkeypatch, script: ScriptedWindow) -> None:
    monkeypatch.setattr(windows.winapi, "is_window", script.is_window)
    monkeypatch.setattr(windows.winapi, "window_pid", script.window_pid)
    monkeypatch.setattr(windows.winapi, "window_title", script.window_title)
    monkeypatch.setattr(windows.winapi, "post_close", script.post_close)


# =========================================================== 1. immediate close


def test_a_target_that_disappears_immediately_is_closed(monkeypatch, clock):
    script = ScriptedWindow(alive=[False], pid=[ORIGINAL_PID])
    _wire(monkeypatch, script)

    result = windows.close(555)

    assert result == {"closed": True, "title": "Alvo"}
    assert script.post_close_calls == 1
    # Not one tick of the deadline was spent: the fast path costs nothing.
    assert clock.now == 0.0


# =============================================== 2. closes after a few polls


def test_a_target_that_disappears_after_a_few_polls_is_closed(monkeypatch, clock):
    script = ScriptedWindow(alive=[True, True, True, False], pid=[ORIGINAL_PID])
    _wire(monkeypatch, script)

    result = windows.close(555)

    assert result["closed"] is True
    # Three poll intervals were spent waiting, and not the full deadline.
    assert clock.now == pytest.approx(3 * POLL)
    assert clock.now < DEADLINE


# ===================================================== 3. genuine refusal


def test_a_target_that_never_disappears_is_reported_as_refused_not_crashed(monkeypatch, clock):
    """The negative case this whole mechanism exists to still get right: a real
    "do you want to save?" dialog must still be reported honestly, not papered
    over by the wider deadline."""
    script = ScriptedWindow(alive=[True], pid=[ORIGINAL_PID])
    _wire(monkeypatch, script)

    result = windows.close(555)

    assert result["closed"] is False
    assert "detail" in result and result["detail"]
    assert "fechou" in result["detail"] or "guardar" in result["detail"]
    # It waited the WHOLE deadline, not a moment more and not a moment less.
    assert clock.now == pytest.approx(DEADLINE)


# ================================================== 4. recycled-handle safety


def test_a_recycled_handle_is_treated_as_the_window_being_gone(monkeypatch, clock):
    """The mechanism Phase E asked for, proved directly.

    ``is_window`` says the handle is alive for every poll -- exactly what a
    brand-new, unrelated window landing on the same recycled integer would
    look like. The owning process id flips partway through, and that is the
    only signal available to tell the two situations apart. It must be
    believed, and it must be caught quickly rather than waiting out the whole
    deadline meant for a real refusal.
    """
    script = ScriptedWindow(alive=[True, True, True, True],
                            pid=[ORIGINAL_PID, ORIGINAL_PID, ORIGINAL_PID, OTHER_PID])
    _wire(monkeypatch, script)

    result = windows.close(555)

    assert result["closed"] is True
    # window_pid call #1 is the owner capture before the loop (ORIGINAL_PID);
    # two polls then still match ORIGINAL_PID (one sleep each), and the third
    # poll reads OTHER_PID and returns immediately -- two poll intervals
    # spent, nowhere near the full deadline a genuine refusal would consume.
    assert clock.now == pytest.approx(2 * POLL)
    assert clock.now < DEADLINE / 2


def test_the_recycled_handle_guard_does_not_fire_on_the_real_windows_pid(monkeypatch, clock):
    """Non-vacuity, the other direction: the SAME pid throughout must never be
    misread as a recycled handle. Without this, the guard above could pass by
    always returning "gone" regardless of pid."""
    script = ScriptedWindow(alive=[True, True, True], pid=[ORIGINAL_PID])
    _wire(monkeypatch, script)

    result = windows.close(555)

    # Same window, same owner the whole time: only IsWindow flipping to False
    # (never scripted here) may report it closed, so this must run out the
    # deadline exactly like the plain refusal case.
    assert result["closed"] is False
    assert clock.now == pytest.approx(DEADLINE)


def test_an_unreadable_owner_pid_falls_back_to_the_handle_check_alone(monkeypatch, clock):
    """If the owning process cannot be determined (``window_pid`` raises),
    the original, simpler behaviour must still hold: trust ``IsWindow``."""
    calls = {"n": 0}

    def flaky_pid(_hwnd: int) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return ORIGINAL_PID  # the owner capture in close(), before post_close
        raise OSError("simulated failure reading the owning process")

    monkeypatch.setattr(windows.winapi, "window_pid", flaky_pid)
    monkeypatch.setattr(windows.winapi, "is_window", lambda _h: False)
    monkeypatch.setattr(windows.winapi, "window_title", lambda _h: "Alvo")
    monkeypatch.setattr(windows.winapi, "post_close", lambda _h: True)

    result = windows.close(555)
    assert result["closed"] is True  # IsWindow alone was enough to decide


def test_the_deadline_has_real_headroom_over_the_value_that_flaked_in_ci():
    """Pins the specific number, not only the mechanism around it.

    The other tests in this file would pass unchanged if this constant were
    quietly reverted to 1.5 -- they assert the polling is correct FOR WHATEVER
    the deadline is, which is the right property to hold in general but is not
    by itself a regression guard for the one number two real CI runs proved
    was too tight. This is that guard.
    """
    assert windows._CLOSE_OBSERVE_SECONDS > 1.5


# ============================================== 5. bounded, not unbounded


def test_verification_never_exceeds_the_stated_deadline(monkeypatch, clock):
    """A refusal that never resolves must still return -- this is what makes a
    generous deadline safe rather than a hazard: it is generous, not open."""
    script = ScriptedWindow(alive=[True], pid=[ORIGINAL_PID])
    _wire(monkeypatch, script)

    windows.close(555)

    assert clock.now <= DEADLINE + POLL
    # And the poll count is exactly what the deadline and interval predict --
    # not "eventually gives up", a specific, checkable number.
    assert script.alive_calls == pytest.approx(DEADLINE / POLL, abs=1)


# ==================================================== 6. failure stays visible


def test_a_refused_close_is_visible_through_the_full_tool_call(monkeypatch, clock):
    """Phase F: the negative result must not be swallowed anywhere between the
    Win32 layer and what the model/user is shown. Exercised through the real
    plugin handler and ToolExecutor, not only the bare function."""
    from core import plugin_loader
    from core.permission_manager import PermissionManager
    from core.tool_execution import ToolExecutor

    script = ScriptedWindow(alive=[True], pid=[ORIGINAL_PID], title="Bloco de Notas")
    _wire(monkeypatch, script)
    monkeypatch.setattr(
        windows, "resolve_window",
        lambda **_kw: {"window_id": 555, "title": "Bloco de Notas",
                       "process": "notepad.exe", "visible": True,
                       "state": "normal", "focused": False})

    plugin_loader.load_all_plugins()
    manager = PermissionManager(confirmation_callback=lambda *_a: True)
    executor = ToolExecutor(manager)
    executor.register_plugin_tools()

    result = executor.execute_tool("pc_window_close", {"window_id": 555})

    assert result["success"] is False
    assert result["output"]["status"] == "refused"
    assert result["output"]["error"] == "refused"
    # The refusal detail is a fixed, generic sentence (deliberately -- see
    # `windows.close`'s docstring); WHICH window it refers to travels in the
    # structured `window` field instead, and that must still be there.
    assert result["output"]["window"]["title"] == "Bloco de Notas"


# ===================================== 7. target binding survives the change


def test_close_still_binds_to_the_exact_hwnd_it_is_given(monkeypatch, clock):
    """The verification change must not have loosened WHICH window is acted
    on. ``windows.close`` receives the caller's hwnd and nothing else -- it is
    never re-resolved by title midway through, which is what would let a
    same-titled but different window be closed instead."""
    seen_hwnds: list[int] = []

    def recording_is_window(hwnd: int) -> bool:
        seen_hwnds.append(hwnd)
        return False

    monkeypatch.setattr(windows.winapi, "is_window", recording_is_window)
    monkeypatch.setattr(windows.winapi, "window_pid", lambda _h: ORIGINAL_PID)
    monkeypatch.setattr(windows.winapi, "window_title", lambda _h: "Alvo")
    monkeypatch.setattr(windows.winapi, "post_close", lambda _h: True)

    windows.close(778899)

    assert seen_hwnds == [778899]


def test_the_destructive_close_tool_still_refuses_a_partial_title_match(monkeypatch):
    """The permission-relevant half of target binding: ``pc_window_close``
    resolves through ``allow_partial=False`` in ``plugins/pc_control.py``, and
    this pass touched none of that. Proved behaviourally, through the real
    handler, by making the underlying resolver visibly enforce the flag it is
    called with rather than by reading the source for the literal."""
    import plugins.pc_control as pc_control_plugin

    seen: dict = {}

    def recording_resolve(*, window_id=None, query=None, allow_partial=True):
        seen["allow_partial"] = allow_partial
        raise PCControlError("not_found", "sem correspondência única")

    monkeypatch.setattr(windows, "resolve_window", recording_resolve)

    result = pc_control_plugin.pc_window_close({"query": "Notas"})

    assert seen["allow_partial"] is False
    # Called directly on the handler (bypassing ToolExecutor), so the result
    # is the raw `core.pc_control.results.fail(...)` shape: `ok`, not
    # `success` -- `ToolExecutor` is what adds the top-level `success` key.
    assert result["ok"] is False
    assert result["status"] == "not_found"
