"""TRIPWIRE: a ``register_*`` function that nobody calls ships its routes as dead code.

The bug class
-------------
Route mounting in this server happens through ``register_*`` functions.
``server/main.py`` calls about forty of them at import time, the ``lifespan``
startup hook calls one more, and a handful of the rest are called from *inside*
another ``register_*`` (goal_routes mounts the mission board and the achievement
routes; tower mounts run/ladder/spire; tribulation mounts breakthrough;
sect_war_routes re-exports sect_war's; battle_pass_routes delegates to
battle_pass's).

So the name appearing in a file is not evidence that its routes exist. A
function can be complete, correct, fully tested, and still never run -- and
FastAPI will not complain, because nothing asked it to. The failure shows up
much later as a 404 on a route that "ships".

Two ways it happens, both measured here:

  1. The function is never called at all.
  2. Two live files define the SAME name, so the caller reaches one of them and
     the other is unreachable while looking identical in review.

What this file asserts, and how
--------------------------------
Three separate checks, each with its own control:

  * ``test_every_register_function_is_reachable_or_exempted`` -- every
    ``def register_*`` in a live server file either RAN during a real boot of
    the app, or appears in ``EXEMPTED`` with a written reason.
  * ``test_no_live_duplicate_register_names_are_left_dead`` -- when two live
    files share a name, BOTH definitions must have run. A duplicate that
    leaves one copy dead fails here.
  * ``test_the_boot_probe_itself_can_fail`` -- the negative control. It feeds
    the same census a fake ``register_*`` that is never called and requires the
    checker to reject it. Without this, a broken probe (a child that boots
    nothing, or a census that scans the wrong directory) would report every
    name as "called" and the first test would pass forever while proving
    nothing.

The route COUNT is checked here too, not just in prose. See
``test_the_route_object_count_is_the_documented_number`` and
``test_no_route_class_hides_a_websocket``: the booted app is asserted to hold
exactly ``EXPECTED_ROUTE_OBJECTS`` (388, measured 2026-10-05) route objects,
counted unfiltered after the lifespan runs, and the per-class breakdown must
sum to that total so a ``.methods`` filter -- which drops the ``/comm/ws``
websocket and reports 387 -- cannot pass quietly.

Why a subprocess, not an in-process import
------------------------------------------
Reachability here is a claim about what HAPPENED while the app booted. In this
pytest session ``server/main.py`` was already imported by the repo-root
conftest before this file was collected, so every import-time ``register_*``
call had already run and could not be observed. Re-importing ``main`` in
process would build a SECOND app and a SECOND player store -- the exact
dual-module-identity bug the root conftest exists to prevent. So the boot runs
in a fresh child interpreter, where every call is observable.

How the child observes a call
-----------------------------
Before importing ``main``, the child imports each defining module and replaces
every ``register_*`` attribute with a wrapper that records the call and then
calls the real function. Two details are load-bearing:

  * The wrapper is keyed by ``(defining module, function name)``, not by name.
    Two files can share a name; keying by name alone cannot tell you which
    definition ran, which is the whole point of the duplicate check.
  * An ALIAS SWEEP follows. ``sect_war_routes.py`` binds
    ``register_sect_war_routes = sect_war.register_sect_war_routes`` at import
    time, so wrapping only the ``sect_war`` attribute leaves the alias pointing
    at the original and the wrapper never fires -- a false "unreachable".
    Measured: without the sweep the child reports ``register_sect_war_routes``
    as never called. The sweep re-points any module attribute that is still the
    original function object.

Every assertion about routes is made against the app object the child booted,
entered through ``TestClient`` so the ``lifespan`` startup hook has run.

Run:
    bash tools/wc_pytest.sh server/tests/test_register_tripwire.py
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parent.parent
_REPO = _SERVER.parent

# ---------------------------------------------------------------------------
# Exemptions. A name listed here is allowed to have zero production call sites.
# Each entry states WHY, so a stale exemption is visible rather than silent.
# Removing an entry whose function has since been wired in makes the test red,
# which is the intended direction of travel.
# ---------------------------------------------------------------------------
EXEMPTED: dict[str, str] = {
    # battle_pass_routes.py:239 -- a SETTER, not a route registrar. It installs
    # the callable that decides whether a payment token is real, so that
    # POST /battle-pass/api/purchase-tier can stop refusing every purchase. No
    # production code calls it: no store integration ships, which is why that
    # route is documented as running in refuse-everything mode. Only tests call
    # it, to exercise the accept path. It mounts no routes, so "uncalled" is
    # not a defect here -- it is an unused extension point.
    "register_payment_verifier": (
        "setter for the payment verifier hook; mounts no routes and is called "
        "only by tests that exercise the accept path"
    ),
}

# ---------------------------------------------------------------------------
# Duplicate names across live files, recorded so a NEW duplicate is a failure.
# Each entry is only tolerated while BOTH definitions are proven to run; see
# test_no_live_duplicate_register_names_are_left_dead.
# ---------------------------------------------------------------------------
KNOWN_DUPLICATE_NAMES: dict[str, str] = {
    # battle_pass.py:1150 and battle_pass_routes.py:329. This is a deliberate
    # wrapper pair, not an accident: battle_pass_routes owns the four
    # /battle-pass/api/* paths and delegates the six legacy ones by calling
    # battle_pass.register_battle_pass_routes. main.py:587 calls the wrapper, so
    # both definitions run and neither is dead. If the delegation inside
    # battle_pass_routes is ever deleted, the battle_pass.py copy stops running
    # and this test goes red.
    "register_battle_pass_routes": (
        "deliberate wrapper pair: battle_pass_routes.register_battle_pass_routes "
        "mounts the four /battle-pass/api/* paths and delegates the six legacy "
        "paths to battle_pass.register_battle_pass_routes"
    ),
}

# Directories under server/ that are not live source. Dot-directories cover
# backups (".bak_sect_guild_20261001" holds stale copies of sect.py and
# sect_wall.py that would otherwise read as live duplicates).
_NOT_LIVE_DIRS = {
    "tests",
    "data",
    "config",
    "migrations",
    "coding-attempts",
    "proto_gen",
    "__pycache__",
}


def _is_live_dir(name: str) -> bool:
    return not name.startswith(".") and name not in _NOT_LIVE_DIRS


def census(root: Path) -> dict[tuple[str, str], int]:
    """Every ``def register_*`` in a live ``.py`` file under ``root``.

    Returns {(relative module path, function name): line number}.

    Parsed with ``ast``, not a regex, so a mention inside a docstring or a
    comment cannot register as a definition. An unparseable file is skipped
    rather than silently contributing zero definitions -- but the skip is
    reported by the caller through ``parse_failures`` so a broken file cannot
    quietly shrink the census this test depends on.
    """
    found: dict[tuple[str, str], int] = {}
    parse_failures: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if _is_live_dir(d))
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            rel = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as exc:
                parse_failures.append(f"{rel}: {exc}")
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("register_")
                ):
                    found[(rel, node.name)] = node.lineno
    if parse_failures:
        pytest.fail(
            "census could not parse live server file(s); a broken file must not "
            "shrink the register_* surface this test guards:\n  "
            + "\n  ".join(parse_failures)
        )
    return found


# ---------------------------------------------------------------------------
# The child program. Kept as source text so the test file has no import-time
# side effect: nothing here runs unless a test asks for the boot probe.
# ---------------------------------------------------------------------------
_CHILD = r'''
import ast, importlib, json, os, sys

ROOT = sys.argv[1]
SERVER = os.path.join(ROOT, "server")
for _p in (ROOT, SERVER):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_SKIP = {"tests", "data", "config", "migrations", "coding-attempts",
         "proto_gen", "__pycache__"}


def _live(d):
    return not d.startswith(".") and d not in _SKIP


def _census():
    out = []
    for dirpath, dirnames, filenames in os.walk(SERVER):
        dirnames[:] = sorted(d for d in dirnames if _live(d))
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, SERVER).replace("\\", "/")
            try:
                tree = ast.parse(open(path, encoding="utf-8",
                                      errors="replace").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name.startswith("register_")):
                    out.append((rel, node.name, node.lineno))
    return sorted(set(out))


calls = []            # [[rel_module, name]] -- every definition that actually ran
import_failures = []  # [[rel_module, error]] -- modules we could not wrap

definitions = _census()

# Pass 1: import each defining module and capture the ORIGINAL function object.
originals = {}
for rel, name, _lineno in definitions:
    modname = rel[:-3].replace("/", ".")
    try:
        module = importlib.import_module(modname)
    except Exception as exc:                      # noqa: BLE001
        import_failures.append([rel, type(exc).__name__])
        continue
    func = getattr(module, name, None)
    if func is None or not callable(func):
        import_failures.append([rel, "MISSING:" + name])
        continue
    originals[(rel, name)] = func

# Pass 2: wrap each original so a call records (rel_module, name).
wrappers = {}
for (rel, name), func in originals.items():
    modname = rel[:-3].replace("/", ".")
    pair = (rel, name)

    def make(pair=pair, func=func):
        def spy(*args, **kwargs):
            calls.append(list(pair))
            return func(*args, **kwargs)
        return spy

    wrapper = make()
    wrappers[(rel, name)] = wrapper
    try:
        setattr(sys.modules[modname], name, wrapper)
    except Exception as exc:                      # noqa: BLE001
        import_failures.append([rel, "SETATTR:" + type(exc).__name__])

# Pass 3: ALIAS SWEEP. A module that bound the original at import time (e.g.
# sect_war_routes.py: `register_sect_war_routes = sect_war.register_sect_war_routes`)
# holds a direct reference, so wrapping the source attribute is not enough.
repointed = []
for module in list(sys.modules.values()):
    if module is None:
        continue
    for (rel, name), wrapper in wrappers.items():
        try:
            current = getattr(module, name)
        except Exception:                          # noqa: BLE001
            continue
        if any(current is orig for (r, n), orig in originals.items() if n == name):
            try:
                setattr(module, name, wrapper)
                repointed.append(getattr(module, "__name__", "?") + ":" + name)
            except Exception:                      # noqa: BLE001
                pass

# Boot for real. Importing main runs the import-time register_* calls; entering
# the TestClient runs the lifespan startup hook (register_event_routes + the
# ownership guard).
import main
from fastapi.testclient import TestClient

paths = []
with TestClient(main.app) as _client:
    # Entering the lifespan is load-bearing: startup registers 4 more routes
    # (384 at bare import -> 388 after startup). Counting outside the block
    # undercounts and the test below would pin the wrong number.
    #
    # Counted UNFILTERED. A websocket route (APIWebSocketRoute, e.g. /comm/ws)
    # has no .methods attribute, so any filter of the form hasattr(r, "methods")
    # silently drops it and reports 387 instead of 388. Record both the raw
    # object list and the class breakdown so the parent can never publish a
    # filtered number without also seeing the route the filter lost.
    route_objects = list(main.app.routes)
    for route in route_objects:
        paths.append(getattr(route, "path", None))

    route_classes = {}
    for route in route_objects:
        name = type(route).__name__
        route_classes[name] = route_classes.get(name, 0) + 1

    no_methods = sorted(
        (type(route).__name__, getattr(route, "path", "<no .path attribute>"))
        for route in route_objects if not getattr(route, "methods", None)
    )

print("TRIPWIRE_JSON " + json.dumps({
    "definitions": [list(d) for d in definitions],
    "calls": calls,
    "routes": paths,
    "route_object_count": len(route_objects),
    "route_classes": route_classes,
    "routes_without_methods": [list(pair) for pair in no_methods],
    "import_failures": import_failures,
    "repointed": sorted(set(repointed)),
}))
'''


def _run_boot_probe(root: Path, server: Path) -> dict:
    """Boot the real app in a fresh interpreter and report what ran."""
    script = server / "tests" / "_register_tripwire_child.py"
    script.write_text(_CHILD, encoding="utf-8")
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("WC_PG_")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, str(script), str(root)],
            capture_output=True,
            text=True,
            cwd=str(root),
            env=env,
            timeout=600,
        )
    finally:
        try:
            script.unlink()
        except OSError:
            pass

    marker = "TRIPWIRE_JSON "
    idx = proc.stdout.find(marker)
    if idx < 0:
        pytest.fail(
            "the boot probe produced no result, so nothing below it is proven.\n"
            f"returncode={proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout[-3000:]}\n"
            f"--- stderr ---\n{proc.stderr[-3000:]}"
        )
    return json.loads(proc.stdout[idx + len(marker):].splitlines()[0])


@pytest.fixture(scope="module")
def boot() -> dict:
    """One real boot of the app, shared by every check in this file.

    Module scope because the boot costs a full interpreter start plus the app's
    own import and startup work, and because the three assertions below all
    read the same boot -- a second boot would only add a way for them to
    disagree.
    """
    return _run_boot_probe(_REPO, _SERVER)


# ---------------------------------------------------------------------------
# The checker, separated from the probe so the negative control can exercise
# the exact logic the real assertion uses.
# ---------------------------------------------------------------------------
def find_unreachable(boot_result: dict) -> dict[tuple[str, str], int]:
    """Definitions that never ran during the boot and are not exempt.

    Returns {(relative path, function name): line number} so the failure
    message can point at the source rather than just naming the function.
    """
    ran = {tuple(pair) for pair in boot_result["calls"]}
    exempt = set(EXEMPTED)
    return {
        (rel, name): lineno
        for rel, name, lineno in map(tuple, boot_result["definitions"])
        if (rel, name) not in ran and name not in exempt
    }


# ---------------------------------------------------------------------------
# Control: is the census itself real?
# ---------------------------------------------------------------------------
def test_the_boot_probe_itself_can_fail():
    """NEGATIVE CONTROL. A fake uncalled ``register_*`` must be rejected.

    Without this, a probe that booted nothing, or a census pointed at the wrong
    directory, would report every definition as reached and the main assertion
    would pass while measuring nothing.
    """
    fake = {
        "definitions": [["guild.py", "register_never_called_probe", 7]],
        "calls": [],          # nothing ran
        "routes": ["/"],
        "import_failures": [],
        "repointed": [],
    }
    assert find_unreachable(fake) == {("guild.py", "register_never_called_probe"): 7}, (
        "the reachability checker accepted a register_* function that never ran; "
        "the main assertion is therefore measuring nothing"
    )

    # And the converse, so the checker is not simply "always reject":
    ran = {
        "definitions": [["guild.py", "register_called_probe", 7]],
        "calls": [["guild.py", "register_called_probe"]],
        "routes": ["/"],
        "import_failures": [],
        "repointed": [],
    }
    assert find_unreachable(ran) == {}

    # The census must also find a real definition in a real file, or it is
    # scanning nothing:
    real = census(_SERVER)
    assert ("guild.py", "register_guild_routes") in real, (
        "the census did not find register_guild_routes in server/guild.py; it "
        "is scanning the wrong tree or nothing at all"
    )


# ---------------------------------------------------------------------------
# The tripwire.
# ---------------------------------------------------------------------------
def test_every_register_function_is_reachable_or_exempted(boot):
    """Every ``def register_*`` either ran during a real boot, or is exempt."""
    assert not boot["import_failures"], (
        "could not import a module that defines a register_* function, so its "
        f"reachability is unknown: {boot['import_failures']}"
    )

    definitions = [tuple(d) for d in boot["definitions"]]
    assert len(definitions) >= 50, (
        f"census found only {len(definitions)} register_* definitions; the "
        "server is known to have far more, so the scan is broken"
    )

    unreachable = find_unreachable(boot)
    assert not unreachable, (
        "these register_* functions were defined but never ran during a real "
        "boot of the app, so the routes they mount do not exist:\n"
        + "\n".join(
            f"  server/{rel}:{lineno}  def {name}()"
            for (rel, name), lineno in sorted(unreachable.items())
        )
        + "\nIf one of these is deliberate, add it to EXEMPTED with the reason."
    )


def test_the_boot_actually_registered_routes(boot):
    """The reachability result is only meaningful if routes really were mounted.

    Guards against the failure mode where the child imports nothing, records
    no calls, and the main assertion would otherwise pass because the census
    was also empty.
    """
    paths = boot["routes"]
    assert len(paths) > 300, f"only {len(paths)} routes after boot; expected hundreds"
    assert len(set(paths)) == len(paths), "the booted app has duplicate paths"

    # A route from a module whose register_* ran at import time, and one
    # registered by the lifespan startup hook. Both prove the import path and
    # the startup hook actually executed.
    assert "/guild/create" in paths, "guild routes missing: import-time mounting did not run"
    assert "/events/list" in paths, (
        "lifespan-registered event routes missing: the startup hook did not run"
    )


# ---------------------------------------------------------------------------
# The route count itself.
#
# MEASURED 2026-10-05: the booted app holds 388 route objects = 374 APIRoute +
# 9 LifecycleRoute + 4 docs pages + 1 APIWebSocketRoute. Docs say 388.
#
# Why this is an assertion and not a number in a sentence: a hand-edited figure
# cannot tell anyone it went stale. On 2026-10-05 four documents had already
# gone stale against each other over "387" vs "388", because counting with
# `hasattr(r, "methods")` silently drops the /comm/ws websocket route (a
# websocket route has no .methods attribute) and reports 387. So the two
# defects are fixed together:
#
#   * EXPECTED_ROUTE_OBJECTS below is the contract. Change it deliberately, in
#     this file, when the API genuinely grows -- never in a doc.
#   * The assertion requires the class breakdown to add up. If someone re-adds a
#     .methods filter, the websocket disappears from the breakdown, the sum no
#     longer equals the total, and the test goes RED instead of quietly
#     publishing 387 again.
#
# EXPECTED_ROUTE_OBJECTS is 388, measured 2026-10-05 (before this test existed)
# by entering the app lifespan with TestClient and counting len(app.routes) with
# no filter. The census was reproduced independently the same day.
EXPECTED_ROUTE_OBJECTS = 388
# The classes the 388 decomposes into, as a floor rather than an exact set: a new
# class is allowed to appear, a drop in one of these is not.
EXPECTED_MINIMUM_PER_CLASS = {
    "APIRoute": 374,
    "LifecycleRoute": 9,
    "Route": 4,               # the FastAPI docs pages
    "APIWebSocketRoute": 1,    # /comm/ws
}


def test_the_route_object_count_is_the_documented_number(boot):
    """The booted app must hold exactly the documented number of route objects.

    Counting is UNFILTERED (see the child program) and taken AFTER the lifespan
    has run, because startup registers 4 more routes than a bare import sees.
    """
    total = boot["route_object_count"]
    classes = boot["route_classes"]

    assert total == len(boot["routes"]), (
        f"the child reported {total} route objects but listed "
        f"{len(boot['routes'])} paths; the census is not a faithful copy of "
        "app.routes and nothing downstream can be trusted"
    )

    assert sum(classes.values()) == total, (
        f"the class breakdown sums to {sum(classes.values())}, not the reported "
        f"total of {total}. A route object is being dropped before it is "
        "classified -- almost always a filter on .methods, which removes the "
        f"websocket route. Breakdown: {classes}"
    )

    assert total == EXPECTED_ROUTE_OBJECTS, (
        f"the booted app has {total} route objects, but this file records "
        f"{EXPECTED_ROUTE_OBJECTS} and the design docs were corrected to "
        f"{EXPECTED_ROUTE_OBJECTS} on 2026-10-05.\n"
        f"  breakdown: {classes}\n"
        "If the API really gained or lost routes, update EXPECTED_ROUTE_OBJECTS "
        "and the docs together, in the same commit."
    )

    for name, floor in EXPECTED_MINIMUM_PER_CLASS.items():
        assert classes.get(name, 0) >= floor, (
            f"class {name} has {classes.get(name, 0)} instances, expected at "
            f"least {floor}. Check the app did not silently drop a registrar."
        )


def test_no_route_class_hides_a_websocket(boot):
    """The routes without .methods must be named out loud, not counted away.

    A websocket route cannot serve HTTP verbs and so has no .methods. Every
    counting method that filters on .methods loses exactly these. Listing them
    means a new socket shows up in the failure output of the count test instead
    of quietly reducing it by one.
    """
    no_methods = [tuple(pair) for pair in boot["routes_without_methods"]]
    named = {(cls, path) for cls, path in no_methods}

    assert ("APIWebSocketRoute", "/comm/ws") in named, (
        "the /comm/ws chat socket is missing from app.routes. A route count that "
        f"filters on .methods will report 387 again; current classes: {no_methods}"
    )

    # Every entry must be accounted for by name, so an unclassified object fails
    # rather than being absorbed into the total.
    classified = {cls for cls, _ in no_methods}
    assert classified <= set(EXPECTED_MINIMUM_PER_CLASS), (
        f"a route without .methods has a class this file does not name: "
        f"{sorted(classified - set(EXPECTED_MINIMUM_PER_CLASS))}. Name it in "
        "EXPECTED_MINIMUM_PER_CLASS or work out why it should not be there."
    )


def test_no_live_duplicate_register_names_are_left_dead(boot):
    """Two live files sharing a name is allowed only if BOTH copies ran.

    The failure this catches is the shadowing one: the caller reaches the copy
    it imported by name, and the other copy looks complete and tested while
    shipping nothing.
    """
    ran = {tuple(pair) for pair in boot["calls"]}

    by_name: dict[str, list[str]] = {}
    for rel, name, _lineno in map(tuple, boot["definitions"]):
        by_name.setdefault(name, []).append(rel)

    duplicates = {n: files for n, files in by_name.items() if len(files) > 1}
    undeclared = set(duplicates) - set(KNOWN_DUPLICATE_NAMES)
    assert not undeclared, (
        "a register_* name is now defined in more than one live file and is not "
        f"recorded in KNOWN_DUPLICATE_NAMES: {undeclared}. Add it with the reason, "
        "or remove the duplicate."
    )

    dead_copies = {
        name: files for name, files in duplicates.items()
        if not all((f, name) in ran for f in files)
    }
    assert not dead_copies, (
        "these register_* names are defined twice and at least one copy never "
        "ran during a real boot, so that copy ships dead routes:\n"
        + "\n".join(
            f"  {name}: defined in {files}, reached only in "
            f"{[f for f in files if (f, name) in ran]}"
            for name, files in sorted(dead_copies.items())
        )
    )


def test_every_exemption_is_still_needed(boot):
    """An exemption whose function has since been wired in should be removed.

    This is what stops the exemption table from quietly becoming a place where
    dead code goes to be forgotten. Direction of travel: wiring a function in
    turns this red, and the fix is deleting the entry.
    """
    ran_names = {tuple(pair)[1] for pair in boot["calls"]}
    unnecessary = sorted(set(EXEMPTED) & ran_names)
    assert not unnecessary, (
        "these exemptions are no longer needed because the function now runs "
        f"during boot: {unnecessary}. Delete the entries from EXEMPTED."
    )