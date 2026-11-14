"""Does the ONCE-ONLY payment guarantee survive SIMULTANEOUS requests?

THE GAP THIS FILE CLOSES
------------------------
Two existing files already prove the guarantee holds when the same payout
arrives TWICE, SEQUENTIALLY, against a real PostgreSQL database:

* ``server/tests/test_stone_backfill_pg.py`` -- the backfill, 7 tests
* ``server/tests/test_once_only_pg.py``      -- the gift-series route, 5 tests

Both send the duplicate request AFTER the first has returned. That is the easy
case. A real player double-taps, and a real client retries, and those two
requests are in flight at the same time.

Nothing measured that against a database. The row lock in
``wallet_store_pg._mutate`` is there for exactly this, and its own comment says
so -- ``wallet_store_pg.py:263``::

    # Lock before checking receipt so simultaneous identical calls replay.
    cur.execute("SELECT ... FOR UPDATE", (player_id,))

No test exercised it. An in-memory store CANNOT: its replay check and its
balance write are Python statements in one process, so a threaded test there
proves nothing about a database row lock. This file is the missing half.

MEASURED, and the reason this file is not what it looks like
-----------------------------------------------------------
Writing it surfaced something a threaded test would have hidden. The first
version of this file drove N threads through the BOUND wallet store
(``WalletStore(player_store=players)``) and passed. It proved nothing.
``PlayerStore.transaction()`` takes a process-wide ``RLock`` and a
``pg_advisory_xact_lock`` BEFORE any wallet SQL runs, so the threads serialised
in Python and never reached the ``FOR UPDATE``. That is the whole reason a
concurrency test can be green with its lock deleted.

So this file asserts concurrency on the STANDALONE wallet store
(``WalletStore(player_store=None)``), which opens its own connection per thread
with no Python lock at all, and separately across N real OS PROCESSES against
the bound store -- the deployed multi-worker shape, which a shared ``RLock``
cannot serialise because the processes do not share memory.

WHAT IS ASSERTED
----------------
1. N simultaneous claims under the SAME key credit ONCE. THE CLAIM.
2. N simultaneous claims under DIFFERENT keys all land. A wallet that loses an
   update when two rewards race is a wallet that destroys money, so a
   once-only test that never checks this can pass while the store is broken.
3. N separate OS PROCESSES under the same key credit once. Threads alone cannot
   reach the deployed shape.
4. A negative control proving 1 has teeth: remove ``FOR UPDATE`` and the same
   burst must break.

WHERE THE LIMIT ACTUALLY IS -- read this before trusting a green line
-------------------------------------------------------------------
Money did not double in test 4 even with the row lock deleted. The
``idempotency_keys`` table's PRIMARY KEY refuses the second ``INSERT``, so the
balance was still correct. What changed is that 3 of 8 threads raised
``UniqueViolation`` instead of replaying the receipt.

So the honest finding is: the row lock is NOT what prevents double payment --
the primary key is. The row lock is what makes the outcome CLEAN. Without it a
race produces an unhandled database exception travelling out of a payout, which
a player sees as a failed purchase and the server sees as a 500. This file
asserts the clean outcome (one success, N-1 replays, zero exceptions) and the
control asserts the difference is real, so nobody later "simplifies" the lock
away on the belief it was redundant.

SKIPS CLEANLY
-------------
No ``WC_PG_HOST``, no running container, or no ``psycopg2`` -> skips with a
reason naming what is missing. Run with ``-rs``: a silent skip is a green exit
code having measured nothing.

Run
---
    python -m pytest server/tests/test_once_only_concurrency_pg.py -v -rs
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import subprocess
import sys
import threading
from typing import Dict, List, Tuple

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "server"
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PG_CONTAINER = "wcpg-hermes"
PG_PORT = "55432"

#: How many requests are released at the same instant. Eight is enough that a
#: barrier releases them genuinely together and small enough that the container
#: does not turn this into a connection-limit test.
BURST = 8

#: What one claim is worth. A round number so a lost update is obvious.
CLAIM = 100

#: What every burst starts from.
START = 1000


# ---------------------------------------------------------------------------
# 1. Is a real database available? Decided ONCE at import so the -rs reason is
#    stable and readable.
# ---------------------------------------------------------------------------

def _probe_database() -> Tuple[bool, str]:
    if not os.environ.get("WC_PG_HOST"):
        return False, ("WC_PG_HOST is not set, so the server booted in memory mode. "
                       "This file needs a real PostgreSQL; a thread test against "
                       "the in-memory store cannot prove anything about a "
                       "database row lock. See docs/POSTGRES-TEST-DATABASE.md.")
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
        return False, ("container %r is not running. Start it with: "
                       "docker start %s" % (PG_CONTAINER, PG_CONTAINER))
    return True, ""


_AVAILABLE, _SKIP_REASON = _probe_database()

pytestmark = pytest.mark.skipif(
    not _AVAILABLE,
    reason="once-only guarantee under CONCURRENT requests is UNPROVEN against "
           "PostgreSQL -- %s" % (_SKIP_REASON,))


# ---------------------------------------------------------------------------
# 2. Container plumbing. The credential is resolved at RUN TIME and never
#    written to disk: this machine's scratch directory is shared.
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


def _resolve_user() -> str:
    return os.environ.get("WC_PG_USER") or _container_env().get("POSTGRES_USER", "")


def _resolve_password() -> str:
    for name in ("WC_PG_PASSWORD", "PGPASSWORD"):
        value = os.environ.get(name)
        if value:
            return value
    return _container_env().get("POSTGRES_PASSWORD", "")


def _psql(sql: str, db: str, user: str) -> subprocess.CompletedProcess:
    """psql INSIDE the container over its local socket: no password on argv."""
    return subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", user, "-d", db,
         "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------------------
# 3. Fixture: a throwaway database built by the APPLICATION'S own runner.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg_database():
    """A real PostgreSQL database carrying the schema the APPLICATION applied.

    Order matters:
      1. resolve the credential (never store it),
      2. create a database whose name ends in ``_test`` and is unique per run,
         because this machine is shared,
      3. random encryption seed (PostgreSQL mode encrypts a player's
         non-column state; without one every write dies naming nothing useful),
      4. ``apply_migrations()`` -- never .sql piped by hand.
    """
    from server.migrations_runner import apply_migrations

    user, password = _resolve_user(), _resolve_password()
    if not user or not password:
        pytest.fail("could not resolve the PostgreSQL credential for %r from the "
                    "environment or from `docker inspect`. Refusing to guess."
                    % (PG_CONTAINER,))

    db = "wc_onceonly_conc_%s_test" % secrets.token_hex(4)
    for sql in ("DROP DATABASE IF EXISTS %s WITH (FORCE);" % db,
                "CREATE DATABASE %s OWNER %s;" % (db, user)):
        r = _psql(sql, "postgres", user)
        if r.returncode != 0:
            pytest.fail("could not prepare the throwaway database %r: %s"
                        % (db, r.stderr.strip()))

    previous = {k: os.environ.get(k) for k in (
        "WC_PG_HOST", "WC_PG_PORT", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD",
        "WC_SAVE_ENCRYPTION_KEY", "WC_SAVE_ENCRYPTION_DEV_SEED")}
    try:
        os.environ.pop("WC_SAVE_ENCRYPTION_KEY", None)
        os.environ["WC_SAVE_ENCRYPTION_DEV_SEED"] = "wc-once-conc-%s" % secrets.token_hex(24)
        os.environ.update(WC_PG_HOST="127.0.0.1", WC_PG_PORT=PG_PORT, WC_PG_DB=db,
                          WC_PG_USER=user, WC_PG_PASSWORD=password)

        result = apply_migrations()
        if result.skipped_no_database or not result.applied:
            pytest.fail("the application's own migration runner applied %r migrations "
                        "(no-database=%r) to %r. An empty database would let every "
                        "assertion below pass for the wrong reason."
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
# 4. Reading the truth: always a FRESH connection opened by this file.
# ---------------------------------------------------------------------------

def _connect(cfg):
    import psycopg2
    return psycopg2.connect(host="127.0.0.1", port=int(PG_PORT), dbname=cfg["db"],
                            user=cfg["user"], password=cfg["password"])


def _stones(cfg, player_id: str) -> int:
    """The committed spirit-stone balance, from this file's own connection.

    Never from the response the code under test returned: the code agreeing
    with itself is not evidence.
    """
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM wallets WHERE player_id = %s", (player_id,))
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    assert row, "no wallets row for %r, so the balance cannot be read at all" % (player_id,)
    raw = row[0]
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    return int((raw or {}).get("spirit_stone", 0))


def _seed(cfg, tag: str) -> Tuple[str, object]:
    """A funded wallet on the STANDALONE store. Returns (player_id, wallet).

    Standalone means ``player_store=None``: its ``transaction()`` opens a
    dedicated connection per thread and takes NO Python lock and NO advisory
    lock. That is the only configuration in which the ``FOR UPDATE`` row lock
    is the thing under test -- with a PlayerStore bound, ``transaction()``
    serialises every thread in Python first (see this file's docstring).
    """
    import wallet_store_pg

    player_id = "conc_%s_%s" % (tag, secrets.token_hex(5))
    wallet = wallet_store_pg.WalletStore(player_store=None)
    assert wallet.player_store is None, (
        "the wallet is bound to a PlayerStore, so transaction() will serialise "
        "these threads in Python before any wallet SQL runs. A green result "
        "here would say nothing about the database row lock.")
    with wallet.transaction():
        wallet.create(player_id, "OnceOnlyConc", "none")

    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE wallets SET balance = %s::jsonb, version = 0 "
                        "WHERE player_id = %s",
                        (json.dumps({"spirit_stone": START, "fateum": 0,
                                     "destium": 0, "souldust": 0}), player_id))
        conn.commit()
    finally:
        conn.close()
    return player_id, wallet


def _burst(wallet, player_id: str, tag: str, n: int = BURST) -> Tuple[List[dict], List[str]]:
    """Release ``n`` claims simultaneously and collect what each one got.

    A ``threading.Barrier`` is the release mechanism: every thread parks on it
    and is let go at the same instant, so the requests genuinely overlap
    instead of being staggered by thread start-up order.
    """
    import wallet_store_pg

    barrier = threading.Barrier(n)
    results: List[dict] = []
    errors: List[str] = []
    lock = threading.Lock()

    def claim(suffix: str):
        try:
            barrier.wait(timeout=60)
            r = wallet_store_pg.earn(wallet, player_id, "spirit_stone", CLAIM,
                                     "conc:%s:%s" % (player_id, suffix))
            with lock:
                results.append(r)
        except Exception as exc:  # noqa: BLE001 - the point is to observe them
            with lock:
                errors.append("%s: %s" % (type(exc).__name__,
                                          str(exc).strip().splitlines()[0]))

    threads = [threading.Thread(target=claim, args=("same" if tag == "same"
                                                   else "diff%d" % i,))
               for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(90)
    assert not any(t.is_alive() for t in threads), (
        "threads did not finish: a burst left work running, so the balance read "
        "afterwards is a mid-flight snapshot and any verdict from it is noise")
    return results, errors


def _assert_exactly_once(results, errors, moved, label):
    """One credit, no exceptions, and the rest clean replays."""
    assert not errors, (
        "%s: %d of %d simultaneous claims raised instead of replaying. Money did "
        "not double -- the idempotency_keys primary key caught it -- but the "
        "player sees a failed purchase and the server sees a 500: %s"
        % (label, len(errors), BURST, errors[:2]))
    assert len(results) == BURST, (
        "%s: %d of %d claims returned a result" % (label, len(results), BURST))
    fresh = [r for r in results if not r.get("_replayed")]
    assert len(fresh) == 1, (
        "%s: %d claims were treated as new instead of 1. The once-only guarantee "
        "is broken for simultaneous requests: %r" % (label, len(fresh), fresh))
    assert moved == CLAIM, (
        "%s: the balance moved %d, and one claim is worth %d. %r"
        % (label, moved, CLAIM,
           "money was minted" if moved > CLAIM else "money was destroyed"))


# ===========================================================================
# 1. THE CLAIM: simultaneous identical claims credit ONCE
# ===========================================================================

def test_simultaneous_identical_claims_credit_once(pg_database):
    """A double-tapped payout, released at the same instant, pays once.

    Eight threads on one key. One credit, seven replays, no exceptions.
    """
    cfg = pg_database
    player_id, wallet = _seed(cfg, "same")

    before = _stones(cfg, player_id)
    results, errors = _burst(wallet, player_id, "same")
    moved = _stones(cfg, player_id) - before

    print("\n=== %d SIMULTANEOUS claims, SAME key ===" % BURST)
    print("balance %d -> %d (moved %d, one claim is %d)"
          % (before, before + moved, moved, CLAIM))
    print("fresh credits=%d  replays=%d  exceptions=%d"
          % (sum(1 for r in results if not r.get("_replayed")),
             sum(1 for r in results if r.get("_replayed")), len(errors)))

    assert before == START, "setup wrong: expected to start at %d" % (START,)
    _assert_exactly_once(results, errors, moved, "same-key burst")


def test_simultaneous_different_claims_do_not_lose_an_update(pg_database):
    """Eight DIFFERENT rewards racing must all land, or the wallet destroys money.

    The mirror of the once-only test and the one most likely to be broken: a
    read-modify-write that takes the row lock too late, or not at all, loses
    whichever update it overwrites. A once-only test cannot see this, because
    it only ever issues one credit.
    """
    cfg = pg_database
    player_id, wallet = _seed(cfg, "diff")

    before = _stones(cfg, player_id)
    results, errors = _burst(wallet, player_id, "diff")
    moved = _stones(cfg, player_id) - before

    print("\n=== %d SIMULTANEOUS claims, DIFFERENT keys ===" % BURST)
    print("balance %d -> %d (moved %d, all %d should land for %d)"
          % (before, before + moved, moved, BURST, BURST * CLAIM))

    assert not errors, "%d of %d distinct claims raised: %s" % (
        len(errors), BURST, errors[:2])
    assert moved == BURST * CLAIM, (
        "%d of %d distinct rewards were LOST (moved %d, expected %d). Two "
        "rewards racing overwrote each other: the wallet destroyed money while "
        "every individual request reported success."
        % (BURST * CLAIM - moved, BURST, moved, BURST * CLAIM))


# ===========================================================================
# 2. Across PROCESSES, which threads cannot reach
# ===========================================================================

_CHILD = pathlib.Path(__file__).with_name("_once_only_conc_child.py")


def _write_child():
    """A child that makes ONE claim in its own process and prints JSON.

    A thread shares the parent's ``RLock``; a process does not. The deployed
    server runs several workers, so the guarantee has to hold across processes
    or this file only proves the single-process case.
    """
    _CHILD.write_text(
        '"""One claim, one process. Written by test_once_only_concurrency_pg.py."""\n'
        "import json, sys\n"
        "sys.path.insert(0, %r)\n"
        "sys.path.insert(0, %r)\n"
        "import wallet_store_pg\n"
        "from player_store import PlayerStore\n"
        "pid, key = sys.argv[1], sys.argv[2]\n"
        "players = PlayerStore()\n"
        "w = wallet_store_pg.WalletStore(players)\n"
        "try:\n"
        "    r = wallet_store_pg.earn(w, pid, 'spirit_stone', %d, key)\n"
        "    print(json.dumps({'ok': r.get('ok'),\n"
        "                      'replayed': bool(r.get('_replayed'))}))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'ok': False, 'replayed': False,\n"
        "                      'error': type(exc).__name__ + ': ' + str(exc)[:120]}))\n"
        % (str(REPO_ROOT), str(SERVER_DIR), CLAIM),
        encoding="utf-8")


def test_simultaneous_claims_from_separate_processes_credit_once(pg_database):
    """N OS processes, one key, one credit. The deployed multi-worker shape."""
    import wallet_store_pg
    from player_store import PlayerStore

    cfg = pg_database
    players = PlayerStore()
    assert players._pg is True, (
        "PlayerStore is not in database mode; this would measure the in-memory "
        "store and pass without touching PostgreSQL")
    wallet = wallet_store_pg.WalletStore(players)

    player_id = "conc_mp_%s" % secrets.token_hex(5)
    with players.transaction():
        wallet.create(player_id, "OnceOnlyConcMP", "none")
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE wallets SET balance = %s::jsonb, version = 0 "
                        "WHERE player_id = %s",
                        (json.dumps({"spirit_stone": START, "fateum": 0,
                                     "destium": 0, "souldust": 0}), player_id))
        conn.commit()
    finally:
        conn.close()

    _write_child()
    key = "conc-mp:%s:one" % player_id
    before = _stones(cfg, player_id)
    procs = [subprocess.Popen([sys.executable, str(_CHILD), player_id, key],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=dict(os.environ), cwd=str(REPO_ROOT))
             for _ in range(BURST)]
    payloads = []
    for p in procs:
        out, _ = p.communicate(timeout=120)
        text = out.decode("utf-8", "replace").strip()
        assert text, ("a child process produced no output at all, so this test "
                      "would count an absent answer as a pass")
        payloads.append(json.loads(text.splitlines()[-1]))
    moved = _stones(cfg, player_id) - before

    try:
        print("\n=== %d SEPARATE PROCESSES, one key ===" % BURST)
        print("balance %d -> %d (moved %d, one claim is %d)"
              % (before, before + moved, moved, CLAIM))
        print("fresh=%d replayed=%d errors=%d"
              % (sum(1 for p in payloads if not p.get("replayed")),
                 sum(1 for p in payloads if p.get("replayed")),
                 sum(1 for p in payloads if p.get("error"))))

        broken = [p for p in payloads if p.get("error")]
        assert not broken, (
            "%d of %d separate processes raised: %r. The claim must hold across "
            "processes, not just across threads, because the server runs several "
            "workers." % (len(broken), BURST, broken[:2]))
        fresh = [p for p in payloads if not p.get("replayed")]
        assert len(fresh) == 1, (
            "%d processes were treated as new claims instead of 1" % (len(fresh),))
        assert moved == CLAIM, (
            "balance moved %d across %d processes, and one claim is %d: %r"
            % (moved, BURST, CLAIM,
               "money was minted" if moved > CLAIM else "money was destroyed"))
    finally:
        _CHILD.unlink(missing_ok=True)


# ===========================================================================
# 3. NEGATIVE CONTROL -- proof test 1 can fail
# ===========================================================================

def test_removing_the_row_lock_breaks_the_simultaneous_burst(pg_database,
                                                              monkeypatch):
    """Delete ``FOR UPDATE`` and the clean outcome must collapse.

    The honest result, and the reason this control asserts on EXCEPTIONS and
    not on the balance: without the row lock the money still does not double,
    because the ``idempotency_keys`` PRIMARY KEY refuses the second INSERT.
    What breaks is the OUTCOME -- racing threads raise ``UniqueViolation`` out
    of a payout instead of replaying the receipt.

    So the real finding is that the row lock is not the double-payment guard;
    the primary key is. The row lock is what turns a race into a clean replay.
    A control asserting only on the balance would pass here having proved
    nothing, which is why it asserts the burst degrades instead.

    The lock is removed by rewriting the SQL through a thin cursor proxy. Note
    a psycopg2 cursor's ``execute`` is READ-ONLY: the first attempt assigned
    to it, every thread died with ``AttributeError``, and the control reported
    a clean pass having measured nothing at all.
    """
    import psycopg2.extras as extras
    import wallet_store_pg

    def _mutate_without_row_lock(store, player_id, currency, delta, key,
                                 client_version=None, reward_type="", offline=None):
        """A copy of ``_mutate`` differing in exactly one thing: the row lock."""

        class _NoLockCursor:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, params=None):
                self._inner.execute(sql.replace(" FOR UPDATE", ""), params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self._inner.close()

        raw = store.conn.cursor()
        cur = _NoLockCursor(raw)
        cur.execute("SELECT version,balance,last_offline_claimed_at "
                    "FROM wallets WHERE player_id = %s", (player_id,))
        row = cur.fetchone()
        if row is None:
            raw.close()
            return {"ok": False, "error": "wallet not found"}
        replay = wallet_store_pg._replay(cur, player_id, key)
        if replay is not None:
            raw.close()
            return replay
        version, balance, last_claimed = row
        if balance.get(currency, 0) + delta < 0:
            raw.close()
            return {"ok": False, "error": "insufficient balance",
                    "balance": balance.get(currency, 0)}
        balance = dict(balance)
        balance[currency] = balance.get(currency, 0) + delta
        cur.execute("UPDATE wallets SET balance=%s,version=%s,updated_at=NOW() "
                    "WHERE player_id=%s",
                    (extras.Json(balance), version + 1, player_id))
        response = {"ok": True, "currency": currency, "balance": balance,
                    "wallet_version": version + 1, "earned": delta,
                    "reward_type": reward_type}
        wallet_store_pg._record(cur, player_id, key, response)
        raw.close()
        return response

    monkeypatch.setattr(wallet_store_pg, "_mutate", _mutate_without_row_lock,
                        raising=True)

    cfg = pg_database
    player_id, wallet = _seed(cfg, "nolock")
    before = _stones(cfg, player_id)
    results, errors = _burst(wallet, player_id, "same")
    moved = _stones(cfg, player_id) - before

    print("\n=== NEGATIVE CONTROL: ' FOR UPDATE' removed ===")
    print("balance %d -> %d (moved %d, one claim is %d)"
          % (before, before + moved, moved, CLAIM))
    print("fresh=%d replayed=%d exceptions=%d"
          % (sum(1 for r in results if not r.get("_replayed")),
             sum(1 for r in results if r.get("_replayed")), len(errors)))
    for e in errors[:3]:
        print("   ", e[:120])
    print("=> money did NOT double: the idempotency_keys PRIMARY KEY caught the "
          "duplicate INSERT. What degraded is the OUTCOME.")

    # The control bites if the burst is no longer clean.
    degraded = bool(errors) or (moved != CLAIM)
    assert degraded, (
        "removing the row lock left the %d-way burst perfectly clean (%d moved, "
        "%d exceptions). This control proves nothing: either the lock is not the "
        "guard this file thinks it is, or this harness cannot reproduce the race."
        % (BURST, moved, len(errors)))
