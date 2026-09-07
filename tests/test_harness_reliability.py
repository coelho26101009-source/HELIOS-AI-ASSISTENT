"""The test infrastructure, tested.

These are tests about tests, which is usually a smell. They are here because
each one corresponds to a defect that actually shipped in this repository's
test suite and that no product test could have caught:

  * the real-Chromium tests could not run on Linux, so CI reported 53 skips as
    a pass;
  * a wedged harness produced no output at all and burned the full 600s
    subprocess timeout before anyone learned anything;
  * killing a harness killed the process we held and left its renderer, GPU
    and utility children behind.

A guard nobody has watched fail is not a guard, so each of these reintroduces
the condition rather than asserting that the code looks right.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import psutil
import pytest

from tests.electron_harness import (
    ELECTRON_BIN,
    ELECTRON_DIR,
    HARNESS_TIMEOUT,
    _terminate_tree,
    child_env,
    require_harness,
    run_harness,
    survivors_of,
)

# Only the tests that actually SPAWN Electron are marked; the two above them
# are questions about this repository's own configuration and must be answered
# on every runner, including the ones with no Electron at all.
spawns_electron = pytest.mark.chromium


# ======================================================= the binary is findable


def test_the_electron_binary_is_looked_for_under_its_platform_name():
    """The whole reason 53 UI tests never ran outside one Windows machine.

    Every module hardcoded `electron.exe`. That path cannot exist on Linux, so
    the lookup missed, the tests skipped, and the skip read as a pass in a job
    that was never going to run them.
    """
    expected = "electron.exe" if sys.platform == "win32" else "electron"
    assert ELECTRON_BIN.name == expected, (
        f"on {sys.platform} the harness looks for {ELECTRON_BIN.name}, "
        f"which cannot exist here")


def test_the_timeout_is_bounded_by_measurement_not_by_a_round_number():
    """240s against a slowest measured healthy run of 32.1s.

    The old values were 600s, 300s and 180s in three modules for harnesses
    that all finish in about half a minute. A ten-minute ceiling on a
    thirty-second job is not patience, it is ten minutes of a CI runner
    learning nothing.
    """
    assert 120 <= HARNESS_TIMEOUT <= 400, (
        f"{HARNESS_TIMEOUT}s is not in the range the measurements support")


# ==================================================== the process tree is reaped


def _spawn_harness() -> subprocess.Popen:
    """Start a harness and wait until it has really built its process tree."""
    require_harness()
    proc = subprocess.Popen(
        [str(ELECTRON_BIN), str(ELECTRON_DIR / "test" / "chat-drive.js")],
        cwd=str(ELECTRON_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=child_env(), encoding="utf-8", errors="replace",
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if len(survivors_of(proc.pid)) > 1:      # the parent plus at least one child
            return proc
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    _terminate_tree(proc.pid)
    pytest.fail("the harness never spawned a renderer to clean up")


@spawns_electron
def test_killing_a_harness_takes_its_renderer_and_gpu_children_with_it():
    """Electron is not one process, and `subprocess.kill()` only knows one.

    Reproduces the orphan condition directly: start a real harness, wait until
    Chromium has forked its renderer, GPU and utility processes, then reap the
    tree and demand that none of them survived.
    """
    proc = _spawn_harness()
    tree = survivors_of(proc.pid)
    assert len(tree) > 1, "expected a multi-process Chromium tree to reap"

    _terminate_tree(proc.pid)
    proc.communicate()

    deadline = time.time() + 15
    while time.time() < deadline:
        alive = [pid for pid in tree if psutil.pid_exists(pid)]
        if not alive:
            break
        time.sleep(0.2)

    still_alive = []
    for pid in tree:
        try:
            still_alive.append((pid, psutil.Process(pid).name()))
        except psutil.Error:
            pass
    assert not still_alive, f"orphaned after the tree kill: {still_alive}"


@spawns_electron
def test_a_harness_that_never_answers_fails_the_test_instead_of_hanging():
    """The 600s wait, reproduced and bounded.

    A one-second timeout against a harness that takes about thirty guarantees
    the timeout path is the one taken. It must raise, name the harness, and
    leave nothing running -- not sit there.
    """
    started = time.time()
    with pytest.raises(AssertionError, match="did not finish within"):
        run_harness("chat-drive.js", timeout=1.0)
    elapsed = time.time() - started

    assert elapsed < 60, (
        f"the timeout took {elapsed:.0f}s to give up on a 1s deadline")


# ============================================== the harness reports its own hang


@spawns_electron
def test_a_wedged_harness_reports_its_last_phase_rather_than_dying_silently():
    """The watchdog, proved by making it fire.

    NANO_WATCHDOG_MS shortens the harness's own deadline so it cannot finish.
    It must still emit the normal JSON shape -- naming the phase it was stuck
    in -- and run_harness must turn that into a named failure, because "no
    report" tells a reader nothing about where the run stopped.
    """
    # Set on THIS process: child_env() copies os.environ into the child.
    os.environ["NANO_WATCHDOG_MS"] = "2000"
    try:
        with pytest.raises(AssertionError, match="internal .* deadline while in phase"):
            run_harness("chat-drive.js")
    finally:
        os.environ.pop("NANO_WATCHDOG_MS", None)
