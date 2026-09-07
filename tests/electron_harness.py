"""Running a real-Chromium harness from pytest, once, correctly.

Three test modules drive Electron harnesses -- test_chat_ui_contract.py,
test_settings_v2.py and test_desktop_shell.py -- and each had grown its own
copy of the spawn logic. The copies had drifted, and every place they differed
was a defect:

  * ALL THREE looked for `electron.exe`. That file exists only on Windows, so
    on Linux the lookup fails, `pytest.skip` fires, and 51 assertions about the
    shipped UI report as "skipped" rather than "never ran here". This is the
    whole reason the Chromium suite was invisible in CI: not that CI could not
    run it, but that the path could not match on the CI platform.
  * Only test_chat_ui_contract.py decoded the child's output as UTF-8. The
    others take the locale codec, which is cp1252 on this machine, and these
    harnesses report Portuguese UI copy. One curly quote is enough to kill the
    reader thread inside subprocess, hand back `stdout=None`, and error every
    test in the module with "argument of type 'NoneType' is not iterable".
  * The timeouts were 600s, 300s and 180s for harnesses that all finish in
    under 35 seconds, and none of them cleaned up the process tree.

So it lives here once.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

ROOT = Path(__file__).resolve().parent.parent
ELECTRON_DIR = ROOT / "electron"
FRONTEND_OUT = ROOT / "frontend" / "out"

#: The Electron binary, named the way the platform names it. electron-builder
#: ships `dist/electron.exe` on Windows, `dist/electron` on Linux and macOS.
ELECTRON_BIN = (
    ELECTRON_DIR / "node_modules" / "electron" / "dist"
    / ("electron.exe" if sys.platform == "win32" else "electron")
)

#: How long a harness gets before the test kills it.
#:
#: Chosen from measurement, not from fear. The four harnesses were each timed
#: on a healthy workstation run:
#:
#:     render-check.js    21.9s   (3 runs, spread 0.06s)
#:     memory-render.js   24.2s   (3 runs, spread 0.06s)
#:     chat-drive.js      29.2s   (5 runs, spread 0.27s)
#:     settings-drive.js  32.1s   (3 runs, spread 0.08s)
#:
#: The spread within a harness is well under a second once Chromium's timer
#: throttling is disabled, so the distribution has no long tail to accommodate
#: -- a run either takes its usual half-minute or it is wedged. 240s is roughly
#: 7x the slowest healthy run, which leaves generous room for a CI runner with
#: software rendering under xvfb while still bounding a hang to four minutes
#: instead of the ten the old 600s allowed.
#:
#: Each harness also carries its OWN deadline (electron/test/lib/watchdog.js),
#: set lower, so the normal path is that the harness reports its last phase and
#: exits and this timeout never fires at all. This is the backstop for the case
#: where the harness is too wedged to report -- and it is deliberately the
#: second line of defence, because a report names the failure and a kill does
#: not.
HARNESS_TIMEOUT = float(os.environ.get("NANO_HARNESS_TIMEOUT", "240"))


def child_env() -> dict:
    """A clean environment for spawning Electron.

    ELECTRON_RUN_AS_NODE is exported by editors that are themselves Electron
    apps -- VS Code sets it -- and inheriting it makes the electron binary run
    as plain Node, so `require('electron')` returns the npm shim and the
    harness dies on `app.disableHardwareAcceleration()` with a confusing
    "Cannot read properties of undefined".
    """
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    return env


def _terminate_tree(pid: int) -> int:
    """Kill the process tree rooted at `pid`, and nothing else.

    Returns the number of processes that had to be killed, so a caller can
    report that cleanup was actually necessary.

    A harness is not one process: Electron spawns a renderer, a GPU process and
    one or more utility processes as children of the main one. Killing only the
    process we hold -- which is all `subprocess.run(timeout=...)` does -- can
    leave those behind, and an orphaned renderer holds the harness's HTTP port
    and its share of the machine's memory for as long as the session lasts.

    THE CHILDREN ARE ENUMERATED FIRST, BEFORE THE PARENT DIES. Once the parent
    is gone its children are reparented to init (or to nothing on Windows), and
    `children(recursive=True)` returns an empty list -- so a tree kill that
    terminates the parent first is exactly the orphan-maker it was meant to
    prevent.

    Scoped strictly to this tree. Nothing here matches on an image name, so a
    developer's own Electron applications -- including the editor running these
    tests -- are never candidates.
    """
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return 0

    try:
        family = parent.children(recursive=True)
    except psutil.Error:
        family = []
    family.append(parent)

    for proc in family:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(family, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=5)
    return len(family)


def survivors_of(pid: int) -> list[int]:
    """PIDs still alive in the tree rooted at `pid`. For proving cleanup."""
    living = []
    for proc in psutil.process_iter(["pid", "ppid"]):
        try:
            if proc.pid == pid or proc.ppid() == pid:
                living.append(proc.pid)
        except psutil.Error:
            pass
    return living


def require_harness() -> None:
    """Skip only for a reason that is genuinely about this machine.

    Unless NANO_REQUIRE_CHROMIUM is set, in which case a missing binary is a
    FAILURE. The job in CI that exists specifically to run these tests sets it,
    because "the Electron binary is not installed" is a perfectly reasonable
    thing for a laptop to say and a completely unacceptable thing for that job
    to say: it would go green having run nothing, which is the exact state this
    pass was opened to end.
    """
    required = os.environ.get("NANO_REQUIRE_CHROMIUM") == "1"

    missing = None
    if not ELECTRON_BIN.exists():
        missing = (f"the Electron binary is not installed at {ELECTRON_BIN} "
                   "(run `npm ci` in electron/)")
    elif not (FRONTEND_OUT / "index.html").exists():
        missing = "frontend/out is not built (run `npm run build` in frontend/)"

    if missing is None:
        return
    if required:
        raise AssertionError(
            f"NANO_REQUIRE_CHROMIUM=1 but {missing}. This job exists to run "
            "the real-Chromium tests, so skipping them is a failure.")
    pytest.skip(missing)


def run_harness(script: str, timeout: float | None = None) -> dict:
    """Run one harness and return its JSON report.

    Cleans up the Electron process tree on every exit path: success, a report
    that fails to parse, the timeout, an assertion raised by a caller, and
    KeyboardInterrupt. `subprocess.run` handles only the first and last of
    those, and only for the process it holds directly.
    """
    require_harness()
    timeout = HARNESS_TIMEOUT if timeout is None else timeout

    proc = subprocess.Popen(
        [str(ELECTRON_BIN), str(ELECTRON_DIR / "test" / script)],
        cwd=str(ELECTRON_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=child_env(),
        # DECODE AS UTF-8, EXPLICITLY. `text=True` alone decodes with the
        # locale codec, which on a Portuguese Windows install is cp1252 -- and
        # these harnesses report Portuguese UI copy. A single curly quote
        # (UTF-8 E2 80 9D) is enough: byte 0x9D is undefined in cp1252, the
        # reader thread dies inside subprocess, stdout comes back as None, and
        # every test in the module errors at fixture setup with a message that
        # says nothing about what broke.
        encoding="utf-8", errors="replace",
    )
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_tree(proc.pid)
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"{script} did not finish within {timeout:.0f}s and was killed. "
                f"A healthy run takes about 30s, so this is a hang, not slow "
                f"work.\nlast stderr:\n{(stderr or '')[-4000:]}"
            ) from None
    except BaseException:
        # Assertion, KeyboardInterrupt, anything: the tree does not outlive the
        # test that started it.
        _terminate_tree(proc.pid)
        raise

    _terminate_tree(proc.pid)

    if "{" not in (stdout or ""):
        raise AssertionError(
            f"{script} produced no report (exit {proc.returncode}):\n"
            f"{(stderr or '')[-4000:]}")
    report = json.loads(stdout[stdout.index("{"):])

    # The harness's own watchdog reports in the normal shape rather than dying
    # silently. Turn that into the failure it is, here, so the caller's
    # assertions do not run against a report with no measurements in it.
    if report.get("timedOut"):
        raise AssertionError(
            f"{script} hit its internal {report.get('timeoutMs', 0) / 1000:.0f}s "
            f"deadline while in phase {report.get('lastPhase')!r}.\n"
            f"last stderr:\n{(stderr or '')[-4000:]}")
    return report
