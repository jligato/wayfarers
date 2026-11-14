"""
Does the ONCE-ONLY payment guarantee survive a real PostgreSQL database?

THE GAP THIS FILE CLOSES
------------------------
The guarantee was fixed and proven only against the IN-MEMORY store. That proof
cannot transfer, because the database path is a DIFFERENT CLASS:

  * ``wallet_defense.WalletStore``    -- in-memory. Replay state is a Python dict
    (``seen``/``recall``, ``wallet_defense.py:350``).
  * ``wallet_store_pg.WalletStore``   -- PostgreSQL. Replay state is the
    ``idempotency_keys`` TABLE, and the replay is a ``SELECT`` inside the same
    cursor as the balance ``UPDATE`` (``_replay``/``_record``,
    ``wallet_store_pg.py:98-108``).

``wallet_dispatch.spend``/``earn`` pick between them per call on
``store.is_postgres`` (``wallet_dispatch.py:41-53``). So the in-memory test drove
code the deployed game never runs, and the PostgreSQL replay branch had never
been asserted against a BALANCE.

THE ROUTE UNDER TEST
--------------------
``POST /gift-series/{player_id}/{series_id}/purchase`` -- ``gift_series.py:642``,
charging under the stable replay key
``gift_purchase:{player_id}:{series_id}:{tier}:{currency}`` (``gift_series.py:698``).

Why this route: the defect the memory-mode proof was written for lives here.
``gift_series.py:664-672`` records it -- the key used to embed
``int(time.time())``, so every retry crossing a second minted a fresh key and
charged the player again; and one key was shared across several currencies, so
the second currency replayed the first one's receipt and was never charged.
``server/tests/test_gift_charged_once.py`` proves the fix against
``wallet_defense.WalletStore``. This file is the missing other half.

WHAT IS ASSERTED
----------------
1. Two identical purchases move the balance ONCE. The second pays 0.
2. Two DIFFERENT purchases (``monthly`` then ``season``) BOTH move it. A guard
   that blocks everything would satisfy (1) while being an outage.
3. The same for the CREDIT path (``earn``), which is separate code.
4. A negative control: remove the receipt mechanism and (1) must go red.

Every balance is read from a FRESH DATABASE CONNECTION. The HTTP response is
written by the code under test, so quoting it proves only that the code agrees
with itself.

A purchase also GRANTS its first tier, so the expected movement is the COST
minus that grant. Those numbers are DERIVED from the shipped route and series
definitions at run time, never typed here, so this file cannot drift from the
product about what a purchase costs or pays.

SKIPS CLEANLY
-------------
No ``WC_PG_HOST``, no container, or no ``psycopg2`` -> the module skips with a
reason naming what is missing. This is a REGRESSION PIN on a route, so a
checkout with no database must be able to run it and see a skip rather than a
red suite. Run with ``-rs`` to see the reason: a silent skip is not a pass.

With a database, the fixture builds its OWN throwaway database whose name ends in
``_test`` and is unique per run, applies the schema with the APPLICATION'S OWN
migration runner (``server.migrations_runner.apply_migrations``) -- never the
.sql files by hand, which collide with it -- and drops it afterwards. It never
touches the shared ``wayfarers`` database.

The password is RESOLVED at run time from the container's own environment. No
literal is in this file and none is printed.

Run
---
    python -m pytest server/tests/test_once_only_pg.py -v -rs
"""

from __future__ import annotations

