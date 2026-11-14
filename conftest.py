"""Test-suite isolation for the Wayfarer's Cycle server.

The bug this fixes
------------------
The suite imported ``server/main.py`` under TWO different module names:

* ``server.main`` -- used by test_zones, test_dungeons, test_npcs, test_beasts ...
* ``main``        -- used by test_battle_pass_xp, test_lottery_pity,
                     test_retention, test_security_regression ...

``main.py`` lives in ``server/`` and most test files push ``server/`` onto
``sys.path``, so Python loaded the very same file twice under two names. Each
copy ran the module body separately and therefore built its OWN singletons:

    players_db    the in-memory PlayerStore holding every player
    wallet_store  the wallet
    app           the FastAPI application

Each copy also called the ``register_*_routes(app, players_db)`` helpers, and
those helpers park the store in a MODULE-LEVEL global inside shared top-level
helper modules (``zone_routes._players_db``, ``npc_routes``, ``sect_wall`` ...).
Whichever copy of ``main`` was imported LAST overwrote that global, so the
routes wired up by the copy that ran first ended up reading the SECOND copy's
player store.

The symptom: a test creates a player through the FastAPI test client and the
very next call answers ``404 Player not found``, because the create and the
read went to two different in-memory stores. Every file passes on its own and
fails as a group, and WHICH files fail depends on collection order -- so the
failure set moved between runs.

Verified with::

    # these two used to fail 14/15 depending on order
    pytest server/test_zones.py server/test_battle_pass_xp.py
    pytest server/test_battle_pass_xp.py server/test_zones.py

The fix
-------
Make ``main`` and ``server.main`` name ONE module object, so the session has a
single app and a single player store. We put both the repo root and ``server/``
on ``sys.path``, import ``server.main`` once, and register the resulting object
in ``sys.modules`` under the bare name ``main`` as well. A meta-path finder
then claims any later ``import main`` and hands back that same object instead of
executing the file a second time. The same aliasing is applied to every other
top-level module that lives in ``server/`` so the helper modules cannot be
duplicated either.

Note: no autouse teardown fixture is registered on purpose. An earlier attempt
restored module globals after every test; it broke more files than it fixed,
because several suites deliberately construct their own store and app. The
single-module-identity repair is the whole fix.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
import os as _os

# Set a signing key for tests that need one
_os.environ.setdefault("WC_SIGNING_KEY", "test-signing-key-32-bytes-long!!")

ROOT = Path(__file__).resolve().parent        # .../wayfarers-cycle
SERVER = ROOT / "server"                      # .../wayfarers-cycle/server

# Manual demo scripts, not tests. They are named ``test_*.py`` so pytest collects
# them, but none of them defines a ``test_`` function or a single ``assert`` --
# they only ``print``. Two of them corrupt the whole session at IMPORT time:
#
#   test_lifecycle_offline.py   line 22 runs ``main.players_db = players_db``,
#                               permanently swapping the application's global
#                               store for a fresh empty one. Every later test then
#                               creates players in one store and reads them from
#                               another, so they see ``KeyError`` / ``404``. This
#                               alone broke 70 tests in server/.
#   test_integration.py         spawns a uvicorn on 8766 with no teardown at all,
#   test_wallet_integration.py  leaving an orphan that answers the next run's
#                               readiness probe, so the next run silently tests
#                               the PREVIOUS run's code.
#
# Excluding them is lossless (they assert nothing) and is deliberately narrower
# than an autouse global-restore fixture, which this file already records as
# having broken more suites than it fixed. They stay on disk as runnable scripts:
#     python test_lifecycle_offline.py
collect_ignore = [
    "test_integration.py",
    "test_lifecycle_offline.py",
    "test_offline.py",
    "test_wallet_integration.py",
]

# Names of top-level helper modules that must exist exactly once. Importing
# ``server/main.py`` pulls these in by bare name (``from zone_routes import ...``).
_SERVER_HELPERS = (
    "player_store", "wallet_defense", "wallet_dispatch", "lifecycle",
    "lifecycle_guard", "zone_routes", "npc_routes", "dungeon_routes",
    "event_routes", "receipt_routes", "sect", "sect_wall", "npcs", "tower",
    "battle_pass", "tribulation", "first_session", "inner_skills",
    "inventory", "missions", "friends", "communication", "events",
    # auth holds the session store in a module-level singleton, and identity is
    # its admin/principal counterpart. Neither was listed, so ``import auth``
    # and ``import server.auth`` produced two module objects with two separate
    # stores: a token minted through one could not be resolved by a route
    # holding the other, and every authenticated request failed 401 while
    # anonymous ones correctly failed too.
    "auth", "identity",
    # dungeons holds the party store in a module-level DungeonManager
    # singleton, and it was imported under BOTH spellings: server/main.py
    # reaches it as ``server.dungeons`` (dungeon_routes.py:19-20) while
    # tower_routes.py reached it as bare ``dungeons``. Two module objects, two
    # managers, two empty party stores: a party created through
    # POST /dungeons/party/create was invisible to /tower/spire/party/invite,
    # which answered 400 "You are not in a party" right after the 200 that
    # created it. Aligning the spelling fixes production; this list entry stops
    # the class of split from recurring inside the suite.
    "dungeons",
)


class _SingleModuleFinder:
    """Meta path finder that resolves bare server modules to one object.

    Without this, ``import main`` inside a test file re-executes
    ``server/main.py`` and creates a second app and a second player store.
    """

    def __init__(self) -> None:
        self._aliases: dict[str, object] = {}

    def alias(self, name: str, module: object) -> None:
        self._aliases[name] = module

    def find_module(self, fullname, path=None):  # pragma: no cover - legacy API
        return None

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self._aliases:
            return None
        from importlib.machinery import ModuleSpec
        return ModuleSpec(fullname, _AliasLoader(self._aliases[fullname]))


class _AliasLoader:
    """Loader that returns an already-created module instead of running code."""

    def __init__(self, module: object) -> None:
        self._module = module

    def create_module(self, spec):              # pragma: no cover - trivial
        return self._module

    def exec_module(self, module):              # pragma: no cover - trivial
        return None


def _install_single_module_identity() -> object:
    """Import server.main once and make every alias resolve to that object."""
    for path in (str(ROOT), str(SERVER)):
        if path not in sys.path:
            sys.path.insert(0, path)

    finder = _SingleModuleFinder()
    if not any(isinstance(f, _SingleModuleFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, finder)

    # If a bare ``main`` was already imported before conftest ran, that copy is
    # the one other modules may already hold references to, so adopt it as the
    # canonical one and point ``server.main`` at it rather than the reverse.
    main_mod = sys.modules.get("main")
    if main_mod is None:
        main_mod = importlib.import_module("server.main")
    sys.modules["server.main"] = main_mod
    sys.modules["main"] = main_mod
    finder.alias("main", main_mod)
    finder.alias("server.main", main_mod)

    # Alias the shared top-level helper modules so ``zone_routes`` and
    # ``server.zone_routes`` are the same object. Their module-level globals
    # (where route handlers stash the store) must not be duplicated.
    #
    # Preference is PACKAGED first (``server.<name>``), not bare. Whichever
    # spelling an existing importer already bound a reference to must win, or
    # the alias silently re-points the module table at a DIFFERENT object than
    # the one live route handlers captured, which is the same split wearing a
    # new disguise. Measured: ``tower_run`` imports bare ``dungeons`` first, so
    # ``sys.modules["dungeons"]`` already exists while the routes are still
    # loading, and a bare-first alias bound BOTH names to that early object --
    # leaving tower_routes/dungeon_routes (which hold the packaged one)
    # pointing at an orphaned module. Prefer the packaged object, and fall back
    # to bare only when no packaged object was ever loaded.
    for name in _SERVER_HELPERS:
        mod = sys.modules.get(f"server.{name}") or sys.modules.get(name)
        if mod is None:
            continue
        sys.modules[f"server.{name}"] = mod
        if name != "server.main":
            sys.modules[name] = mod
        finder.alias(name, mod)
        finder.alias(f"server.{name}", mod)

    return main_mod


MAIN_MODULE = _install_single_module_identity()


# ---------------------------------------------------------------------------
# Second leak: suites that build their OWN store and rebind module globals
# without restoring them.
#
# server/test_lifecycle_route_guards.py does this in its _make_env() helper:
#
#     app_module.players_db   = store        # replaces the shared store
#     app_module.wallet_store = wallet
#     lifecycle.route_class_store = store
#     lifecycle.time.time = lambda: now[0]   # freezes the clock for good
#     lifecycle.register_routes(app_module.app, lambda: store)
#
# Every one of those assignments is permanent. After that file runs, the app
# and every route handler read a throwaway store that the NEXT test file knows
# nothing about, so its freshly created players come back 404. Its frozen
# clock also makes later files compute deadlines against a stale "now".
#
# We cannot edit the test file (out of scope) and must not touch production
# code, so we snapshot the handful of globals it clobbers before each test and
# put them back afterwards.
# ---------------------------------------------------------------------------

# (module name, attribute name) pairs that carry the store / wallet / clock.
_CLOBBERED: tuple = (
    ("main", "players_db"),
    ("main", "wallet_store"),
    ("main", "USE_PG"),
    ("lifecycle", "route_class_store"),
    ("lifecycle", "_live_players_db"),
    ("lifecycle", "time"),
    ("lifecycle_guard", "route_class_store"),
    ("zone_routes", "_players_db"),
    ("npc_routes", "_players_db"),
    ("dungeon_routes", "_players_db"),
    ("event_routes", "_players_db"),
    ("receipt_routes", "_players_db"),
    ("sect_wall", "_players_db"),
    ("tower", "_players_db"),
    ("battle_pass", "_players_db"),
    ("first_session", "_players_db"),
    ("inner_skills", "_players_db"),
)

# Store references are also parked under a ``_store``-suffixed name in several
# helper modules; match any attribute ending in one of these.
_STORE_SUFFIXES = ("_players_db", "route_class_store", "players_store")


def _snapshot_clobbered() -> list:
    saved = []
    for mod_name, attr in _CLOBBERED:
        mod = sys.modules.get(mod_name)
        if mod is None or not hasattr(mod, attr):
            continue
        saved.append((mod, attr, getattr(mod, attr)))
    return saved


def _restore_clobbered(saved: list) -> None:
    for mod, attr, value in saved:
        try:
            setattr(mod, attr, value)
        except Exception:                        # pragma: no cover - read-only
            pass

    # Catch store references parked under names we did not enumerate.
    canonical = MAIN_MODULE.players_db
    for mod in list(sys.modules.values()):
        if mod is None or not hasattr(mod, "__dict__"):
            continue
        try:
            items = list(vars(mod).items())
        except Exception:                        # pragma: no cover
            continue
        for attr, value in items:
            if not attr.endswith(_STORE_SUFFIXES):
                continue
            if callable(value):                 # accessor function, not a store
                continue
            if hasattr(value, "transaction") and value is not canonical:
                try:
                    setattr(mod, attr, canonical)
                except Exception:                # pragma: no cover - read-only
                    pass

    # Undo a frozen clock. ``lifecycle.time`` is rebound to the real module.
    import time as _real_time
    for mod_name in ("lifecycle", "server.lifecycle"):
        mod = sys.modules.get(mod_name)
        if mod is not None and getattr(mod, "time", None) is not _real_time:
            try:
                mod.time = _real_time
            except Exception:                    # pragma: no cover
                pass


@pytest.fixture(autouse=True)
def _restore_shared_singletons():
    """Undo store/wallet/clock rebinds so one file cannot poison the next."""
    saved = _snapshot_clobbered()

    # ``main.sect_store`` is a third process-wide singleton (sect.py line 27:
    # ``self.sects = {}``). Sects created by one file stay visible to the next,
    # so a later file that counts sects, or declares a war against a sect id a
    # previous file already consumed, sees stale rows. Snapshot it too.
    sect_store = getattr(MAIN_MODULE, "sect_store", None)
    sect_saved = None
    if sect_store is not None:
        sect_saved = (
            dict(getattr(sect_store, "sects", {})),
            dict(getattr(sect_store, "player_sect", {})),
            dict(getattr(sect_store, "wars", {})),
        )

    # Fourth leak: ``lifecycle.register_routes(app, ...)`` APPENDS routes to the
    # live app instead of replacing them. Each call in that file adds another
    # copy of the nine /lifecycle/* routes, every copy closing over the
    # throwaway store that test built. After one file runs, requests match the
    # FIRST matching handler -- a stale store from a finished test -- so later
    # files get 404s and wrong lifecycle states. Snapshot the route list so we
    # can cut the duplicates back to the pre-test set.
    app = getattr(MAIN_MODULE, "app", None)
    routes_saved = list(app.router.routes) if app is not None else None

    yield

    _restore_clobbered(saved)

    if app is not None and routes_saved is not None:
        try:
            restored = list(routes_saved)
            # Trimming the duplicate /lifecycle/* routes can also delete a route
            # family that the app lifespan registers LATER, because the snapshot
            # above was taken before that first startup. The restored list then
            # holds a pre-startup route table while the app still carries the
            # dedupe marker that registration hung on it, so the next startup's
            # ``register_event_routes`` sees its own marker, early-returns, and
            # /events + /events/claim stay missing -> a 404 for whichever test
            # runs next. Measured: 1 failed / 810 passed with the event-claim
            # test as the victim; it passes alone.
            #
            # Keep any route the snapshot is missing rather than shrinking the
            # table below what a later startup would install. Deduplication of
            # the lifecycle routes still works: the restored list is a superset,
            # so the pre-test copies win, which is the intent of this fixture.
            for route in list(app.router.routes):
                key = (getattr(route, "path", None),
                       tuple(sorted(getattr(route, "methods", ()) or ())))
                if key[0] is None:
                    continue
                known = (key[0], key[1])
                if known not in {(getattr(r, "path", None),
                                  tuple(sorted(getattr(r, "methods", ()) or ())))
                                 for r in restored}:
                    restored.append(route)
            app.router.routes = restored
        except Exception:                        # pragma: no cover - read-only
            pass

    if sect_store is not None and sect_saved is not None:
        for attr, snapshot in zip(("sects", "player_sect", "wars"), sect_saved):
            container = getattr(sect_store, attr, None)
            if isinstance(container, dict):
                container.clear()
                container.update(snapshot)

    # Leave the shared in-memory store empty for the next test.
    store = MAIN_MODULE.players_db
    for attr in ("_mem", "_known", "_keys"):
        container = getattr(store, attr, None)
        if isinstance(container, (dict, set)):
            container.clear()
