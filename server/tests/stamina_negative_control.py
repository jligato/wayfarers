"""
NEGATIVE CONTROL for the stamina system.

The point of this script is NOT to be a test that passes. The point is to prove
that the tests in `test_stamina_system.py` can FAIL, by reverting one guard at a
time and showing the matching test goes red.

Why this matters: this project has shipped a whole class of "green" security
tests where the route 404'd for an unrelated reason and the test read the 404 as
"the guard worked". A refusal assertion that cannot go red is decoration.

SABOTAGE IS IN-MEMORY. NOTHING IS WRITTEN TO DISK.
--------------------------------------------------
The sabotage used to rewrite ``server/stamina.py`` and
``server/stamina_routes.py`` in place, run the test, and restore the bytes from
a backup. That shape is unsafe on this shared multi-agent worktree: the window
between the write and the restore is a live defect in the product, a crashed run
leaves the sabotage behind, and a restore from a backup taken at start-up will
faithfully put back an ALREADY-sabotaged tree and call it a success.

Each sabotage below now runs inside a child process that patches the loaded
module's own globals. The module object is shared by every importer, so the
route and the test see the sabotaged code, while the file on disk is never
opened for writing. There is nothing to restore because nothing is written.

Two things that shape the implementation, both verified on this tree:

1. ``stamina`` and ``server.stamina`` are TWO module objects built from the same
   file (the tests put both ``server/`` and the repo root on ``sys.path``, and
   there is no ``server/__init__.py``, so it is a namespace package). Every
   source sabotage is therefore applied to BOTH names, or the test imports one
   and the sabotage lands on the other.

2. ``stamina_routes`` builds its handlers as CLOSURES inside
   ``register_stamina_routes``. Patching the module's globals is not enough --
   the closure already captured them. So the routes are re-registered on the
   live app after the patch, which is the only way to reach a nested handler
   without writing to disk.

See ``server/tests/test_battle_pass_money_path_control.py`` for the same
in-memory shape applied to another subsystem, and
``server/tests/test_sect_v2_negative_controls.py`` for the monkeypatch variant.

Run it with a live PostgreSQL (the guards it reverts are the database ones):

    WC_PG_HOST=... WC_PG_PORT=... WC_PG_DB=... WC_PG_USER=... \\
    WC_PG_PASSWORD=... WC_SAVE_ENCRYPTION_DEV_SEED=... \\
        python server/tests/stamina_negative_control.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STAMINA_PY = REPO / "server" / "stamina.py"
ROUTES_PY = REPO / "server" / "stamina_routes.py"
MIGRATION = REPO / "server" / "migrations" / "014_stamina.sql"
TEST_FILE = "server/tests/test_stamina_system.py"
PYTHON = sys.executable

#: (label, file, old text, new text, test id that must go RED)
#:
#: NOTE ON THE SCHEMA SABOTAGES. The first version of this script edited the
#: `.sql` FILE for the three constraint checks, and all three stayed green. That
#: is not a weak test — it is a real property of this system: once
#: `apply_migrations()` has recorded version "014" in `schema_migrations`, the
#: file is never read again, so editing it changes nothing at runtime. Editing a
#: migration file to prove a test bites is the wrong instrument.
#:
#: The schema guards are therefore sabotaged by DROPPING THE CONSTRAINT ON THE
#: LIVE DATABASE and putting it back afterwards, which is what "the guard is not
#: there" actually looks like to the code under test.
SCHEMA_SABOTAGES = [
    (
        "drop the ceiling CHECK from the live database",
        "ALTER TABLE stamina_pool DROP CONSTRAINT stamina_pool_within_ceiling",
        "ALTER TABLE stamina_pool ADD CONSTRAINT stamina_pool_within_ceiling "
        "CHECK (stamina <= max_stamina)",
        "test_the_database_refuses_a_bar_above_its_ceiling",
    ),
    (
        "drop the purchase PRIMARY KEY from the live database",
        "ALTER TABLE stamina_purchase DROP CONSTRAINT stamina_purchase_pkey",
        "ALTER TABLE stamina_purchase ADD CONSTRAINT stamina_purchase_pkey "
        "PRIMARY KEY (player_id, purchase_key)",
        "test_the_purchase_primary_key_is_what_stops_a_double_charge",
    ),
    (
        "drop the recovery ledger's granted<=owed CHECK from the live database",
        "ALTER TABLE stamina_recovery DROP CONSTRAINT stamina_recovery_grant_within_owed",
        "ALTER TABLE stamina_recovery ADD CONSTRAINT stamina_recovery_grant_within_owed "
        "CHECK (stamina_granted <= stamina_owed)",
        "test_a_regeneration_row_cannot_claim_more_than_it_owed",
    ),
]

#: Source-text sabotages applied IN MEMORY inside the child process:
#: (label, module to patch, old text, new text, test id that must go RED)
#:
#: ``module`` is patched under BOTH its bare and its ``server.``-prefixed name;
#: see the header for why that is two objects and not one.
SABOTAGES = [
    (
        "make the store ADD stamina instead of subtracting it",
        "stamina",
        "        after = available - amount",
        "        after = available + amount  # SABOTAGED",
        "test_consume_deducts_and_info_agrees",
    ),
    (
        "let the purchase key be a day-only string (collides across players)",
        "stamina",
        'return f"refill:{player_id}:{int(now // 86400)}"',
        'return f"refill:{int(now // 86400)}"  # SABOTAGED: no player id',
        "test_two_players_can_both_buy_on_the_same_day",
    ),
    (
        "stop refusing a spend that grants nothing (full-bar purchase)",
        "stamina",
        "    if stamina_after_value <= stamina_before:\n        raise StaminaInjectionError(\"a purchase that grants nothing is not a purchase\")",
        "    if False:  # SABOTAGED\n        raise StaminaInjectionError(\"never\")",
        "test_a_purchase_that_grants_nothing_is_refused_before_any_write",
    ),
    (
        "let the caller choose the price by reading it from the request",
        "stamina_routes",
        "        try:\n            amount = stamina_system.cost_for(spend_type)\n        except stamina_system.StaminaInjectionError as exc:\n            raise HTTPException(status_code=400, detail=str(exc))",
        "        amount = 1  # SABOTAGED: caller-settable price",
        "test_a_caller_cannot_name_its_own_amount_or_price",
    ),
    (
        "make every type cost the same (a flat 1-stamina economy)",
        "stamina",
        '    "teleport": 20,       # = base_travel_cost * teleport_cost_multiplier (2.0)',
        '    "teleport": 1,        # SABOTAGED: flat cost',
        "test_each_known_type_costs_the_servers_price",
    ),
    (
        "let an unknown spend type fall back to a default cost",
        "stamina",
        "    if not is_known_type(type_name):\n        raise StaminaInjectionError(f\"unknown stamina type {type_name!r}\")",
        "    if False:  # SABOTAGED: unknown types default to 1\n        pass",
        "test_an_unknown_spend_type_is_refused_rather_than_defaulted",
    ),
    (
        "read the ceiling from outside the transaction (the stale-snapshot bug)",
        "stamina_routes",
        "        with _read(db, player_id):\n            return stamina_system.pool_status(db, player_id, now=time.time())",
        "        return stamina_system.pool_status(db, player_id, now=time.time())  # SABOTAGED",
        "test_the_ceiling_check_reads_the_pool_row_not_a_stale_snapshot",
    ),
]


#: The child bootstrap. It applies ONE source-text sabotage to the ALREADY
#: IMPORTED module inside this process, then hands over to pytest.
#:
#: ``inspect.getsource`` only READS the file. The anchor is replaced on the
#: returned string and the result is exec'd against the module's own ``__dict__``,
#: so every global a function needs resolves to the shipped object and the
#: route calls the sabotaged version. Zero bytes are written.
#:
#: The patch happens BEFORE pytest is handed control, so the test module imports
#: an already-sabotaged module.
_CHILD = r"""
import importlib, inspect, json, os, sys