import os
import pathlib
import secrets
import subprocess
import sys
from typing import Dict, Tuple

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "server"
for _path in (str(REPO_ROOT), str(SERVER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

PG_CONTAINER = "wcpg-hermes"
PG_PORT = "55432"
PASSWORD_ENV_VARS = ("WC_TEST_PG_PASSWORD", "WC_PG_PASSWORD", "PGPASSWORD")
DEV_SEED_ENV = "WC_SAVE_ENCRYPTION_DEV_SEED"
MASTER_KEY_ENV = "WC_SAVE_ENCRYPTION_KEY"

#: The purchase tier as the route names it, and the series key it initialises
#: (gift_series.py:712). Used only to pick the right URL. Every NUMBER asserted
#: below is derived from the product at run time.
PURCHASE_SERIES_KEY = {"monthly": "monthly_card", "season": "season_card"}

#: Enough for the most expensive single purchase, so no test can fail for want
#: of money instead of for the reason it exists.
STARTING_BALANCE = 100000


# ---------------------------------------------------------------------------
# 1. Is a database even available? Decided ONCE at import so the skip reason is
#    stable and readable under -rs.
# ---------------------------------------------------------------------------

def _probe_database() -> Tuple[bool, str]:
    if not os.environ.get("WC_PG_HOST"):
        return False, ("WC_PG_HOST is not set. This file needs a real PostgreSQL; "
                       "see docs/POSTGRES-TEST-DATABASE.md. The in-memory "
                       "equivalent is server/tests/test_gift_charged_once.py, and "
                       "it does NOT cover the database store class.")
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return False, "psycopg2 is not installed in this interpreter"
    try:
        r = subprocess.run(["docker", "inspect", PG_CONTAINER],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "docker is not usable on this host (%s)" % (exc,)
    if r.returncode != 0:
        return False, ("container %r is not running. Start it with: docker start %s"
                       % (PG_CONTAINER, PG_CONTAINER))
    return True, ""


_AVAILABLE, _SKIP_REASON = _probe_database()

pytestmark = pytest.mark.skipif(
    not _AVAILABLE,
    reason="once-only payment guarantee is UNPROVEN against PostgreSQL -- %s"
           % (_SKIP_REASON,))


# ---------------------------------------------------------------------------
# 2. Credentials and container plumbing. Never a literal.
# ---------------------------------------------------------------------------

def _container_env() -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        raw = subprocess.run(
            ["docker", "inspect", PG_CONTAINER, "--format",
             "{{range .Config.Env}}{{println .}}{{end}}"],
            capture_output=True, text=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _resolve_password() -> str:
    for name in PASSWORD_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return _container_env().get("POSTGRES_PASSWORD", "")


def _resolve_user() -> str:
    return _container_env().get("POSTGRES_USER") or "wctest"


def _psql(sql: str, db: str, user: str) -> subprocess.CompletedProcess:
    """psql INSIDE the container: local socket, so no password on the argv."""
    return subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", user, "-d", db,
         "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------------------
# 3. The fixture: a throwaway database built by the application's own runner.
# ---------------------------------------------------------------------------

@pytest.fixture
def pg_database():
    """A real PostgreSQL database with the schema the APPLICATION applied.

    The order is the recipe's whole point:

    1. Resolve the password from the container; never store it.
    2. Create a database whose name ends in ``_test``, unique per run -- this box
       is shared, and a fixed name lets a concurrent run's DROP/CREATE race it.
    3. Generate a random encryption seed. PostgreSQL mode encrypts a player's
       non-column state, so without one every write dies with
       ``MasterKeyUnavailable``, which names nothing about databases.
    4. Apply the schema with ``apply_migrations()``. The by-hand path is never
       used here; mixing the two produces a database the app cannot boot.
    """
    from server.migrations_runner import apply_migrations

    user = _resolve_user()
    password = _resolve_password()
    if not password:
        pytest.fail("no password for %r could be resolved from the environment or "
                    "from `docker inspect`. Refusing to guess." % (PG_CONTAINER,))

    db = "wc_onceonly_%s_test" % secrets.token_hex(4)
    for sql in ("DROP DATABASE IF EXISTS %s WITH (FORCE);" % db,
                "CREATE DATABASE %s OWNER %s;" % (db, user)):
        r = _psql(sql, "postgres", user)
        if r.returncode != 0:
            pytest.fail("could not prepare the throwaway database %r: %s"
                        % (db, r.stderr.strip()))

    previous = {k: os.environ.get(k) for k in (
        "WC_PG_HOST", "WC_PG_PORT", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD",
        MASTER_KEY_ENV, DEV_SEED_ENV)}
    try:
        os.environ.pop(MASTER_KEY_ENV, None)
        os.environ[DEV_SEED_ENV] = "wc-once-only-%s" % secrets.token_hex(24)
        os.environ.update(WC_PG_HOST="127.0.0.1", WC_PG_PORT=PG_PORT, WC_PG_DB=db,
                          WC_PG_USER=user, WC_PG_PASSWORD=password)

        result = apply_migrations()
        if result.skipped_no_database or not result.applied:
            pytest.fail(
                "the application's migration runner applied %r migrations "
                "(no-database=%r) to %r. This fixture owns the schema and must "
                "build a real one; an empty database would let every assertion "
                "below pass for the wrong reason."
                % (result.applied, result.skipped_no_database, db))

        yield {"db": db, "user": user, "password": password}
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _psql("DROP DATABASE IF EXISTS %s WITH (FORCE);" % db, "postgres", user)


# ---------------------------------------------------------------------------
# 4. Reading the truth: always a fresh connection.
# ---------------------------------------------------------------------------

def _connect(db: str, user: str, password: str):
    import psycopg2
    return psycopg2.connect(host="127.0.0.1", port=int(PG_PORT), dbname=db,
                            user=user, password=password)


def _query(db: str, user: str, password: str, sql: str, params=()) -> list:
    conn = _connect(db, user, password)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description is not None else []
        conn.commit()
        return rows
    finally:
        conn.close()


def _balance(db: str, user: str, password: str, player_id: str) -> Dict[str, int]:
    """The player's currency balances, from a FRESH connection."""
    rows = _query(db, user, password,
                  "SELECT balance FROM wallets WHERE player_id = %s", (player_id,))
    assert rows, ("no wallets row for %r, so the balance cannot be read at all. "
                  "The route never created one." % (player_id,))
    balance = rows[0][0]
    if isinstance(balance, str):
        import json
        balance = json.loads(balance)
    return {str(k): int(v) for k, v in (balance or {}).items()}


def _receipt_keys(db: str, user: str, password: str, player_id: str) -> list:
    rows = _query(db, user, password,
                  "SELECT key FROM idempotency_keys WHERE player_id = %s ORDER BY key",
                  (player_id,))
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# 5. Expected money, DERIVED from the shipped definitions.
# ---------------------------------------------------------------------------

def _route_costs() -> Dict[str, Dict[str, int]]:
    """The per-tier cost table, read out of the route's own source.

    ``purchase_gift_series`` builds ``costs`` as a local, so it is not reachable
    as an attribute. Rather than duplicate the numbers, this parses the literal
    out of the function source. A price change in the route therefore flows
    through to the expectation automatically instead of making this file lie.
    """
    import ast

    import gift_series

    # `purchase_gift_series` is NESTED inside register_gift_series_routes, so it
    # is not a module attribute and `inspect.getsource` cannot reach it. Parse
    # the module file and find the function by name instead.
    tree = ast.parse(pathlib.Path(gift_series.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "purchase_gift_series":
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Assign)
                        and any(getattr(t, "id", None) == "costs"
                                for t in inner.targets)
                        and isinstance(inner.value, ast.Dict)):
                    return ast.literal_eval(inner.value)
    raise AssertionError(
        "could not read the `costs` dict out of purchase_gift_series. This file "
        "derives its expected money from the product so the two cannot drift; "
        "if the route was restructured, update _route_costs to match.")


def _tier1_grant(series_id: str) -> Dict[str, int]:
    """What the first tier of a series pays, in wallet currencies.

    Read from the product rather than typed here. A hardcoded 100 would be a
    snapshot: the next content edit would move the number and this file would
    fail for a reason that has nothing to do with idempotency.
    """
    import gift_series

    rewards = gift_series.SERIES_REGISTRY[series_id].tiers[0].rewards or {}
    return {"spirit_stone": int(rewards.get("spirit_stones", 0) or 0),
            "fateum": int(rewards.get("fateum", 0) or 0)}


def _net_expected(tier_key: str) -> Dict[str, int]:
    """Balance movement of ONE purchase of ``tier_key``: grant minus cost.

    Negative = money left the wallet. A purchase both charges and grants its
    first tier, so the NET is what the player actually experiences; asserting
    the raw cost would be wrong by the grant every time.
    """
    series_id = PURCHASE_SERIES_KEY[tier_key]
    costs = _route_costs()[series_id]
    grant = _tier1_grant(series_id)
    return {c: grant.get(c, 0) - costs.get(c, 0)
            for c in set(list(costs) + list(grant))}


# ---------------------------------------------------------------------------
# 6. The app, wired to the POSTGRESQL store class.
# ---------------------------------------------------------------------------

@pytest.fixture
def pg_app(pg_database):
    """A FastAPI app whose wallet store is the PostgreSQL class.

    The in-memory class is asserted NOT to be in use, for the reason the sibling
    durability harness gives: a test that silently ran against memory would pass
    while measuring nothing, and a green summary line looks exactly like a real
    proof.
    """
    from fastapi import FastAPI

    import gift_series
    import wallet_store_pg
    from player_store import PlayerStore

    players = PlayerStore()
    assert players._pg is True, (
        "PlayerStore is not in database mode, so this file would measure the "
        "in-memory store and pass without touching PostgreSQL at all")
    wallet = wallet_store_pg.WalletStore(players)
    assert wallet.is_postgres is True, (
        "the wallet store is not the PostgreSQL class, so the replay branch "
        "under test is not the one the deployed game runs")

    # Seed one player + wallet. `players.signing_key` is NOT NULL and the route
    # reads the row, so a partial insert dies on a constraint that has nothing
    # to do with idempotency.
    import psycopg2
    player_id = "onceonly_%s" % secrets.token_hex(6)
    conn = _connect(pg_database["db"], pg_database["user"], pg_database["password"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO players (id, name, spirit_root, signing_key) "
                "VALUES (%s, %s, %s, %s)",
                (player_id, "OnceOnlyProbe", "none",
                 psycopg2.Binary(os.urandom(32))))
            cur.execute(
                "INSERT INTO wallets (player_id, balance, version) "
                "VALUES (%s, %s::jsonb, 0)",
                (player_id, '{"spirit_stone": %d, "fateum": %d}'
                 % (STARTING_BALANCE, STARTING_BALANCE)))
        conn.commit()
    finally:
        conn.close()

    app = FastAPI()
    gift_series.init_gift_series_store(pg_pool=None)
    gift_series.register_gift_series_routes(
        app, store_provider=lambda: players, wallet_store_provider=lambda: wallet)

    from fastapi.testclient import TestClient

    yield {"client": TestClient(app), "player_id": player_id, "wallet": wallet,
           "players": players, "db": pg_database["db"], "user": pg_database["user"],
           "password": pg_database["password"]}


def _purchase(pg_app, tier_key: str):
    """POST the purchase route for one tier. The same URL and params both times
    when the caller is testing a replay."""
    return pg_app["client"].post(
        "/gift-series/%s/%s/purchase"
        % (pg_app["player_id"], PURCHASE_SERIES_KEY[tier_key]),
        params={"tier": tier_key})


def _moved(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
    """How much each currency moved. Negative = money left the wallet."""
    return {c: after.get(c, 0) - before.get(c, 0)
            for c in sorted(set(before) | set(after))}


# ===========================================================================
# 1. THE VERDICT: the same purchase key, twice, on a real database
# ===========================================================================

def test_the_same_purchase_key_is_charged_once_against_postgresql(pg_app):
    """Two identical purchases must move the balance ONCE. THE CLAIM.

    Read from a fresh database connection, never from the response the code
    under test produced.
    """
    db, user, password = pg_app["db"], pg_app["user"], pg_app["password"]
    pid = pg_app["player_id"]

    # Anti-vacuity FIRST. If a fresh connection could not read, an unchanged
    # balance would be indistinguishable from "the replay correctly paid
    # nothing" -- the exact confusion this file exists to rule out.
    assert _query(db, user, password, "SELECT 1") == [(1,)], (
        "this file could not open its own database connection, so every balance "
        "assertion below would be reading nothing")

    before = _balance(db, user, password, pid)
    assert before.get("spirit_stone") == STARTING_BALANCE, before

    first = _purchase(pg_app, "monthly")
    second = _purchase(pg_app, "monthly")
    after = _balance(db, user, password, pid)
    moved = _moved(before, after)
    expected = _net_expected("monthly")

    print("\n=== SAME KEY, CALLED TWICE (monthly) ===")
    print("RESPONSE 1 status=%s body=%s" % (first.status_code, first.text[:600]))
    print("RESPONSE 2 status=%s body=%s" % (second.status_code, second.text[:600]))
    print("BALANCE before=%s" % (before,))
    print("BALANCE after =%s" % (after,))
    print("MOVED         =%s" % (moved,))
    print("EXPECTED (ONE purchase) =%s" % (expected,))
    print("idempotency receipts=%s" % (_receipt_keys(db, user, password, pid),))

    assert first.status_code == 200, (
        "the FIRST purchase did not succeed, so the replay half measured "
        "nothing: %s %s" % (first.status_code, first.text[:400]))

    twice = {c: expected[c] * 2 for c in expected}
    assert moved == expected, (
        "ONCE-ONLY FAILS against PostgreSQL: two identical monthly purchases "
        "moved %s where ONE purchase moves %s. The database store class replayed "
        "nothing, so the retry was charged again.%s"
        % (moved, twice if moved == twice else expected,
           " (That is the DOUBLE-CHARGE figure.)" if moved == twice else ""))

    # The receipt table is the MECHANISM, not a side effect. If it were empty,
    # the green above could have come from another path and the guarantee would
    # still be unproven for the reason that matters.
    receipts = _receipt_keys(db, user, password, pid)
    assert any(k.startswith("gift_purchase:") for k in receipts), (
        "no `gift_purchase:` receipt exists in idempotency_keys (%r). The replay "
        "must be decided by a row in the table; without one, once-only came from "
        "somewhere this test has not measured." % (receipts,))


# ===========================================================================
# 2. No over-blocking: two DIFFERENT keys both pay
# ===========================================================================

def test_two_different_purchases_both_charge_against_postgresql(pg_app):
    """A once-only guard must not block everything.

    ``monthly`` and ``season`` are two distinct logical purchases, keyed
    differently (``gift_series.py:698``). A guard that namespaced the key too
    broadly -- or a uniqueness constraint with the wrong columns -- would make
    the "charged once" test above pass while quietly breaking the game. So this
    asserts the OPPOSITE direction.
    """
    db, user, password = pg_app["db"], pg_app["user"], pg_app["password"]
    pid = pg_app["player_id"]

    before = _balance(db, user, password, pid)
    monthly = _purchase(pg_app, "monthly")
    season = _purchase(pg_app, "season")
    after = _balance(db, user, password, pid)
    moved = _moved(before, after)

    one_monthly = _net_expected("monthly")
    one_season = _net_expected("season")
    expected = {c: one_monthly.get(c, 0) + one_season.get(c, 0)
                for c in set(one_monthly) | set(one_season)}

    print("\n=== TWO DIFFERENT KEYS (monthly then season) ===")
    print("monthly status=%s body=%s" % (monthly.status_code, monthly.text[:500]))
    print("season  status=%s body=%s" % (season.status_code, season.text[:500]))
    print("BALANCE before=%s" % (before,))
    print("BALANCE after =%s" % (after,))
    print("MOVED         =%s" % (moved,))
    print("EXPECTED (BOTH paid) =%s" % (expected,))

    assert monthly.status_code == 200, (
        "the monthly purchase failed, so the two-distinct-keys half measured "
        "nothing: %s %s" % (monthly.status_code, monthly.text[:400]))
    assert season.status_code == 200, (
        "the SEASON purchase was REFUSED. That is OVER-BLOCKING: monthly and "
        "season are different logical purchases keyed differently at "
        "gift_series.py:698, and a once-only guarantee that swallows the second "
        "is an outage dressed as a guard: %s %s"
        % (season.status_code, season.text[:400]))

    assert moved == expected, (
        "two DIFFERENT purchases moved %s where together they should move %s. "
        "Either one was blocked (over-blocking) or one was charged twice."
        % (moved, expected))


# ===========================================================================
# 3. The CREDIT path: a retried claim pays once
# ===========================================================================

def test_the_credit_path_pays_once_against_postgresql(pg_app):
    """``earn`` is a different code path from ``spend`` and a different bug.

    ``gift_series.claim_tier`` credits under
    ``gift_series_reward:{player}:{series}:{tier}:spirit_stone``
    (``gift_series.py:452-454``). Two identical credits of tier 1 must move the
    balance once. This drives the WALLET directly rather than the route, because
    the route's 24-hour tier cooldown refuses the second claim for a reason that
    has nothing to do with idempotency and would mask the question.
    """
    db, user, password = pg_app["db"], pg_app["user"], pg_app["password"]
    pid = pg_app["player_id"]

    import wallet_store_pg
    from player_store import PlayerStore

    players = PlayerStore()
    wallet = wallet_store_pg.WalletStore(players)

    tier1 = _tier1_grant("new_player_7_day")["spirit_stone"]
    assert tier1 > 0, "tier 1 of new_player_7_day pays no spirit stones"

    key = "gift_series_reward:%s:new_player_7_day:0:spirit_stone" % pid
    before = _balance(db, user, password, pid)

    with players.transaction():
        first = wallet_store_pg.earn(wallet, pid, "spirit_stone", tier1, key,
                                     "gift_series_reward")
    with players.transaction():
        second = wallet_store_pg.earn(wallet, pid, "spirit_stone", tier1, key,
                                      "gift_series_reward")
    after = _balance(db, user, password, pid)
    credited = after["spirit_stone"] - before["spirit_stone"]

    print("\n=== CREDIT PATH, SAME KEY TWICE ===")
    print("attempt 1: %s" % (first,))
    print("attempt 2: %s" % (second,))
    print("CREDITED spirit_stone=%d (expected %d if once-only HOLDS)"
          % (credited, tier1))

    assert first.get("ok") is True, first
    assert second.get("_replayed") is True, (
        "the second identical credit did not come back as a replay. Without the "
        "_replayed marker the wallet re-executed the grant: %s" % (second,))
    assert credited == tier1, (
        "ONCE-ONLY FAILS against PostgreSQL on the CREDIT path: two identical "
        "credits moved %d spirit stones where one pays %d%s."
        % (credited, tier1,
           " (credited TWICE)" if credited == tier1 * 2 else ""))


# ===========================================================================
# 4. PINNED DEFECT: the replayed purchase answers 400, not the receipt
# ===========================================================================
#
# MEASURED against PostgreSQL, not reasoned. The second identical purchase
# returns 400 {"detail":"Failed to initialize series"} while the money moved
# once -- so this is a RESPONSE-CONTRACT defect, not a double charge.
#
# Mechanism, confirmed by calling claim_tier directly against the same
# database: the second claim is refused by the 24-hour tier cooldown
# (gift_series.py:416-418), and the route DISCARDS that reason and substitutes a
# generic 400 (gift_series.py:713-716).
#
# Why it matters: a client that retries a purchase after a lost response gets an
# error, shows the player a failure, and may retry again -- while the wallet
# silently replays the original charge. The receipt exists but is unreachable
# through the route.
#
# strict=True: if someone fixes this the xfail becomes an XPASS and the suite
# goes RED demanding this marker be removed. That is the point of pinning it.

# RESOLVED 2026-10-04. This test carried ``xfail(strict=True)`` over a defect in
# gift_series.py:713-716, where a replayed purchase discarded the real reason
# and answered 400 instead of replaying the stored 200 receipt. The route now
# replays the stored receipt, so this test PASSES and the marker is GONE.
#
# Why a strict xfail had to go rather than stay: strict=True turns a fix into an
# XPASS, which pytest reports as a FAILURE. The suite would have stayed red on a
# fixed defect until someone deleted this line -- and a marker left in place
# after the fix is a hole, not a safety net.

def test_a_replayed_purchase_answers_400_instead_of_the_original_receipt(pg_app):
    """Pins the measured behaviour so the defect cannot change unnoticed."""
    first = _purchase(pg_app, "monthly")
    second = _purchase(pg_app, "monthly")
    print("\nreplay status 1=%s 2=%s body=%s"
          % (first.status_code, second.status_code, second.text[:300]))
    assert second.status_code == 200, (
        "the replayed purchase answered %s %s. Expected the original 200 "
        "receipt to be replayed." % (second.status_code, second.text[:300]))


# ===========================================================================
# 5. THE NEGATIVE CONTROL: the receipt must be what makes test 1 green
# ===========================================================================
#
# A green test proves nothing until it CAN go red. This removes the receipt
# mechanism -- the replay SELECT returns nothing and the receipt INSERT does
# nothing, which is exactly "the wallet has no idempotency at all" -- and
# requires the same two purchases to move the balance TWICE.
#
# It also stubs the tier claim to always succeed. Without that, the route's own
# refund path (gift_series.py:704, 715) hands the money straight back and the
# double charge becomes invisible -- so the control would be measuring the
# refund path instead of the receipt, which is not the question.

def test_negative_control_removing_the_receipt_charges_twice(pg_app, monkeypatch):
    """Remove the receipt mechanism; the once-only figure MUST double.

    If this FAILS, test 1's green is not evidence: the balance moved once for
    some reason other than the idempotency receipt.
    """
    import gift_series
    import wallet_store_pg

    db, user, password = pg_app["db"], pg_app["user"], pg_app["password"]
    pid = pg_app["player_id"]

    # 1. Remove the replay. `_mutate` looks `_replay` up in module globals at
    #    call time, so patching the module attribute is enough.
    monkeypatch.setattr(wallet_store_pg, "_replay",
                        lambda cur, player_id, key: None)
    monkeypatch.setattr(wallet_store_pg, "_record",
                        lambda cur, player_id, key, response: None)

    # 2. Make the tier claim always succeed AND report no grant, so the route
    #    cannot refund the second purchase (which would hide the double charge)
    #    and the arithmetic below is a clean 2 x cost.
    class _AlwaysOk:
        def __getattr__(self, _name):
            return lambda *a, **k: {"ok": True, "tier_claimed": 1,
                                    "rewards": {}, "completed": False}

    monkeypatch.setattr(gift_series, "gift_series_store", _AlwaysOk())

    before = _balance(db, user, password, pid)
    first = _purchase(pg_app, "monthly")
    second = _purchase(pg_app, "monthly")
    after = _balance(db, user, password, pid)
    moved = _moved(before, after)

    # With the grant stubbed out, one purchase moves exactly MINUS its cost --
    # no `_net_expected` here, which assumes the real tier-1 grant.
    costs = _route_costs()[PURCHASE_SERIES_KEY["monthly"]]
    one = {c: -amount for c, amount in costs.items()}
    twice = {c: one[c] * 2 for c in one}
    print("\n=== NEGATIVE CONTROL (receipt mechanism removed) ===")
    print("monthly status=%s body=%s" % (first.status_code, first.text[:300]))
    print("repeat  status=%s body=%s" % (second.status_code, second.text[:300]))
    print("MOVED with guard removed =%s" % (moved,))
    print("ONE purchase would move   =%s" % (one,))
    print("TWO purchases DO move     =%s" % (twice,))

    assert first.status_code == 200, first.text[:300]
    assert second.status_code == 200, (
        "with the receipt mechanism removed the SECOND purchase was refused "
        "(%s %s), so the control is not measuring a double charge. Test 1's "
        "green is not evidence that the receipt is load-bearing."
        % (second.status_code, second.text[:300]))

    assert moved == twice, (
        "NEGATIVE CONTROL FAILED: with the replay SELECT and the receipt INSERT "
        "both stubbed out, two identical purchases still moved %s rather than "
        "%s. The balance therefore moved once in test 1 for some reason other "
        "than the idempotency receipt, so test 1 does not prove the once-only "
        "guarantee." % (moved, twice))


# ===========================================================================
# 6. WHERE the guarantee ends: the 24-hour receipt expiry
# ===========================================================================
#
# Ported from the pruned sibling test_once_only_payout_postgres_proof.py, which
# measured this and nothing else that survives. Kept because no other test here
# asks it.
#
# ``_record`` writes each receipt with ``NOW() + INTERVAL '24 hours'`` and
# ``_replay`` only honours a row whose ``expires_at > NOW()``, so once-only is a
# 24-HOUR promise, not a permanent one.
#
# This drives the WALLET directly rather than the route, and the reason is
# measured rather than stylistic: through the route, a retry after the tier was
# already granted is refused by the series cooldown, the route calls its own
# refund path, and the NET movement comes back zero. That zero is
# indistinguishable from "the wallet correctly replayed". Asking the wallet the
# same question takes the refund out of the picture.


def test_the_once_only_guarantee_ends_when_the_receipt_expires(pg_app):
    """Pin the real boundary of the guarantee instead of leaving it assumed."""
    from wallet_dispatch import spend

    db, user, password = pg_app["db"], pg_app["user"], pg_app["password"]
    pid = pg_app["player_id"]
    players, wallet = pg_app["players"], pg_app["wallet"]

    def _version() -> int:
        with players.transaction():
            return wallet.get(pid).version

    series_id = PURCHASE_SERIES_KEY["monthly"]
    key = "gift_purchase:%s:%s:monthly:spirit_stone" % (pid, series_id)
    cost = _route_costs()[series_id]["spirit_stone"]

    def _charge():
        with players.transaction():
            return spend(wallet, pid, "spirit_stone", cost, _version(), key)

    before = _balance(db, user, password, pid)
    first = _charge()
    assert first.get("ok"), (
        "the first charge failed, so this half measured nothing: %r" % (first,))
    after_first = _balance(db, user, password, pid)

    # Inside the 24h window: the guarantee must hold and pay nothing.
    replay_inside = _charge()
    after_inside = _balance(db, user, password, pid)
    moved_inside = _moved(after_first, after_inside)

    # Age the receipt out by rewriting its own expiry, on this throwaway
    # database only. Nothing here edits the product.
    aged = _query(db, user, password,
                  "UPDATE idempotency_keys SET expires_at = NOW() - INTERVAL '1 minute' "
                  "WHERE key = %s RETURNING key", (key,))
    expired_rows = [r[0] for r in aged]
    assert expired_rows, (
        "no receipt row was found for %r, so the expiry was never forced and "
        "the second half of this test measured nothing" % (key,))

    replay_outside = _charge()
    after_outside = _balance(db, user, password, pid)
    moved_outside = _moved(after_inside, after_outside)

    paid_again = moved_outside.get("spirit_stone", 0) != 0

    print("\n=== RECEIPT EXPIRY BOUNDARY (wallet asked directly) ===")
    print("rows aged out = %s" % (expired_rows,))
    print("MOVED while the receipt was LIVE = %s  (must be all zero)"
          % (moved_inside,))
    print("MOVED after the receipt EXPIRED  = %s" % (moved_outside,))
    print("=> the once-only guarantee is %s"
          % ("NOT permanent: a retry after the 24h window pays again."
             if paid_again else
             "still enforced after the receipt expired."))

    # Inside the window the guarantee is the claim, and it must hold.
    assert replay_inside.get("_replayed") is True, (
        "the retry inside the 24h window was not answered from the receipt: %r. "
        "The database wallet replayed nothing." % (replay_inside,))
    assert all(v == 0 for v in moved_inside.values()), (
        "ONCE-ONLY FAILS inside the 24h window: a retry moved %s. The database "
        "wallet replayed nothing." % (moved_inside,))

    # Past the window this MEASURES and REPORTS rather than asserting a wish. The
    # receipt expires by design, so the expected result is a second charge; if a
    # future change hardened this, the printed verdict changes and this assertion
    # fails on purpose so nobody can later claim the window was never examined.
    assert paid_again is True, (
        "NOTABLE, NOT A PAYMENT DEFECT: the retry after the receipt expired "
        "moved %s, so the once-only guarantee is STILL enforced past the 24h "
        "window. The expiry clause in wallet_store_pg.py must be re-read before "
        "anyone concludes anything from this line." % (moved_outside,))
