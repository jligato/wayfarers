"""Pytest collection policy for the server package.

Several files in this directory match pytest's ``test_*.py`` discovery pattern
but are NOT pytest test modules: they contain no test functions and instead run
their work as module-level statements at import time. They are standalone
durability/integration scripts that need a live PostgreSQL container
(``docker compose up``) or a live uvicorn server on a fixed port, and they print
an evidence line such as ``LEGACY_PG_DURABILITY {...}`` when they succeed.

pytest imports a matching file during collection, so merely running
``pytest server/`` executed these scripts at the wrong moment. They then failed
with misleading errors - ``assert 404 == 200``, ``{"detail":"Player not
found"}``, ``urllib.error.HTTPError: HTTP Error 500`` - that look like product
bugs but are really the script running outside its intended environment.

``collect_ignore`` keeps the filenames (docs and runner scripts reference them
by name, and they stay runnable as ``python server/<name>.py``) while telling
pytest not to import them. The scripts keep their own assertions unchanged; they
are executed deliberately, not skipped.

To run one deliberately, invoke it as a script, not through pytest:

    python server/check_legacy_persistence.py

server/test_lifecycle_persistence.py is deliberately NOT listed here. It is a
real pytest module with real test functions; it needs a real fix, not an
exclusion.

Cross-test state isolation
--------------------------
The third leak fixed here is a frozen clock.

``server/lifecycle.py`` does a plain ``import time`` at module level, so
``lifecycle.time`` IS the standard library ``time`` module -- verified by
running ``python -c "import lifecycle, time; print(lifecycle.time is time)"``,
which prints ``True``.

``server/test_lifecycle_route_guards.py`` then writes, inside its ``_make_env()``
helper::

    lifecycle.time.time = lambda: now[0]

Because ``lifecycle.time`` is the shared stdlib module and not a copy, that line
is not a local edit. It replaces ``time.time`` for the ENTIRE pytest process.
Every test file collected after it -- and every test function after it, in the
same file -- then reads a "current time" frozen at 2_000_000_000.0 forever.

The damage is not theoretical. ``server/sect.py`` mints ids with::

    sect_id = "sect_%d" % int(time.time() * 1000)

With the clock frozen, ``server/test_sect_war.py`` mints BOTH its attacker sect
and its defender sect inside the same frozen millisecond, so the second
``create_sect`` overwrites the first row in ``SectStore.sects`` and rebinds the
founder in ``SectStore.player_sect``. The result is
``data["sect"]["your_role"] is None`` and ``KeyError: 'war_id'`` -- 11 failures
in a file that passes 11/11 on its own.

Why a snapshot/restore fixture rather than editing the test
-----------------------------------------------------------
server/test_lifecycle_route_guards.py is owned by another agent and is not in
this task's write scope, so the repair has to live in a conftest. An earlier
repair in this file rebound the NAME ``lifecycle.time`` back to the module,
which does nothing: the damage is an attribute mutated ON that shared module
object, not the name pointing elsewhere. The only correct undo is to put the
original ``time.time`` function object back.

This fixture is additive and cannot weaken any assertion. It captures the
pristine ``time.time`` once, at conftest import, and restores it after every
test. A test is free to freeze the clock for its own duration -- that is what
``test_lifecycle_route_guards.py`` and ``test_lifecycle_deadlines.py`` both do
on purpose -- and the next test simply gets the real clock back.

The fourth leak fixed here is the database settings
---------------------------------------------------
The same shape, one level up. ``PlayerStore.__init__`` latches whether to use
PostgreSQL exactly once, at construction, from the five ``WC_PG_*`` settings.
Any module that sets one of them without cleaning up flips every LATER test
onto a database-backed store, and the resulting failures appear in files that
have nothing to do with the module that caused them.

``server/tests/stamina_e2e.py`` did exactly that with a bare
``os.environ.setdefault()`` at module level. It was fixed at the source -- the
settings now live inside a ``main()`` that only runs when the file is executed
as a script, so importing it is inert. That fix is necessary but not sufficient:
the filename does not match pytest's ``test_*.py`` discovery pattern, so
pytest never imports it, and a guard that only covers a file nobody collects
would be theatre. This conftest clears the five settings around every test so
the NEXT module that repeats the mistake is contained, and the failure lands on
that module instead of on innocent bystanders.

The values seen at conftest import are preserved: a developer who runs the real
PostgreSQL suite with ``WC_PG_HOST`` on the command line keeps it. Only settings
that were absent at the start are ever removed.
"""

# Standard library modules that a test may monkeypatch in place. The value is
# captured at import time, before any test function has run, so it is pristine.
import os as _os
import time as _time

_REAL_TIME_TIME = _time.time

# The five settings that switch the app between the in-memory player store and
# a real PostgreSQL database. The player store latches this decision ONCE, when
# the store object is built, and keeps it for the life of the process.
_PG_VARS = ("WC_PG_HOST", "WC_PG_PORT", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD")

# What the environment looked like before any test module was imported. A value
# that is None here means the developer did not ask for a database run, so any
# appearance of it later is a leak and must be undone. A value that is already
# set here is the developer's deliberate choice and is never touched.
_PG_AT_START = {k: _os.environ.get(k) for k in _PG_VARS}


def _clear_pg_env():
    """Remove only the database settings that were not deliberately set.

    Restoring the developer's own value matters: someone running the real
    PostgreSQL suite with WC_PG_HOST on the command line must still get it.
    """
    for k in _PG_VARS:
        if _PG_AT_START[k] is None:
            _os.environ.pop(k, None)


# These were previously listed here under their old ``test_*.py`` names.
# They have been renamed to ``check_*.py``, which no longer matches
# pytest's discovery pattern at all, so no ignore entry is needed for
# them any more:
#
#   check_artifacts_persistence.py    check_pvp_persistence.py
#   check_equipment_persistence.py    check_restart_durability.py
#   check_ghost_battle_persistence.py
#   check_inner_skills_persistence.py
#   check_legacy_persistence.py
#
# The duplicate ``test_integration.py`` was deleted outright: its bytes
# were identical to ``server/integration_script.py``, which the contract
# test ``server/tests/test_integration_script_contract.py`` already covers.
collect_ignore: list[str] = []


def pytest_runtest_setup(item):
    """Guarantee every test starts from the real wall clock and a clean env.

    Runs before the test's own fixtures, so a test that deliberately freezes
    the clock still sees its own frozen value for the whole of its body, and a
    test that sets a database setting on purpose still sees its own value.
    """
    if _time.time is not _REAL_TIME_TIME:
        _time.time = _REAL_TIME_TIME
    _clear_pg_env()


def pytest_runtest_teardown(item, nextitem):
    """Hand the real clock and a clean environment back to the next test."""
    if _time.time is not _REAL_TIME_TIME:
        _time.time = _REAL_TIME_TIME
    _clear_pg_env()


# ===========================================================================