_HERE = os.getcwd()
for _p in (_HERE, os.path.join(_HERE, "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_spec = json.loads(os.environ["WC_CTRL_SABOTAGE"])
_module = _spec["module"]
_old = _spec["old"]
_new = _spec["new"]

# Two objects, one file: `stamina` and `server.stamina` are distinct modules
# here, and so are `main` and `server.main`. Patch every name that resolves to
# the target file so whichever identity the test imports carries the sabotage.
_names = [_module]
if _module != "server." + _module:
    _names.append("server." + _module)

_patched = []
for _name in _names:
    _mod = importlib.import_module(_name)
    _src = inspect.getsource(_mod)
    _n = _src.count(_old)
    if _n != 1:
        raise SystemExit(
            "WC_CTRL anchor is not unique in %s (%d matches): %r"
            % (_name, _n, _old[:70]))
    exec(compile(_src.replace(_old, _new, 1),
                 "<in-memory control: %s>" % _name, "exec"), _mod.__dict__)
    _patched.append(_name)

# `stamina_routes` registers its handlers as CLOSURES inside
# `register_stamina_routes`, so rebinding the module globals does not reach
# them: the closure already captured the originals. Re-register the routes on
# the live app so the handlers the request path actually calls are rebuilt from
# the patched module. Routes are dropped first, otherwise the original ones are
# matched before the replacements ever get a chance.
if _module == "stamina_routes":
    import main as _top_main
    from server import main as _pkg_main
    _routes = importlib.import_module("stamina_routes")
    for _app_mod in (_top_main, _pkg_main):
        _app = _app_mod.app
        _app.router.routes[:] = [
            _r for _r in _app.router.routes
            if not getattr(_r, "path", "").startswith("/stamina")]
        _routes.register_stamina_routes(_app, lambda: _app_mod.players_db,
                                        lambda: _app_mod.wallet_store)

# Prove the patch landed, from the OTHER side: the shipped text on disk is
# untouched, so a marker in the file would mean the patch went to disk.
_src = inspect.getsource(importlib.import_module(_module))
if "WC_CTRL" in _src or "NEGCTRL" in _src:
    raise SystemExit("the in-memory control leaked a marker into the module")

import pytest
sys.exit(pytest.main(sys.argv[1:]))
"""


def _run(test_id: str, sabotage: dict | None = None) -> subprocess.CompletedProcess:
    """Run the whole stamina file, or one test id when one is named.

    Passing an empty ``test_id`` must select the FILE, not
    ``path::`` — a trailing ``::`` with no id matches nothing, so the baseline
    read "no tests ran" and the first version of this script refused to proceed
    on a perfectly green tree.

    ``sabotage`` is applied IN MEMORY in the child, never written to disk. A
    child process rather than an in-process patch, because the module objects
    are process-wide singletons: a mutant left in this process would leak into
    every later sabotage arm and the arms would stop being independent.
    """
    env = dict(os.environ)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env.pop("PYTHONPATH", None)
    if sabotage is None:
        env.pop("WC_CTRL_SABOTAGE", None)
        argv = [PYTHON, "-m", "pytest", "-q", "-p", "no:warnings"]
    else:
        env["WC_CTRL_SABOTAGE"] = json.dumps(sabotage)
        argv = [PYTHON, "-c", _CHILD]
    target = f"{TEST_FILE}::{test_id}" if test_id else TEST_FILE
    return subprocess.run(
        argv + [target], cwd=str(REPO), env=env, capture_output=True, text=True,
    )


def _require_pristine() -> None:
    """Refuse to run against a tree that is ALREADY sabotaged on disk.

    READ-ONLY. The old fixture took whatever was on disk as its "original" and
    copied it back afterwards, so a tree left broken by a crashed earlier run
    was faithfully restored as a sabotage and the control blamed the wrong
    thing. There is no backup here; this only refuses to start.
    """
    for path in (STAMINA_PY, ROUTES_PY):
        text = path.read_text(encoding="utf-8")
        for marker in ("SABOTAGED", "NEGCTRL", "WC_CTRL"):
            if marker in text:
                raise SystemExit(
                    "%s already carries a %r marker on disk, so a previous run "
                    "left a sabotage behind and this run would measure the wrong "
                    "thing. Restore it by hand before re-running." % (path, marker))


def _require_unchanged(before: dict) -> None:
    """The shipped files must be byte-identical to how the run found them."""
    for path, digest in before.items():
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        if after != digest:
            raise SystemExit(
                "%s changed on disk during the control run. This fixture must "
                "sabotage in memory only." % path)


def _sql(statements):
    """Run one or more statements against the test database, autocommitting."""
    import psycopg2

    conn = psycopg2.connect(
        host=os.environ["WC_PG_HOST"],
        port=int(os.environ.get("WC_PG_PORT", 5432)),
        dbname=os.environ["WC_PG_DB"],
        user=os.environ["WC_PG_USER"],
        password=os.environ.get("WC_PG_PASSWORD", ""),
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for stmt in statements if isinstance(statements, (list, tuple)) \
                    else [statements]:
                cur.execute(stmt)
    finally:
        conn.close()


def main() -> int:
    _require_pristine()
    # Hashed BEFORE the first child runs, so "nothing was written" is a
    # measurement rather than a promise.
    before = {f: hashlib.sha256(f.read_bytes()).hexdigest()
              for f in (STAMINA_PY, ROUTES_PY, MIGRATION)}

    print("=== BASELINE (nothing sabotaged) ===")
    base = _run("")
    print(base.stdout.strip().splitlines()[-1] if base.stdout.strip() else "?")
    if base.returncode != 0:
        print("BASELINE IS RED. A control run on an unbroken tree must be green; "
              "fix that before trusting anything below.")
        return 2

    failures = []
    try:
        # --- the database constraints, sabotaged on the live database --------
        for label, drop_sql, restore_sql, test_id in SCHEMA_SABOTAGES:
            try:
                _sql(drop_sql)
            except Exception as exc:  # noqa: BLE001
                failures.append((label, f"could not drop: {exc}", test_id))
                print(f"[DROP FAILED] {label}: {exc}")
                continue
            try:
                proc = _run(test_id)
            finally:
                # A test that is supposed to go red can leave its probe rows
                # behind — that is exactly what "it refused the second insert"
                # means when the PRIMARY KEY is gone. Restore the constraint
                # only after clearing them, or the restore itself fails on the
                # duplicates the sabotage produced. This happened on the first
                # run: `could not create unique index stamina_purchase_pkey ...
                # (stam_key_probe, refill:1) is duplicated`.
                for probe in ("stam_key_probe", "stam_ceiling_probe",
                              "stam_recov_probe"):
                    try:
                        _sql(f"DELETE FROM players WHERE id = '{probe}'")
                    except Exception:  # noqa: BLE001 - cleanup is best effort
                        pass
                _sql(restore_sql)

            went_red = proc.returncode != 0
            print(f"[{'went RED (good)' if went_red else 'STAYED GREEN (BAD)'}] {label}"
                  f"\n    -> {test_id}")
            if not went_red:
                failures.append((label, "stayed green", test_id))

        # --- the python guards, sabotaged IN MEMORY -------------------------
        # No file is opened for writing here. The child patches the module it
        # already imported, runs the named test, and exits.
        for label, module, old, new, test_id in SABOTAGES:
            proc = _run(test_id, {"module": module, "old": old, "new": new})
            combined = proc.stdout + proc.stderr
            if "WC_CTRL anchor is not unique" in combined:
                failures.append((label, "ANCHOR NOT UNIQUE", test_id))
                print(f"[ANCHOR MISS] {label}")
                continue

            went_red = proc.returncode != 0
            verdict = "went RED (good)" if went_red else "STAYED GREEN (BAD)"
            print(f"[{verdict}] {label}\n    -> {test_id}")
            if not went_red:
                failures.append((label, "stayed green", test_id))
    finally:
        # The invariant the disk-sabotage shape could not offer. A crash no
        # longer matters: nothing was written, so there is nothing to restore.
        try:
            _require_unchanged(before)
            print("SOURCE FILES UNCHANGED (verified by hash)")
        except SystemExit as exc:
            failures.append(("disk invariant", str(exc), "-"))
            print(f"[DISK CHANGED] {exc}")

    print("\n=== AFTER RESTORE ===")
    after = _run("")
    print(after.stdout.strip().splitlines()[-1] if after.stdout.strip() else "?")

    total = len(SCHEMA_SABOTAGES) + len(SABOTAGES)
    print(f"\nSABOTAGES={total} CAUGHT={total - len(failures)} "
          f"MISSED={len(failures)}")
    for label, why, tid in failures:
        print(f"  MISSED: {label} -> {tid} ({why})")
    # The tree is never mutated, so the closing check is the SHIPPED suite, not
    # a restore. If it is red, a source file is broken for real.
    if after.returncode != 0:
        print("THE SHIPPED SUITE IS RED AFTER THE RUN — do not trust this run.")
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
