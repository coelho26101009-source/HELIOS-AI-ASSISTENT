"""Shared pytest configuration.

ORDER INDEPENDENCE
------------------
core/main.py builds its world at import time -- `brain`, `memory`,
`memory_stack`, `tool_executor`, `permission_manager` and a dozen more are
module-level singletons, and every test in this suite shares the one instance.
A test that leaves one of them altered does not fail; it makes some LATER,
unrelated test fail, and which test that is depends on collection order. That
failure mode is invisible in a suite that only ever runs in one order, which is
what this suite did.

So the order is made changeable, and reproducible:

    pytest --shuffle                 shuffle with a fresh random seed
    pytest --shuffle-seed=12345      shuffle exactly the way seed 12345 does
    pytest                           source order, unchanged

The seed is always printed, and printed again in the failure header, so a
shuffled failure can be replayed exactly rather than described as "it sometimes
fails". Shuffling is OFF by default: a suite that reorders itself on every run
turns one real defect into an intermittent one, and CI pins a seed instead (see
.github/workflows/ci.yml) so the order it exercises is a different one from the
source order but is the same on every run.

This is deliberately eleven lines of hook rather than a dependency on
pytest-randomly. That plugin also reseeds `random` and `numpy` before every
test, which changes what the tests under it actually do -- it would be adding a
second, uncontrolled variable to the very experiment being run here -- and it
would have to be installed in CI to keep the suite reproducible there.
"""
from __future__ import annotations

import random


def pytest_addoption(parser):
    group = parser.getgroup("order")
    group.addoption("--shuffle", action="store_true", default=False,
                    help="shuffle test order to expose order dependencies")
    group.addoption("--shuffle-seed", action="store", type=int, default=None,
                    help="shuffle with this exact seed (implies --shuffle)")


def pytest_collection_modifyitems(session, config, items):
    seed = config.getoption("shuffle_seed")
    if seed is None and not config.getoption("shuffle"):
        return
    if seed is None:
        seed = random.randrange(1 << 31)
    random.Random(seed).shuffle(items)
    config._nano_shuffle_seed = seed


def pytest_report_header(config):
    seed = getattr(config, "_nano_shuffle_seed", None)
    if seed is None:
        return None
    return f"test order shuffled with --shuffle-seed={seed}"


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Say the seed again at the END, where a failing run is actually read."""
    seed = getattr(config, "_nano_shuffle_seed", None)
    if seed is not None:
        terminalreporter.write_line(
            f"test order was shuffled; reproduce with --shuffle-seed={seed}")


# --------------------------------------------------------------------------
#  The real-Chromium tests, addressable as a set
# --------------------------------------------------------------------------
#: Fixtures that spawn Electron and drive the shipped bundle in real Chromium.
#: A test is a Chromium test if it asks for one of these -- which is a fact
#: about the test, not a label someone has to remember to keep in sync.
_CHROMIUM_FIXTURES = {"chat_report", "graph_report", "drive_report", "render_report"}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "chromium: drives the shipped bundle in real Chromium via an Electron "
        "harness; needs electron/node_modules and a built frontend/out")


def pytest_itemcollected(item):
    """Mark the Chromium tests by the fixtures they request.

    Applied here rather than written onto ~51 test functions by hand, so the
    marker cannot drift away from the thing it describes: ask for the harness
    and you are a Chromium test, in every module, forever.
    """
    if _CHROMIUM_FIXTURES.intersection(getattr(item, "fixturenames", ())):
        item.add_marker("chromium")
