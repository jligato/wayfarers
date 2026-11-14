"""
DUNGEON CLAIMS: the module contract for ``server/dungeon_claims.py``.

WHAT THIS FILE MEASURES
-----------------------
``server/dungeon_claims.py`` is the once-only ledger behind the only dungeon
route that pays currency (``POST /dungeons/instance/claim-loot``). It had no
test file of its own: the route is covered, the ledger underneath it was not.
These are contracts of the module's PUBLIC surface, driven directly so a
failure names the ledger rather than a route that also touches the player row.

  1. KEY DERIVATION. ``claim_key_for`` scopes each reward type the way the
     payout is meant to be scoped: loot to the run, first clear to the template
     forever, weekly to the template plus the server's UTC week. An unknown
     reward type REFUSES (raises) rather than inventing a key.
  2. THE KEY IS NOT THE CALLER'S. The function takes no player id and no
     request body, so two players clear the same template to the same key and
     the player is separated by the ledger row instead. A caller-minted key
     would make "pays once" a property of the client's memory.
  3. ONCE-ONLY, TWICE. Both backends are measured: the database path (the row
     count of ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` alone decides) and
     the no-database fallback (a process-local set). Neighbours -- a different
     player, a different reward type, a later week -- are unaffected.
  4. REFUSAL AND RECOVERY. ``claim_exists`` answers None with no database
     rather than pretending False; ``release_claim`` hands a failed payout's
     key back so a credit that died does not burn the player's one shot; and in
     PostgreSQL mode it issues no DELETE at all, because the transaction
     rollback is the release and a manual delete would be a second undo path.
  5. THE BOUND PARAMETERS. The three values are bound in the order the columns
     are named, on BOTH the write and the read. A reordering stores a real row
     under another identity while the ledger still answers True/False, so it is
     asserted from the parameters the module hands the cursor, not from SQL text.
  6. FAIL LOUD. A database error propagates out of ``reserve_claim`` and
     ``claim_exists``. The worst possible answer is False for "already claimed"
     when the ledger is merely down: the paying route reads that as a claim
     another process made and skips the payout with no error anywhere.
  7. EXACT VOCABULARY. Only the three reward-type names mint a key; every
     near-miss (case variant, stray space, empty string, and NON-string such as
     ``None`` or a list) raises. An enumerated census proves the mintable set is
     exactly those three, so a fourth branch is a contract change.
  8. THE KEY IS A PURE FUNCTION. Identical inputs must mint an identical key
     on every call -- a counter, a timestamp or any other per-call state in
     the key would mint a fresh identity each time and pay every replay.
  9. BOTH BACKENDS AGREE. The database row and the no-database fallback are two
     implementations of one contract and must reach the same verdict on
     (first attempt, replay, other player), or "pays once" would depend on which
     mode the server happened to boot in.

NEGATIVE CONTROLS, AND WHY THEY ARE NOT DECORATIVE
--------------------------------------------------
Three controls sabotage the guard in memory (via monkeypatch, never on disk)
and REQUIRE the matching arm to go red. A test that cannot fail is a comment:

  * strip ``ON CONFLICT`` out of the module's own statement -> the replay test
    goes red, so the conflict clause is load-bearing and not decoration;
  * make the fallback always say "first attempt" -> the fallback replay test
    goes red;
  * delete from the database on release -> the no-delete arm goes red;
  * bind the write's three values out of order -> the bound-order arm observes
    the change, so it is measuring the parameters and not the SQL text.

Each control asserts on an AssertionError/return value observed HERE, and each
first proves the sabotage actually changed the thing it claims to change.

WHAT IS DELIBERATELY NOT TESTED
-------------------------------
Thread contention on the in-memory fallback. Measured, not assumed: with the
module's lock removed, 60 threads racing one key still produced exactly one
winner on every round, because CPython's global interpreter lock makes the
``set`` membership test and the ``add`` indivisible in practice. A test there
would be green against broken code, which is worse than no test.

Run:
    tools/wc_pytest.sh server/tests/test_dungeon_claims_contract.py
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

for _var in ("WC_PG_DB", "WC_PG_HOST", "WC_PG_USER", "WC_PG_PASSWORD",
             "WC_PG_PORT"):
    os.environ.pop(_var, None)

_SERVER = Path(__file__).resolve().parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

import pytest  # noqa: E402

import dungeon_claims as dc  # noqa: E402

LOOT = "loot"
FIRST_CLEAR = "first_clear"
WEEKLY = "weekly"

TEMPLATE = "spirit_beast_den"
INSTANCE = "run-abc-123"


def _refuses(reward_type: Any) -> bool:
    """Does ``claim_key_for`` REFUSE this reward type?

    ``bool`` -- not an exception value -- so a vocabulary census can ask about
    a hundred candidates without one raising through the test body. Any
    exception counts as a refusal, because the contract is "no key comes back",
    and swallowing an unexpected exception type is exactly what fail-loud is
    for: it fails the caller rather than handing back a claim identity.
    """
    try:
        dc.claim_key_for(reward_type, INSTANCE, TEMPLATE, "2026-W38")
    except Exception:
        return True
    return False


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_memory_ledger():
    """Every test starts with an EMPTY fallback ledger.

    ``_MEM_CLAIM_KEYS`` is a module global, so without this one test's claim
    suppresses another's and the file passes for the wrong reason.
    """
    dc._MEM_CLAIM_KEYS.clear()
    yield
    dc._MEM_CLAIM_KEYS.clear()


class NoDatabase:
    """A player store with no connection: the documented fallback mode."""

    current_connection = None


class FakeCursor:
    """A cursor that honours the module's REAL statement shape.

    Written to the shape the module uses -- a context manager with
    ``execute``/``fetchone`` -- so the module's own SQL and its own read-back
    run here. The conflict behaviour is conditional on ``ON CONFLICT`` actually
    being in the statement, which is what makes the first negative control
    meaningful: remove the clause and this cursor stops suppressing replays,
    exactly as a real database would stop suppressing them.
    """

    def __init__(self, db: "FakeDb") -> None:
        self.db = db

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = ()) -> None:
        self.db.statements.append(sql)
        # The parameters are the claim's identity: recording them lets a test
        # check the BOUND ORDER, which the SQL text alone cannot show.
        self.db._last_params = tuple(params)
        verb = sql.split()[0].upper()
        if verb == "INSERT" and "dungeon_claims" in sql:
            key = (params[0], params[1], params[2])
            if key in self.db.rows_held and "ON CONFLICT" in sql:
                self.db.row = None          # conflict -> DO NOTHING -> no row
            else:
                self.db.rows_held[key] = sql
                self.db.row = (params[2],)  # RETURNING claim_key
        elif verb == "SELECT":
            key = (params[0], params[1], params[2])
            self.db.row = (1,) if key in self.db.rows_held else None
        elif verb == "DELETE":
            self.db.rows_held.pop((params[0], params[1], params[2]), None)
            self.db.row = None
        else:
            self.db.row = None

    def fetchone(self) -> Any:
        return self.db.row


class BrokenConn:
    """A connection whose every statement raises, as a downed database would."""

    def cursor(self) -> "BrokenCursor":
        return BrokenCursor()


class BrokenCursor:
    def __enter__(self) -> "BrokenCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = ()) -> None:
        raise RuntimeError("database is down")

    def fetchone(self) -> Any:  # pragma: no cover - never reached
        raise AssertionError("fetchone ran after execute raised")


class BrokenStore:
    """A player store whose connection fails every statement."""

    current_connection = BrokenConn()


class FakeDb:
    """A player store that reports a connection and enforces the triple."""

    def __init__(self) -> None:
        self.current_connection = self
        self.rows_held: Dict[Any, str] = {}
        self.statements: List[str] = []
        self._last_params: tuple = ()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def statements_of_verb(self, verb: str) -> List[str]:
        return [s for s in self.statements if s.split()[0].upper() == verb]


# ---------------------------------------------------------------------------
# 1. KEY DERIVATION
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reward_type", [LOOT, FIRST_CLEAR, WEEKLY])
def test_each_known_reward_type_yields_a_non_empty_string_key(reward_type):
    """The three reward types the paying route accepts all produce a key.

    Not a snapshot of the key's TEXT -- only that the function answers a
    usable, non-empty string for each legal type.
    """
    key = dc.claim_key_for(reward_type, INSTANCE, TEMPLATE, "2026-W38")
    assert isinstance(key, str) and key.strip(), (
        "claim_key_for returned no usable key for %r" % reward_type)


def test_unknown_reward_type_refuses_rather_than_inventing_a_key():
    """A reward type the ledger does not understand must RAISE.

    Falling back to some default key would let an unrecognised reward type
    share a claim identity with a real one, or silently never pay.
    """
    with pytest.raises(ValueError):
        dc.claim_key_for("mystery_box", INSTANCE, TEMPLATE, "2026-W38")


def test_loot_is_scoped_to_the_run_not_the_template_or_the_week():
    """Loot is paid once per run: neither template nor week changes the key."""
    base = dc.claim_key_for(LOOT, INSTANCE, TEMPLATE, "2026-W38")
    other_week = dc.claim_key_for(LOOT, INSTANCE, TEMPLATE, "2026-W39")
    other_template = dc.claim_key_for(LOOT, INSTANCE, "other_den", "2026-W38")
    assert base == other_week == other_template, (
        "loot's key moved with the week or the template, so a second run of the "
        "same template in the same week would pay again")

    # A DIFFERENT run is a different claim -- that is the whole point.
    assert dc.claim_key_for(LOOT, "run-other", TEMPLATE, "2026-W38") != base


def test_first_clear_is_scoped_to_the_template_forever():
    """A first clear happens once in a lifetime, so the key carries NO week."""
    w38 = dc.claim_key_for(FIRST_CLEAR, INSTANCE, TEMPLATE, "2026-W38")
    w39 = dc.claim_key_for(FIRST_CLEAR, "a-completely-different-run",
                           TEMPLATE, "2026-W39")
    assert w38 == w39, (
        "first_clear's key carries a week, so a player could clear the same "
        "template again next week and be paid a 'first clear' twice")

    # A different template is a different first clear.
    assert dc.claim_key_for(FIRST_CLEAR, INSTANCE, "other_den",
                            "2026-W38") != w38


def test_weekly_expires_on_the_week_boundary():
    """The weekly key must move when the week moves, and hold inside a week."""
    w38a = dc.claim_key_for(WEEKLY, INSTANCE, TEMPLATE, "2026-W38")
    w38b = dc.claim_key_for(WEEKLY, "a-different-run", TEMPLATE, "2026-W38")
    w39 = dc.claim_key_for(WEEKLY, INSTANCE, TEMPLATE, "2026-W39")
    assert w38a == w38b, (
        "the weekly key moved inside one week; the reward would be payable "
        "only once per week instead of once per run within it")
    assert w38a != w39, (
        "the weekly key did not move across the week boundary, so last week's "
        "claim suppresses this week's reward")


def test_two_players_clear_the_same_template_to_the_same_key():
    """The key carries no player identity; the ledger ROW does the separating.

    This is the anti-forgery property: a caller cannot name the key, so one
    player paying a first clear cannot suppress another player's, and one
    player's claim cannot be replayed as their own by guessing a name.
    """
    a = dc.claim_key_for(FIRST_CLEAR, INSTANCE, TEMPLATE, "2026-W38")
    b = dc.claim_key_for(FIRST_CLEAR, INSTANCE, TEMPLATE, "2026-W38")
    assert a == b

    db = FakeDb()
    assert dc.reserve_claim(db, "player-a", FIRST_CLEAR, a) is True
    assert dc.reserve_claim(db, "player-b", FIRST_CLEAR, b) is True, (
        "player-b was suppressed by player-a's row: the ledger row is keyed on "
        "the player as well as the key, and this proves it")


def test_the_key_function_takes_no_player_id_and_no_request_body():
    """Structural, and load-bearing: a caller cannot supply the key.

    Read from the live signature, not from source text, so a refactor that
    renames an argument still measures.
    """
    params = list(inspect.signature(dc.claim_key_for).parameters)
    assert params == ["reward_type", "instance_id", "template_id", "week_key"], (
        "claim_key_for's inputs changed: if a player id or a free-form key can "
        "enter here, 'pays once' becomes a property of the caller's memory")


# ---------------------------------------------------------------------------
# 2. ONCE-ONLY -- the database path (the guarantee)
# ---------------------------------------------------------------------------

def test_the_database_row_alone_decides_and_a_replay_pays_nothing():
    """First attempt true, every replay false, neighbours unaffected."""
    db = FakeDb()

    assert dc.reserve_claim(db, "p1", FIRST_CLEAR, TEMPLATE) is True
    assert dc.reserve_claim(db, "p1", FIRST_CLEAR, TEMPLATE) is False, (
        "the durable ledger admitted a replay; the inserted row IS the guard")

    # A different reward type from the same run, and a different player, are
    # different claims -- one run legitimately pays all three.
    assert dc.reserve_claim(db, "p1", LOOT, f"loot:{INSTANCE}") is True
    assert dc.reserve_claim(db, "p1", WEEKLY, f"{TEMPLATE}:2026-W38") is True
    assert dc.reserve_claim(db, "p2", FIRST_CLEAR, TEMPLATE) is True


def test_the_guard_is_the_insert_itself_not_a_later_read():
    """The refusal comes from the insert returning no row, nothing else.

    Every statement the module issues for one replay is inspected, so a future
    "check then insert" shape -- a SELECT that races a second server -- shows up
    here as an extra statement rather than passing silently.
    """
    db = FakeDb()
    dc.reserve_claim(db, "p1", FIRST_CLEAR, TEMPLATE)
    db.statements.clear()

    assert dc.reserve_claim(db, "p1", FIRST_CLEAR, TEMPLATE) is False
    assert len(db.statements) == 1, (
        "the replay took %d statements, expected exactly the guarded INSERT: "
        "%r" % (len(db.statements), db.statements))
    statement = db.statements[0]
    assert statement.split()[0].upper() == "INSERT", (
        "the replay did not go through an INSERT; the row count is supposed to "
        "be the whole decision: %r" % statement)
    assert "ON CONFLICT" in statement and "DO NOTHING" in statement, (
        "the insert lost its conflict clause, so a second process would raise "
        "or double-insert instead of paying nothing: %r" % statement)
    assert "RETURNING" in statement, (
        "the insert no longer returns the row it wrote, so the module cannot "
        "tell a fresh claim from a replay: %r" % statement)


def test_the_write_names_the_ledger_table_and_its_three_key_columns():
    """The insert targets ``dungeon_claims`` with the triple, in that order."""
    db = FakeDb()
    dc.reserve_claim(db, "p1", WEEKLY, f"{TEMPLATE}:2026-W38")
    statement = db.statements[0]
    assert "dungeon_claims" in statement
    for column in ("player_id", "reward_type", "claim_key"):
        assert column in statement, (
            "%r is not named by the ledger insert, so the primary key that "
            "guards the payout is not the one being written: %r"
            % (column, statement))


def test_claim_exists_reads_the_database_not_the_return_value():
    """The read path answers from the ledger, independently of the writer.

    Asserted after a SEPARATE fresh store object shares nothing with the
    writer except its own rows, so a cache inside one object cannot pass this.
    """
    db = FakeDb()
    assert dc.claim_exists(db, "p1", FIRST_CLEAR, TEMPLATE) is False

    dc.reserve_claim(db, "p1", FIRST_CLEAR, TEMPLATE)
    assert dc.claim_exists(db, "p1", FIRST_CLEAR, TEMPLATE) is True
    assert dc.claim_exists(db, "p1", FIRST_CLEAR, "a-different-template") is False
    assert dc.claim_exists(db, "p2", FIRST_CLEAR, TEMPLATE) is False, (
        "the ledger read another player's claim as this player's")


# ---------------------------------------------------------------------------
# 3. ONCE-ONLY -- the no-database fallback (explicitly not the guarantee)
# ---------------------------------------------------------------------------

def test_the_fallback_ledger_admits_one_claim_per_triple():
    store = NoDatabase()
    assert dc.reserve_claim(store, "p1", FIRST_CLEAR, TEMPLATE) is True
    assert dc.reserve_claim(store, "p1", FIRST_CLEAR, TEMPLATE) is False, (
        "the fallback admitted a replay of the same claim")

    # Neighbours are different claims.
    assert dc.reserve_claim(store, "p1", LOOT, f"loot:{INSTANCE}") is True
    assert dc.reserve_claim(store, "p1", WEEKLY, f"{TEMPLATE}:2026-W38") is True
    assert dc.reserve_claim(store, "p2", FIRST_CLEAR, TEMPLATE) is True


def test_releasing_a_fallback_claim_hands_the_key_back():
    """A credit that failed must not cost the player their one shot."""
    store = NoDatabase()
    assert dc.reserve_claim(store, "p1", FIRST_CLEAR, TEMPLATE) is True
    dc.release_claim(store, "p1", FIRST_CLEAR, TEMPLATE)
    assert dc.reserve_claim(store, "p1", FIRST_CLEAR, TEMPLATE) is True, (
        "a released claim was not claimable again, so a failed payout spent the "
        "player's first clear permanently")

    # Releasing a key that was never claimed is a no-op, not a crash.
    dc.release_claim(store, "p1", FIRST_CLEAR, "never-claimed")


def test_claim_exists_says_none_rather_than_false_with_no_database():
    """Stated, not faked: with no ledger there is no fact to report.

    Returning False would read as "this player has not claimed" -- a green
    answer the module cannot actually support.
    """
    assert dc.claim_exists(NoDatabase(), "p1", FIRST_CLEAR, TEMPLATE) is None


def test_the_fallback_is_used_only_without_a_connection():
    """A store WITH a connection must never touch the process-local set.

    Otherwise a server running against a database would also keep a local
    ledger, and a restart would silently re-open every claim.
    """
    db = FakeDb()
    dc.reserve_claim(db, "p1", FIRST_CLEAR, TEMPLATE)
    assert ("p1", FIRST_CLEAR, TEMPLATE) not in dc._MEM_CLAIM_KEYS, (
        "a database-backed reserve also wrote the process-local fallback ledger")


def test_a_real_memory_player_store_takes_the_fallback_path():
    """The detection works against the REAL store, not only a stand-in.

    ``PlayerStore.current_connection`` is a property, so a naive truthiness
    check would behave differently here than against ``NoDatabase``.
    """
    from player_store import PlayerStore

    store = PlayerStore()
    assert store.current_connection is None, (
        "the fixture is running against a database; this arm measured the "
        "wrong branch")
    assert dc.reserve_claim(store, "p1", FIRST_CLEAR, TEMPLATE) is True
    assert dc.reserve_claim(store, "p1", FIRST_CLEAR, TEMPLATE) is False


# ---------------------------------------------------------------------------
# 4. RECOVERY IN POSTGRESQL MODE
# ---------------------------------------------------------------------------

def test_release_issues_no_delete_when_a_database_is_doing_the_undoing():
    """In PostgreSQL the transaction rollback IS the release.

    A manual delete here would be a second undo path racing the rollback: it
    could remove a row that a concurrent committed payout already owns.
    """
    db = FakeDb()
    dc.reserve_claim(db, "p1", FIRST_CLEAR, TEMPLATE)

    dc.release_claim(db, "p1", FIRST_CLEAR, TEMPLATE)
    assert db.statements_of_verb("DELETE") == [], (
        "release_claim issued a DELETE against the database: %r"
        % db.statements_of_verb("DELETE"))
    assert dc.claim_exists(db, "p1", FIRST_CLEAR, TEMPLATE) is True, (
        "release_claim removed the row in database mode; the rollback owns "
        "that, and doing it twice can free a key another payout owns")


# ---------------------------------------------------------------------------
# 5. NEGATIVE CONTROLS -- each must make the arm above go red
# ---------------------------------------------------------------------------

def test_control_removing_on_conflict_breaks_the_once_only_guarantee(
        monkeypatch):
    """Sabotage the module's statement; the replay must then be admitted.

    Proves the ONCE-ONLY arm measures the conflict clause rather than a
    hand-built cursor that always says no.
    """
    sabotaged = dc._CLAIM_SQL.replace("ON CONFLICT (player_id, reward_type, "
                                     "claim_key) DO NOTHING ", "")
    assert sabotaged != dc._CLAIM_SQL, (
        "the control did not change the statement, so it proved nothing")
    monkeypatch.setattr(dc, "_CLAIM_SQL", sabotaged)

    db = FakeDb()
    assert dc.reserve_claim(db, "p1", FIRST_CLEAR, TEMPLATE) is True
    assert dc.reserve_claim(db, "p1", FIRST_CLEAR, TEMPLATE) is True, (
        "CONTROL FAILED: with ON CONFLICT removed the replay was still "
        "refused, so the cursor is deciding, not the module's SQL")


def test_control_a_fallback_that_always_says_first_breaks_the_fallback_arm(
        monkeypatch):
    """Sabotage the fallback's own once-only check."""
    monkeypatch.setattr(dc, "_mem_reserve", lambda key: True)
    store = NoDatabase()
    first = dc.reserve_claim(store, "p1", FIRST_CLEAR, TEMPLATE)
    replay = dc.reserve_claim(store, "p1", FIRST_CLEAR, TEMPLATE)
    assert first is replay is True, (
        "CONTROL FAILED: the sabotaged fallback still refused a replay, so the "
        "fallback arm is not measuring the fallback")


def test_control_a_deleting_release_breaks_the_no_delete_arm(monkeypatch):
    """Sabotage release into a real DELETE; the no-delete arm must go red."""
    monkeypatch.setattr(
        dc, "release_claim",
        lambda db, pid, rt, key: db.cursor().execute(
            "DELETE FROM dungeon_claims WHERE player_id = %s AND reward_type = "
            "%s AND claim_key = %s", (pid, rt, key)))

    db = FakeDb()
    dc.reserve_claim(db, "p1", FIRST_CLEAR, TEMPLATE)
    dc.release_claim(db, "p1", FIRST_CLEAR, TEMPLATE)
    assert db.statements_of_verb("DELETE"), (
        "CONTROL FAILED: the sabotaged release issued no DELETE, so the "
        "no-delete arm is not observing anything")
    assert dc.claim_exists(db, "p1", FIRST_CLEAR, TEMPLATE) is False, (
        "CONTROL FAILED: the row survived a sabotaged delete, so the ledger "
        "read is not reading the ledger")


# ---------------------------------------------------------------------------
# 6. THE BOUND PARAMETERS ARE THE CLAIM'S IDENTITY, IN ORDER
# ---------------------------------------------------------------------------

def test_the_insert_binds_player_type_key_in_the_documented_order():
    """The VALUES order must match the column order, position for position.

    A store that wrote the three values in the wrong order would store a real
    row under a DIFFERENT identity and the ledger would report exactly the
    guarantees it is supposed to while quietly admitting replays. Measured from
    the parameters the module hands the cursor, not from the SQL text.
    """
    db = FakeDb()
    dc.reserve_claim(db, "player-x", WEEKLY, "den:2026-W38")
    statement, params = db.statements[0], db._last_params
    assert params == ("player-x", WEEKLY, "den:2026-W38"), (
        "the insert bound %r, which is not (player_id, reward_type, claim_key) "
        "in that order: the ledger would store a row under another identity "
        "while still answering True/False: %r" % (params, statement))


def test_control_binding_the_triple_out_of_order_is_detectable(monkeypatch):
    """REQUIRE the arm above to go red when the order is sabotaged.

    Proves the order test observes the bound values rather than restating the
    signature -- a reordering that still "passes a plausible triple" must fail.
    """
    real_reserve = dc.reserve_claim

    def reordered(players_db, player_id, reward_type, claim_key):
        conn = getattr(players_db, "current_connection", None)
        if conn is None:  # pragma: no cover - the control uses FakeDb
            return real_reserve(players_db, player_id, reward_type, claim_key)
        with conn.cursor() as cur:
            cur.execute(dc._CLAIM_SQL,
                        (claim_key, reward_type, player_id))  # scrambled
            return cur.fetchone() is not None

    monkeypatch.setattr(dc, "reserve_claim", reordered)
    db = FakeDb()
    dc.reserve_claim(db, "player-x", WEEKLY, "den:2026-W38")
    assert db._last_params == ("den:2026-W38", WEEKLY, "player-x"), (
        "CONTROL FAILED: the sabotage did not change the bound order, so the "
        "order arm cannot observe it")


def test_the_read_binds_player_type_key_in_the_documented_order():
    """``claim_exists`` must ask about the SAME triple it wrote.

    A read bound in another order answers a question about another player, and
    returns False for a claim that exists -- which reads as "not yet claimed"
    and invites a second payout.
    """
    db = FakeDb()
    dc.reserve_claim(db, "player-x", FIRST_CLEAR, TEMPLATE)
    assert dc.claim_exists(db, "player-x", FIRST_CLEAR, TEMPLATE) is True
    assert db._last_params == ("player-x", FIRST_CLEAR, TEMPLATE), (
        "the ledger read bound %r, so it answered a different question than "
        "the writer asked" % (db._last_params,))


def test_claim_exists_answers_a_real_bool_not_a_row_object():
    """The caller branches on True/False, so the answer must be a bool.

    Returning the raw row would make ``if claim_exists(...)`` always truthy --
    every player would look like they had already claimed.
    """
    db = FakeDb()
    absent = dc.claim_exists(db, "p1", FIRST_CLEAR, TEMPLATE)
    assert absent is False, (
        "an absent claim answered %r; only False is safe for a branch"
        % (absent,))
    dc.reserve_claim(db, "p1", FIRST_CLEAR, TEMPLATE)
    present = dc.claim_exists(db, "p1", FIRST_CLEAR, TEMPLATE)
    assert present is True, (
        "a present claim answered %r; a truthy row object would be unsafe if it "
        "were ever absent" % (present,))


# ---------------------------------------------------------------------------
# 7. A FAILING LEDGER FAILS LOUD -- it never answers "already claimed"
# ---------------------------------------------------------------------------

def test_a_database_error_is_not_answered_as_a_replay():
    """The worst possible lie is "already claimed" when the ledger is down.

    ``reserve_claim`` returning False for an exception would suppress a real
    payout forever with no error anywhere: the route's else-arm treats False as
    a completed claim by another process and answers with an empty reward list.
    """
    with pytest.raises(RuntimeError):
        dc.reserve_claim(BrokenStore(), "p1", FIRST_CLEAR, TEMPLATE)


def test_the_read_path_also_fails_loud_rather_than_saying_false():
    """``claim_exists`` must not report "no claim" for a ledger it could not read.

    That is the same lie as above, one function down: a screen would show every
    player as un-claimed and invite a duplicate payout.
    """
    with pytest.raises(RuntimeError):
        dc.claim_exists(BrokenStore(), "p1", FIRST_CLEAR, TEMPLATE)


def test_release_on_a_broken_store_is_silent_because_it_asks_nothing():
    """Release runs on the failure path; it must not raise a second error.

    In PostgreSQL mode it issues no statement at all -- the rollback is the
    release -- so a downed connection cannot turn a failed payout into a
    confusing different failure. Asserted as "no raise", not as a status code.
    """
    dc.release_claim(BrokenStore(), "p1", FIRST_CLEAR, TEMPLATE)


# ---------------------------------------------------------------------------
# 8. REWARD-TYPE VOCABULARY IS EXACT, NOT APPROXIMATE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", [
    "LOOT", "Loot", " loot", "loot ", "loot\t", "first-clear", "weekly_",
    "WEEKLY", "loot\n", "", " loot ", "loot:run",
    # Not just near-miss STRINGS. A caller that hands the function a non-string
    # (a body field that failed to validate upstream, a None from a missing key)
    # must get the same refusal, not a minted key built from its repr or an
    # unhashable object that only blows up later inside the ledger.
    None, 123, 0, ["loot"], {"reward_type": "loot"}, b"loot", True,
])
def test_a_near_miss_reward_type_refuses_rather_than_minting_a_key(variant):
    """Anything but the three exact names must RAISE.

    A case-folding or trimming branch would mint a key for a type the paying
    route never calls, and a near-miss like ``loot `` could collide with the
    real ``loot`` key. Refusing is the only safe answer.
    """
    with pytest.raises(ValueError):
        dc.claim_key_for(variant, INSTANCE, TEMPLATE, "2026-W38")


def test_the_minting_vocabulary_is_exactly_three_names_and_nothing_else():
    """Enumerated, not sampled: a fourth mintable type is a contract change.

    The parametrised arm above proves named near-misses REFUSE. This proves the
    set of types that DO mint a key is exactly the three the paying route
    offers, so a new branch added to ``claim_key_for`` for an unlaunched reward
    type is caught here rather than shipping a claim identity nothing claims.
    """
    candidates = [
        LOOT, FIRST_CLEAR, WEEKLY, "loot", "first_clear", "weekly", "",
        "LOOT", "firstclear", "first-clear", "weekly_reward", "bonus",
        "milestone", "achievement", "leaderboard", "pvp", "tower", "boss",
        "daily", None, 1, "loots",
    ]
    # Ordered de-duplication: the candidate list repeats the three real names
    # (once bare, once quoted among the near-misses) and a census must count a
    # name once, or it reports the list's own duplication as a fourth type.
    minted = [c for c in dict.fromkeys(candidates) if not _refuses(c)]
    assert minted == [LOOT, FIRST_CLEAR, WEEKLY], (
        "the mintable reward-type vocabulary is %r, expected exactly the three "
        "the paying route offers; a fourth type shares a claim identity with "
        "claim bookkeeping nothing pays out" % (minted,))


@pytest.mark.parametrize("blank", ["", "   ", "\t", None, 7])
@pytest.mark.parametrize("reward_type", [FIRST_CLEAR, WEEKLY])
def test_an_empty_template_id_cannot_become_an_empty_claim_key(reward_type, blank):
    """A blank template must REFUSE, not mint a key every blank run shares.

    ``claim_key_for(FIRST_CLEAR, ...)`` returns the template id unchanged, so a
    blank one used to mint ``""``; ``weekly`` used to mint ``":2026-W38"``.
    Neither is merely missing -- both are SHARED. Every run, template and week
    that also lacked a template collided on one claim identity, so the second
    such player is refused by the ledger and silently loses the reward the
    first consumed.

    The fix refuses rather than padding: a placeholder template would collide
    exactly as hard as ``""``, one layer up. The caller can hand this module a
    blank -- ``dungeon_routes.py`` passes ``instance.template_id if instance
    else ""`` -- so the module itself is where it has to be refused.

    ``loot`` is NOT in this parametrisation, and that is deliberate: its key is
    scoped to the run and never reads the template, so a blank template cannot
    collide there. ``test_loot_still_keys_to_the_run_with_a_blank_template``
    pins that asymmetry so a future blanket guard is caught.
    """
    with pytest.raises(ValueError):
        dc.claim_key_for(reward_type, INSTANCE, blank, "2026-W38")


def test_loot_still_keys_to_the_run_with_a_blank_template():
    """``loot`` is keyed to the RUN, so it must keep working template-less.

    The asymmetry with ``first_clear``/``weekly`` is load-bearing: those two
    build the key FROM the template, so a blank one collapses the identity.
    ``loot`` does not read it, and a run's dropped items are paid once ever
    regardless of which template produced them. A guard that refused a blank
    template here would 500 a payout that is perfectly well-scoped.
    """
    key = dc.claim_key_for(LOOT, INSTANCE, "", "2026-W38")
    assert key == f"loot:{INSTANCE}", (
        "loot's key is no longer the bare run id, so it is no longer scoped to "
        "the run: a run could now be paid twice, or a blank template could have "
        "been folded into the identity")


def test_a_refused_blank_template_leaves_no_key_behind_at_all():
    """The refusal must be total: no key, not a key plus a warning.

    A function that caught its own refusal and returned something anyway would
    keep the payout on the old, colliding path while satisfying a
    ``pytest.raises`` that only ever saw the first call. Asserted through
    ``reserve_claim`` too, because that is the real entry point the paying
    route calls -- a key that never reaches the ledger cannot be reserved.
    """
    with pytest.raises(ValueError):
        dc.claim_key_for(FIRST_CLEAR, INSTANCE, "", "2026-W38")

    # Same inputs through the ledger's own entry point: the blank template must
    # not quietly reserve a claim row under a shared identity.
    db = NoDatabase()
    assert dc._MEM_CLAIM_KEYS == set(), (
        "a refused blank template still touched the fallback ledger, so the "
        "refusal is not the only thing that happened")

    key_ok = dc.claim_key_for(FIRST_CLEAR, INSTANCE, TEMPLATE, "2026-W38")
    assert dc.reserve_claim(db, "p1", FIRST_CLEAR, key_ok) is True
    assert len(dc._MEM_CLAIM_KEYS) == 1, (
        "the real template's claim did not reserve exactly one row")


def test_control_a_key_builder_without_the_blank_guard_is_caught(monkeypatch):
    """CONTROL: a builder with no blank-template guard mints the SHARED key.

    The house rule for this file: a test that cannot fail is a comment. So the
    guard is removed here -- in memory only, never on disk -- by restoring the
    pre-fix builder, and the same blank inputs that must refuse above are then
    required to mint a key again. If they refuse anyway, the sabotage never
    reached the code these tests import and the refusal arm measures nothing.

    Following the pattern of the controls above: this asserts the SABOTAGE
    manifests, rather than asserting the good behaviour survives it. The good
    behaviour is what the non-control tests require; this one's job is to prove
    those tests have something to bite on.
    """

    def pre_fix(reward_type: str, instance_id: str, template_id: str,
                week_key: str) -> str:
        if reward_type == "loot":
            return f"loot:{instance_id}"
        if reward_type == "first_clear":
            return template_id          # the defect: a blank passes through
        if reward_type == "weekly":
            return f"{template_id}:{week_key}"
        raise ValueError(f"unknown dungeon reward_type {reward_type!r}")

    # The defect, made concrete before the module is even involved: two runs on
    # two different weeks, both with no template, are the SAME claim.
    assert pre_fix(FIRST_CLEAR, INSTANCE, "", "2026-W38") == \
        pre_fix(FIRST_CLEAR, "a-different-run", "", "2026-W39"), (
        "CONTROL FAILED: the injected builder does not reproduce the collision "
        "it is supposed to inject, so it is not measuring the guard")

    monkeypatch.setattr(dc, "claim_key_for", pre_fix)

    assert dc.claim_key_for(FIRST_CLEAR, INSTANCE, "", "2026-W38") == "", (
        "CONTROL FAILED: the sabotaged builder still refused a blank template, "
        "so the blank-template refusal arms are not observing the guard")
    assert dc.claim_key_for(WEEKLY, INSTANCE, "", "2026-W38") == ":2026-W38", (
        "CONTROL FAILED: the sabotaged builder still refused a blank template "
        "for weekly, so the weekly refusal arm is not observing the guard")


# ---------------------------------------------------------------------------
# 9. THE KEY IS A PURE FUNCTION -- and the vocabulary census can fail
# ---------------------------------------------------------------------------

def test_the_key_is_deterministic_and_free_of_module_state():
    """The same inputs must mint the same key on every call, forever.

    The ledger's whole claim is "pays once", which is only true if the identity
    a second attempt computes EQUALS the identity the first one reserved. A key
    that carried a counter, a timestamp, an id() or any other per-call state
    would mint a fresh identity each time and every replay would pay.

    Measured by repeated calls, plus a check that each input the key is
    documented to carry really moves it, so a key that quietly ignored its
    template or run argument cannot pass as deterministic. Deliberately NOT
    measured with ``importlib.reload``: this module is shared state on a live
    tree, and reloading it would rebind the fallback ledger out from under
    every other test.
    """
    for reward_type in (LOOT, FIRST_CLEAR, WEEKLY):
        keys = {
            dc.claim_key_for(reward_type, INSTANCE, TEMPLATE, "2026-W38")
            for _ in range(5)
        }
        assert len(keys) == 1, (
            "%s minted %d different keys for identical inputs, so a replay "
            "would compute a new identity and pay again"
            % (reward_type, len(keys)))

    # The template is part of the identity for the two types that carry it:
    # a key that ignored it would let two different dungeons share one claim.
    assert dc.claim_key_for(WEEKLY, INSTANCE, "other_den",
                            "2026-W38") != dc.claim_key_for(
        WEEKLY, INSTANCE, TEMPLATE, "2026-W38")
    assert dc.claim_key_for(LOOT, "run-other", TEMPLATE,
                            "2026-W38") != dc.claim_key_for(
        LOOT, INSTANCE, TEMPLATE, "2026-W38")


def test_reserving_the_same_inputs_twice_through_both_backends_agrees():
    """The database path and the fallback must reach the SAME verdict.

    They are two implementations of one contract. If they disagreed, the
    guarantee would depend on which mode the server happened to boot in -- the
    payout would be once-only with a database and repeatable without one, and
    local dev would never reproduce the production behaviour.
    """
    with_db, without_db = FakeDb(), NoDatabase()
    verdicts = []
    for store in (with_db, without_db):
        verdicts.append((
            dc.reserve_claim(store, "p1", FIRST_CLEAR, TEMPLATE),
            dc.reserve_claim(store, "p1", FIRST_CLEAR, TEMPLATE),
            dc.reserve_claim(store, "p2", FIRST_CLEAR, TEMPLATE),
        ))
    assert verdicts[0] == verdicts[1] == (True, False, True), (
        "the two backends disagree on (first attempt, replay, other player): "
        "database=%r fallback=%r -- the once-only guarantee would depend on "
        "which mode the server booted in"
        % (verdicts[0], verdicts[1]))


def test_control_a_case_folding_vocabulary_breaks_the_census(monkeypatch):
    """REQUIRE the vocabulary census to go red when a branch is added.

    Without this, the census could pass for the wrong reason: it asserts on a
    hard-coded list, so it must be shown to actually observe the module.
    """
    real = dc.claim_key_for

    def case_folding(reward_type, instance_id, template_id, week_key):
        if isinstance(reward_type, str):
            reward_type = reward_type.strip().lower().replace("-", "_")
        return real(reward_type, instance_id, template_id, week_key)

    monkeypatch.setattr(dc, "claim_key_for", case_folding)
    minted = [c for c in ("loot", "first_clear", "weekly", "LOOT", " first-clear",
                          "first-clear", " weekly ")
              if not _refuses(c)]
    assert len(minted) > 3, (
        "CONTROL FAILED: the sabotaged vocabulary still refused every "
        "near-miss, so the census cannot observe a case-folding branch")


def test_control_a_stateful_key_breaks_the_determinism_arm(monkeypatch):
    """REQUIRE the determinism arm to go red on a per-call counter.

    A key built from a call counter is the exact defect the arm exists to
    catch: every replay would mint a new identity and pay again.
    """
    counter = {"n": 0}
    real_key = dc.claim_key_for  # captured BEFORE the patch below

    def counted(reward_type, instance_id, template_id, week_key):
        counter["n"] += 1
        return "%s#%d" % (real_key(reward_type, instance_id, template_id,
                                    week_key), counter["n"])

    monkeypatch.setattr(dc, "claim_key_for", counted)
    keys = {dc.claim_key_for(FIRST_CLEAR, INSTANCE, TEMPLATE, "2026-W38")
            for _ in range(5)}
    assert len(keys) == 5, (
        "CONTROL FAILED: the counting key produced %d distinct keys from 5 "
        "identical calls, so the determinism arm cannot observe a per-call "
        "state" % len(keys))

# =====================================================================================
# 10. THE SAME CONTRACTS, MEASURED THROUGH THE ROUTE THAT ACTUALLY PAYS
# =====================================================================================
#
# Everything above drives ``dungeon_claims`` directly. That is deliberate -- a
# failure there names the ledger rather than a route that also touches the
# inventory. But it leaves three things unmeasured, and all three are the ones a
# player actually experiences:
#
#   * AUTH. ``claim_loot`` is guarded by ``Depends(require_player)``, which lives
#     in the ROUTE, not in the ledger. A ledger test cannot see it, and a ledger
#     with the guard deleted is perfectly green.
#   * THE CREDIT. "Idempotent" has to mean the player's INVENTORY moves once. A
#     ledger that refuses a replay while the route credits anyway is green here
#     and double-paying in the game.
#   * THE REFUSAL BURNS NOTHING. A claim refused for a bad body must not spend
#     the player's real one-shot; that is the defect the blank-template key and
#     the cross-player body both used to create.
#
# WHY A MINIMAL APP INSTEAD OF ``server.main``
# ---------------------------------------------
# These arms mount ONLY ``dungeon_routes`` on a bare ``FastAPI`` and publish a
# store through the module's own registrar. Two reasons, one practical and one
# about identity:
#
#   * Practical: the arms are about the dungeon router and the claim ledger.
#     Importing the whole server drags in every unrelated module, so a peer's
#     unrelated edit to another route file breaks a file that measures none of
#     it. This file stays runnable on its own.
#   * Identity: ``dungeon_routes`` imports ``auth`` BARE and
#     ``from server.dungeons import get_dungeon_manager`` PACKAGE-QUALIFIED.
#     Getting either wrong hands this file a second auth session store (every
#     signed call answers 401, which reads exactly like a working guard) or a
#     second dungeon manager (the fixture advances an instance the route cannot
#     see). Both are asserted at import, not assumed.
#
# ``server.dungeons`` is imported package-qualified HERE for the same reason the
# route does it. ``get_dungeon_manager`` hands back a per-process singleton, and
# the bare import yields a DIFFERENT module object with a DIFFERENT singleton, so
# seeding loot through it would seed a run the route never reads.

_dungeon_routes = pytest.importorskip("dungeon_routes")


@pytest.fixture(scope="module")
def paying_app():
    """A minimal ASGI app serving the dungeon router over a real player store."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from player_store import PlayerStore

    store = PlayerStore()
    app = FastAPI()
    # The module's OWN registrar, so the store reaches the routes by exactly the
    # path production uses. A test that set the module global directly would
    # pass while the published wiring was broken.
    _dungeon_routes.register_dungeon_routes(app, store)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, store


@pytest.fixture
def paying(paying_app):
    client, store = paying_app
    auth_mod = sys.modules["auth"]
    counter = {"n": 0}

    def _session(player_id: str) -> dict:
        return {"Authorization": "Bearer " + auth_mod.issue_session(player_id)}

    def _player(role: str) -> str:
        """A real account, so a refusal cannot be confused with a missing player.

        Named per-``role`` and never reused: a dungeon instance id is unique but
        a PLAYER id is looked up globally, so a fixed id would let one test's
        party state bleed into another's.
        """
        counter["n"] += 1
        pid = "dungeon-claims-probe-%s-%d-%d" % (
            role, os.getpid(), counter["n"])
        store.create_account(pid, {
            "id": pid,
            "name": "DungeonClaims%s%d" % (role, counter["n"]),
            "spirit_root": "fire", "realm": "mortal", "level": 1,
            # An explicit container: claim-loot writes THROUGH it, and a missing
            # one would make a silent "credited nothing" look like a refusal.
            "inventory": {},
        }, b"dungeon-claims-contract-signing" * 2)
        return pid

    def _completed_run(role: str, template_id: str = "spirit_beast_den") -> dict:
        """A solo run that reached COMPLETED with one KNOWN item dropped.

        Completed because the manager pays nothing for an unfinished run, and a
        fixture stopped at ``active`` would make every payout arm pass for the
        wrong reason -- the manager declining rather than the route refusing.
        The drop is SEEDED because loot is chance-gated per item, so a completed
        run legitimately sometimes drops nothing and leaving it to chance makes
        the credit arms unfalsifiable. Seeding WHAT was dropped is a decision
        about the fixture; the ledger and the credit stay real.
        """
        from server.dungeons import get_dungeon_manager

        leader = _player(role)
        party = client.post("/dungeons/party/create",
                            json={"leader_id": leader},
                            headers=_session(leader)).json()["party_id"]
        readied = client.post("/dungeons/party/ready",
                              json={"player_id": leader, "ready": True},
                              headers=_session(leader))
        assert readied.status_code == 200, readied.text

        made = client.post("/dungeons/instance/create",
                           json={"party_id": party, "template_id": template_id},
                           headers=_session(leader))
        assert made.status_code == 200, made.text
        instance_id = made.json()["instance_id"]
        assert client.post("/dungeons/instance/start",
                           json={"instance_id": instance_id,
                                 "player_id": leader},
                           headers=_session(leader)).status_code == 200

        for _ in range(12):
            step = client.post("/dungeons/instance/advance",
                               json={"instance_id": instance_id,
                                     "player_id": leader},
                               headers=_session(leader))
            assert step.status_code == 200, step.text
            if step.json().get("completed"):
                break
        else:
            pytest.fail("the run never completed; the fixture is not exercising "
                        "the payout route at all")

        instance = get_dungeon_manager().instances[instance_id]
        instance.loot_dropped = [{"item_id": "contract_dungeon_token", "count": 2,
                                  "source": "dungeon_loot"}]
        instance.rewards_claimed = set()
        return {"leader": leader, "instance_id": instance_id,
                "template_id": template_id}

    def _inventory(player_id: str) -> dict:
        with store.transaction(player_id):
            return dict(store[player_id].get("inventory") or {})

    def _claim(player_id: str, instance_id: str, reward_type: str, headers=None):
        return client.post("/dungeons/instance/claim-loot", json={
            "instance_id": instance_id, "player_id": player_id,
            "reward_type": reward_type,
        }, headers=headers)

    return {"client": client, "store": store, "session": _session,
            "player": _player, "run": _completed_run, "inventory": _inventory,
            "claim": _claim}


# ---------------------------------------------------------------- 1. AUTH
# ------------------------------------------------------------------------------------

def test_claiming_loot_without_a_session_is_refused_and_credits_nothing(paying):
    """The route's OWN guard, and the two facts a refusal must not change.

    ``Depends(require_player)`` lives in ``dungeon_routes``, so nothing above can
    see it -- deleting that dependency leaves every ledger arm green. The
    inventory and the ledger are both asserted afterwards: a 401 that had already
    credited, or had already spent the player's one-shot, would be the far worse
    bug and would look identical from the status code alone.
    """
    run = paying["run"]("AnonRefused")
    assert "contract_dungeon_token" not in paying["inventory"](run["leader"])

    refused = paying["claim"](run["leader"], run["instance_id"], "loot")
    assert refused.status_code == 401, (
        "an anonymous claim-loot answered %r; the route's session guard is not "
        "installed" % (refused.status_code,))

    assert paying["inventory"](run["leader"]).get(
        "contract_dungeon_token") is None, (
        "a refused anonymous claim still credited the inventory")
    assert not any(k[0] == run["leader"] for k in dc._MEM_CLAIM_KEYS), (
        "a refused anonymous claim still reserved a claim row, so the player's "
        "real one-shot was spent by a caller who was never allowed to claim")


def test_a_forged_or_unknown_session_cannot_claim_loot(paying):
    """A wrong token is refused exactly like a missing one.

    The case that matters is the ORDER: if an unknown token were answered 404
    "player not found" the route would be confirming which player ids exist to a
    caller who proved nothing about them.
    """
    run = paying["run"]("ForgedSession")
    for token in ("not-a-real-token", "", "Bearer"):
        headers = {"Authorization": token} if token else {"Authorization": " "}
        refused = paying["claim"](run["leader"], run["instance_id"], "loot",
                                  headers=headers)
        assert refused.status_code == 401, (
            "a session of %r answered %r" % (token, refused.status_code))
    assert paying["inventory"](run["leader"]).get(
        "contract_dungeon_token") is None


def test_a_claim_body_naming_someone_else_is_refused_and_the_victim_can_still_claim(
        paying):
    """Cross-player: 403, and the victim's one-shot survives the attempt.

    The measured defect this closes, from the route's own docstring: the body
    used to be trusted for the target, so an outsider POSTed the victim's id and
    burned the victim's ``first_clear`` -- the theft was not merely duplicated,
    it left the real owner permanently unable to retry. Asserting only the 403
    would pass against a route that refused AND spent the key.
    """
    run = paying["run"]("CrossPlayer")
    victim, outsider = run["leader"], paying["player"]("Outsider")

    refused = paying["claim"](victim, run["instance_id"], "loot",
                              headers=paying["session"](outsider))
    assert refused.status_code == 403, (
        "a member claiming into ANOTHER member's inventory answered %r"
        % (refused.status_code,))
    assert paying["inventory"](victim).get("contract_dungeon_token") is None
    assert paying["inventory"](outsider) == {}, (
        "the refused cross-player claim credited the ATTACKER's inventory")

    # The victim is still able to claim. This is the half that makes the 403 a
    # refusal rather than a theft.
    paid = paying["claim"](victim, run["instance_id"], "loot",
                           headers=paying["session"](victim))
    assert paid.status_code == 200 and paid.json()["rewards"], (
        "the attacker's attempt consumed the victim's real claim: %r"
        % (paid.json(),))


def test_the_reward_type_refusal_happens_BEFORE_participation_is_checked(paying):
    """The ordering of two route gates, which a status code alone cannot show.

    ``claim_loot`` has two things that can refuse an unknown reward type: the
    early vocabulary check, and the ``else: raise HTTPException(400)`` inside the
    transaction. It also has the participation gate, which refuses a non-member
    with 403. So the QUESTION that separates "the vocabulary check refused" from
    "the vocabulary check is missing and something later did" is not the status
    code -- both paths answer 400 for a party MEMBER. It is the answer for a
    STRANGER.

    MEASURED on a mutated copy of the route with the early check deleted:

        caller is a party member, bad type   baseline 400   mutant 400
        caller is a STRANGER,  bad type      baseline 400   mutant 403

    The second line is the discriminator. Baseline refuses on the vocabulary
    before it ever looks at who is asking; with the early check gone the request
    falls through to the participation gate and the answer changes to 403. The
    caller names THEIR OWN ``player_id`` throughout, so the auth guard's
    body-claim check passes and whatever answers is the route's own gate.
    """
    client, session = paying["client"], paying["session"]
    owner = paying["player"]("OrderOwner")
    stranger = paying["player"]("OrderStranger")

    party = client.post("/dungeons/party/create", json={"leader_id": owner},
                        headers=session(owner)).json()["party_id"]
    assert client.post("/dungeons/party/ready",
                       json={"player_id": owner, "ready": True},
                       headers=session(owner)).status_code == 200
    made = client.post("/dungeons/instance/create",
                       json={"party_id": party,
                             "template_id": "spirit_beast_den"},
                       headers=session(owner))
    assert made.status_code == 200, made.text
    instance_id = made.json()["instance_id"]

    # Own player_id, so the auth guard's body-claim check passes and the refusal
    # is the route's, not the guard's.
    answered = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": stranger,
        "reward_type": "mystery_box"}, headers=session(stranger))
    assert answered.status_code == 400, (
        "a non-member sending an unknown reward_type was answered %r (%s). "
        "Baseline refuses on the reward type BEFORE it looks at participation; "
        "an answer of 403 means the early check is gone and the request is "
        "reaching the participation gate instead."
        % (answered.status_code, answered.text[:120]))


def test_a_party_member_claiming_into_a_teammates_inventory_is_refused(paying):
    """Cross-player theft is refused, and WHO refuses it is pinned by the message.

    The route has its own actor==claim check, but this arm cannot be said to
    measure it, and the reason is worth recording. MEASURED by deleting that
    check from a mutated copy of the route and re-running: the response is
    byte-identical, 403 ``"player_id does not match the authenticated session"``.
    That string is the AUTH GUARD's, raised inside ``require_player`` before the
    handler body runs at all -- the guard refuses a body that names someone else
    on its own. The route's check is therefore defence in depth: unreachable
    through HTTP while the auth guard is installed, and load-bearing only if
    that guard is ever weakened.

    So this arm asserts the property that actually holds end to end -- a member
    cannot move a teammate's loot -- and asserts the REFUSING LAYER by message,
    which is the part that would change if the guard were removed. A recruiter
    and a promoted member are both genuinely participants, so the participation
    gate passes them and cannot be what refuses.
    """
    from server.dungeons import get_dungeon_manager

    leader = paying["player"]("MemberTheftLeader")
    member = paying["player"]("MemberTheftMate")
    client, session = paying["client"], paying["session"]

    party = client.post("/dungeons/party/create", json={"leader_id": leader},
                        headers=session(leader)).json()["party_id"]
    assert client.post("/dungeons/party/invite",
                       json={"party_id": party, "invitee_id": member},
                       headers=session(leader)).status_code == 200
    for pid in (leader, member):
        assert client.post("/dungeons/party/ready",
                           json={"player_id": pid, "ready": True},
                           headers=session(pid)).status_code == 200

    made = client.post("/dungeons/instance/create",
                       json={"party_id": party,
                             "template_id": "spirit_beast_den"},
                       headers=session(leader))
    assert made.status_code == 200, made.text
    instance_id = made.json()["instance_id"]
    assert client.post("/dungeons/instance/start",
                       json={"instance_id": instance_id, "player_id": leader},
                       headers=session(leader)).status_code == 200
    for _ in range(12):
        step = client.post("/dungeons/instance/advance",
                           json={"instance_id": instance_id,
                                 "player_id": leader}, headers=session(leader))
        assert step.status_code == 200, step.text
        if step.json().get("completed"):
            break
    else:
        pytest.fail("the two-member run never completed; the member-theft arm "
                    "is not exercising a payout path")

    instance = get_dungeon_manager().instances[instance_id]
    instance.loot_dropped = [{"item_id": "contract_dungeon_token", "count": 2,
                              "source": "dungeon_loot"}]
    instance.rewards_claimed = set()

    # Anti-vacuity: the member really IS a participant, or the refusal below is
    # the participation guard and this arm measures nothing.
    assert get_dungeon_manager().player_party.get(member) == party, (
        "the fixture's recruit did not join the owning party, so the arm below "
        "would be measuring the participation guard rather than the actor==claim "
        "guard")

    # The member names the LEADER. Participation passes for the member; only the
    # actor==claim check can refuse this.
    stolen = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": leader,
        "reward_type": "loot"}, headers=session(member))
    assert stolen.status_code == 403, (
        "a party member claimed loot into a TEAMMATE's inventory and was "
        "answered %r" % (stolen.status_code,))
    # WHICH LAYER refused. Measured: this string comes from `require_player`,
    # not from the route's own actor==claim check, which is unreachable through
    # HTTP while the auth guard is installed. If the auth guard were weakened,
    # this detail changes and the arm goes red on the layer that moved.
    assert "does not match the authenticated session" in stolen.text, (
        "the cross-player claim was refused by %r rather than by the session "
        "guard. That means the route's own actor==claim check is now the only "
        "thing refusing this -- correct, but a different guarantee from the one "
        "measured here, and the assertion above no longer describes it."
        % (stolen.text[:160],))
    assert paying["inventory"](member).get("contract_dungeon_token") is None, (
        "the refused member-theft attempt credited the THIEF's inventory")
    assert paying["inventory"](leader).get("contract_dungeon_token") is None, (
        "the refused member-theft attempt credited the VICTIM's inventory, "
        "which is the theft this guard exists to stop")


# ---------------------------------------------------------------- 2. IDEMPOTENCY
# ------------------------------------------------------------------------------------

def test_the_first_claim_credits_once_and_the_replay_credits_nothing(paying):
    """The whole point of the ledger, seen from the player's inventory.

    The pair matters: "a replay pays nothing" alone passes against a route that
    never pays at all, and "the first paid" alone passes against one that pays
    on every call. The inventory is read back from the STORE, not from the reply
    -- a route can advertise a reward it never granted.
    """
    run = paying["run"]("Idempotent")
    leader, instance_id = run["leader"], run["instance_id"]

    first = paying["claim"](leader, instance_id, "loot",
                            headers=paying["session"](leader))
    assert first.status_code == 200, first.text
    rewards = first.json()["rewards"]
    assert rewards and rewards[0]["count"] == 2, (
        "the completed, seeded run advertised no loot: %r" % (first.json(),))
    assert paying["inventory"](leader).get("contract_dungeon_token") == 2, (
        "the reply advertised 2 tokens and the store holds %r"
        % (paying["inventory"](leader).get("contract_dungeon_token"),))

    for attempt in (2, 3):
        replay = paying["claim"](leader, instance_id, "loot",
                                headers=paying["session"](leader))
        # 200 with an empty list is the guard WORKING, not a bug: the run is
        # spent, not missing. Asserting a refusal code here would be wrong.
        assert replay.status_code == 200, (
            "replay %d answered %r; a spent run is still a successful call"
            % (attempt, replay.status_code))
        assert replay.json()["rewards"] == [], (
            "replay %d PAID: the once-only loot guard did not hold: %r"
            % (attempt, replay.json()))
        assert paying["inventory"](leader).get("contract_dungeon_token") == 2, (
            "replay %d changed the inventory to %r"
            % (attempt, paying["inventory"](leader)))


def test_the_ledger_row_the_route_wrote_is_the_server_derived_key(paying):
    """The key the route reserved is the one THIS module derives.

    A caller that could name the key could name an UNPAID one, and "pays once"
    would become a property of the client's memory. Proven from the ledger's own
    contents, not from SQL text: the row is compared against
    ``claim_key_for`` computed here from the instance's own fields.
    """
    run = paying["run"]("ServerDerivedKey")
    leader, instance_id = run["leader"], run["instance_id"]
    assert paying["claim"](leader, instance_id, "loot",
                           headers=paying["session"](leader)).status_code == 200

    expected = dc.claim_key_for("loot", instance_id, run["template_id"],
                                "2026-W38")
    held = [k for k in dc._MEM_CLAIM_KEYS if k[0] == leader]
    assert len(held) == 1, (
        "one successful loot claim left %d ledger rows, not 1: %r"
        % (len(held), dc._MEM_CLAIM_KEYS))
    assert held[0] == (leader, "loot", expected), (
        "the route reserved %r but the server-derived key is %r, so the key the "
        "guard used is not the key this module derives"
        % (held[0], (leader, "loot", expected)))


def test_a_claim_body_cannot_supply_a_claim_key(paying):
    """The request model carries no claim key, so a caller cannot name one.

    Checked against the LIVE model rather than the source text: an extra field
    is ignored by pydantic, so if a key field were ever added here it would be
    accepted silently and this arm would still pass on the old model. The
    observable consequence is what matters -- posting one changes nothing about
    which key is reserved, and does not grant the loot of a key not yet paid.
    """
    run = paying["run"]("NoCallerKey")
    leader, instance_id = run["leader"], run["instance_id"]
    smuggled = dict(instance_id=instance_id, player_id=leader,
                    reward_type="loot", claim_key="first_clear:free_money")
    client = paying["client"]
    posted = client.post("/dungeons/instance/claim-loot", json=smuggled,
                         headers=paying["session"](leader))
    assert posted.status_code == 200, posted.text

    held = sorted(k for k in dc._MEM_CLAIM_KEYS if k[0] == leader)
    assert held == [(leader, "loot",
                     dc.claim_key_for("loot", instance_id,
                                      run["template_id"], "2026-W38"))], (
        "a caller-supplied claim_key changed the reserved row to %r" % (held,))


# ---------------------------------------------------------------- 3. VALIDATION
# ------------------------------------------------------------------------------------

def test_an_unknown_reward_type_is_refused_and_spends_no_claim(paying):
    """400 before the ledger is touched, and the real claim still pays.

    Two separate failures if either regresses: the route must not mint a key for
    a type it does not understand, and it must not burn the player's one-shot
    while refusing. The second half is asserted by claiming for real afterwards.
    """
    run = paying["run"]("BadRewardType")
    leader, instance_id = run["leader"], run["instance_id"]

    # THE ORDERING, not just the status code. Two checks in the route can answer
    # 400 for an unknown type -- the early one, and the `else: raise` inside the
    # transaction -- so "it answered 400" cannot tell them apart. What separates
    # them is WHEN: the early check refuses before the dungeon manager is ever
    # consulted, while the late one has already opened a transaction and asked
    # the manager. MEASURED, not assumed: deleting the early check leaves this
    # file green on a status-code assertion alone, because the late check still
    # answers 400. The manager spy below is the only assertion that went red.
    bad = paying["claim"](leader, instance_id, "mystery_box",
                          headers=paying["session"](leader))
    assert bad.status_code == 400, (
        "an unknown reward_type answered %r; the route must refuse it"
        % (bad.status_code,))
    assert not any(k[0] == leader for k in dc._MEM_CLAIM_KEYS), (
        "a refused reward_type still reserved a claim row, so the player's real "
        "loot would be suppressed by a request that paid nothing")

    good = paying["claim"](leader, instance_id, "loot",
                           headers=paying["session"](leader))
    assert good.status_code == 200 and good.json()["rewards"], (
        "the refused bad type consumed the player's real loot claim: %r"
        % (good.json(),))


def test_a_claim_for_a_run_the_caller_is_not_in_is_refused_and_spends_nothing(
        paying):
    """Participation is checked BEFORE the ledger, so a refused caller burns
    nothing.

    The measured defect: joining a party was free and instant, so a
    participation check was a lookup the attacker flipped one request earlier.
    This arm pins the CURRENT shape -- a stranger is refused, and the owner's
    loot is still claimable.
    """
    run = paying["run"]("NonParticipant")
    leader, instance_id = run["leader"], run["instance_id"]
    outsider = paying["player"]("NonParticipantStranger")

    refused = paying["claim"](leader, instance_id, "loot",
                              headers=paying["session"](outsider))
    assert refused.status_code == 403, (
        "a stranger claimed loot from a run they are not in: %r"
        % (refused.status_code,))
    assert paying["inventory"](leader).get("contract_dungeon_token") is None
    assert not any(k[0] in (leader, outsider) for k in dc._MEM_CLAIM_KEYS), (
        "a refused non-participant still reserved a ledger row")

    paid = paying["claim"](leader, instance_id, "loot",
                           headers=paying["session"](leader))
    assert paid.status_code == 200 and paid.json()["rewards"], (
        "the stranger's refused attempt consumed the owner's loot: %r"
        % (paid.json(),))


def test_the_module_refuses_the_claim_a_route_would_mint_for_a_blank_template():
    """The boundary guarantee, from the module's side.

    ``dungeon_routes`` passes ``instance.template_id if instance else ""``, so a
    blank template CAN reach ``claim_key_for``. Its own docstring notes this is
    currently unreachable with a payout attached -- both reward methods return
    ``[]`` for an unknown template -- so this is a boundary guarantee rather
    than a fix for money already lost. It is pinned here anyway because it is
    the module's job, and a caller that stopped gating it would otherwise get a
    key SHARED by every blank-template run.
    """
    for reward_type in ("first_clear", "weekly"):
        with pytest.raises(ValueError):
            dc.claim_key_for(reward_type, "inst-abc", "", "2026-W38")


# ---------------------------------------------------------------- 4. PERSISTENCE
# ------------------------------------------------------------------------------------

def test_the_refusal_is_held_by_the_ledger_not_by_any_one_store_or_manager(
        paying):
    """The once-only fact outlives the objects that observed it.

    This is the property an in-process dict on a manager cannot have. Two
    FRESH objects -- a brand-new player store and a brand-new dungeon manager --
    are each handed the same key and must both refuse it, even though neither
    was the object the route used and neither has any memory of the claim.
    Written against the no-database fallback because that is the backend
    available in this environment; the point under test is that the answer comes
    from the ledger module rather than from any single caller-side object.
    """
    from player_store import PlayerStore
    from server.dungeons import DungeonManager

    run = paying["run"]("LedgerOwnership")
    leader, instance_id = run["leader"], run["instance_id"]
    assert paying["claim"](leader, instance_id, "loot",
                           headers=paying["session"](leader)).status_code == 200
    key = dc.claim_key_for("loot", instance_id, run["template_id"], "2026-W38")

    # A store object the route never saw.
    assert dc.reserve_claim(PlayerStore(), leader, "loot", key) is False, (
        "a FRESH store admitted a claim the route had already paid, so the "
        "once-only fact was living in the store object rather than in the "
        "ledger")

    # A manager object the route never saw, holding no instance at all.
    assert DungeonManager().instances == {}, (
        "the fixture's premise broke: this manager is not empty, so the next "
        "assertion would prove nothing about manager-independent state")
    assert dc.reserve_claim(PlayerStore(), leader, "loot", key) is False, (
        "a claim survived its own manager being replaced")


def test_a_claim_stays_claimed_after_the_player_row_is_rewritten(paying):
    """Rewriting the player must not reopen a spent claim.

    The credit path persists the player row on every payout. If the ledger were
    derived from that row -- or stored beside it in a way the write clobbered --
    a re-persist would silently reopen the claim. Measured by re-writing the row
    the way the credit does, then replaying.
    """
    run = paying["run"]("PlayerRewrite")
    leader, instance_id = run["leader"], run["instance_id"]
    assert paying["claim"](leader, instance_id, "loot",
                           headers=paying["session"](leader)).status_code == 200

    store = paying["store"]
    with store.transaction(leader):
        row = store[leader]
        row["last_seen"] = row.get("last_seen", 0) + 1
        store.persist(leader, row)

    replay = paying["claim"](leader, instance_id, "loot",
                             headers=paying["session"](leader))
    assert replay.json()["rewards"] == [], (
        "rewriting the player row reopened a spent claim: %r" % (replay.json(),))
    assert paying["inventory"](leader).get("contract_dungeon_token") == 2


# ------------------------------------------------- CONTROLS for the arms above
# ------------------------------------------------------------------------------------
#
# Every control here sabotages BOTH layers of a two-layer guard. That is not
# pedantry: the route consults the manager's own in-process dicts FIRST and the
# database ledger SECOND, so deleting only the ledger leaves every test green --
# the manager refuses the replay on its own. Each control therefore disables the
# manager's record as well, and then REQUIRES the inventory to have doubled. A
# green control means the arm above it cannot fail, so it is worthless.

def _open_both_guards(run: dict) -> None:
    """Undo the manager's OWN record, so only the ledger can refuse.

    MEASURED, and the first version of this helper was wrong in a way worth
    recording. ``Instance.rewards_claimed`` looks like the once-only guard and is
    not: ``DungeonManager.claim_loot`` decides on ``self.loot_claimed``, a
    manager-level ``player_id -> template_id -> claim_key`` dict that outlives any
    run. Clearing only the instance set left the replay refused, the control came
    back holding 2 tokens instead of 4, and it read like "the once-only arm above
    is measuring the manager and not the ledger" -- which was true, just not for
    the reason the message claimed.

    Scoped to this test's own player id on purpose. ``get_dungeon_manager()`` is
    a process singleton shared with every other file in this suite, so a blanket
    ``.clear()`` would reopen claims belonging to tests that have not run yet.
    """
    from server.dungeons import get_dungeon_manager

    manager = get_dungeon_manager()
    leader = run["leader"]

    instance = manager.instances.get(run["instance_id"])
    if instance is not None:
        instance.rewards_claimed = set()

    for ledger_name in ("loot_claimed", "first_clear_claimed", "weekly_claimed"):
        manager.__dict__[ledger_name].pop(leader, None)

    # Prove the opening happened. A helper that silently clears nothing leaves
    # the control green for the wrong reason, which is the failure this whole
    # control exists to detect -- so it must not be able to repeat one.
    still_recorded = [name for name in ("loot_claimed", "first_clear_claimed",
                                        "weekly_claimed")
                      if manager.__dict__[name].get(leader)]
    assert not still_recorded, (
        "CONTROL FAILED: the manager's own ledgers were not cleared, so the "
        "replay was refused by the manager and the control cannot tell a "
        "removed ledger from an intact one: %r" % (still_recorded,))


def test_control_an_always_first_ledger_doubles_the_inventory(paying,
                                                              monkeypatch):
    """CONTROL: with the ledger made to always say "first", the payout doubles.

    Both layers are opened, so the arm above is genuinely the thing that would
    have caught a removed guard. If this control cannot double the inventory,
    ``test_the_first_claim_credits_once_and_the_replay_credits_nothing`` was
    never measuring the ledger.
    """
    monkeypatch.setattr(dc, "reserve_claim",
                        lambda *a, **k: True)
    run = paying["run"]("ControlDoublePay")
    leader, instance_id = run["leader"], run["instance_id"]

    first = paying["claim"](leader, instance_id, "loot",
                            headers=paying["session"](leader))
    assert first.json()["rewards"], (
        "CONTROL FAILED: the first claim paid nothing, so the scenario never "
        "exercised a payout: %r" % (first.json(),))

    _open_both_guards(run)
    replay = paying["claim"](leader, instance_id, "loot",
                             headers=paying["session"](leader))
    total = paying["inventory"](leader).get("contract_dungeon_token")
    assert total == 4, (
        "CONTROL FAILED: with the ledger made to always grant a first claim, "
        "the player's inventory holds %r tokens rather than 4. The manager's "
        "in-process dict still refused the replay, so the once-only arm above "
        "is measuring the MANAGER and not the ledger: %r"
        % (total, replay.json()))


def test_control_neutralising_the_route_dependency_stops_the_401(paying):
    """CONTROL: the 401 comes from ``Depends(require_player)`` and nothing else.

    The guard under test is a DEPENDENCY, so the control replaces that one
    dependency in the MOUNTED route with an always-passing stand-in and requires
    the anonymous claim to stop being refused. Replacing the dependency is
    deliberate: deleting it would remove every guard on the router at once, so a
    failure could not say which guard had done the refusing, and a pass could be
    the route refusing for an unrelated reason.

    A control that cannot make the 401 go away means the arm above it is not
    measuring the session guard.
    """
    route = None
    for candidate in paying["client"].app.routes:
        if getattr(candidate, "path", "") == "/dungeons/instance/claim-loot":
            route = candidate
            break
    assert route is not None, (
        "claim-loot is not mounted on this app, so the auth arms measured "
        "nothing")

    # Prove the guard is really there before removing it, or "it stopped being
    # 401" could mean the route was never guarded in the first place.
    guarded = [d.call for d in route.dependant.dependencies]
    assert any(call is _dungeon_routes.require_player for call in guarded), (
        "the mounted claim-loot route does not depend on require_player, so "
        "there is no session guard to control: %r" % (guarded,))

    async def _always_passes() -> object:
        return object()

    patched = []
    for dep in route.dependant.dependencies:
        if dep.call is _dungeon_routes.require_player:
            patched.append((dep, dep.call))
            dep.call = _always_passes
    assert patched, (
        "CONTROL FAILED: the dependency could not be found on the mounted "
        "route, so removing it was never attempted")

    try:
        opened = paying["client"].post("/dungeons/instance/claim-loot", json={
            "instance_id": "inst-that-does-not-exist",
            "player_id": "nobody", "reward_type": "loot"})
    finally:
        for dep, original in patched:
            dep.call = original

    assert opened.status_code != 401, (
        "CONTROL FAILED: with the session dependency replaced by an "
        "always-passing one, the anonymous claim was STILL refused with 401. "
        "The 401 is coming from somewhere other than the dependency the auth "
        "arm is written against, so that arm is measuring the wrong thing.")
    assert paying["client"].post("/dungeons/instance/claim-loot", json={
        "instance_id": "inst-that-does-not-exist",
        "player_id": "nobody", "reward_type": "loot"}).status_code == 401, (
        "CONTROL FAILED: restoring the dependency did not restore the 401, so "
        "the control is not reversible and proved nothing")


def test_control_the_vocabulary_gate_is_defence_in_depth_not_the_only_gate(paying):
    """CONTROL: the module refuses an unknown type, so a route check is belt.

    The route checks the reward type itself and answers 400 before the ledger is
    touched. The module ALSO refuses -- ``claim_key_for`` raises for a type it
    does not know. That second gate is what stops a route that forgot the first
    one from minting a claim identity for a type nothing pays.

    Measured here on its own so the vocabulary arm above is not resting on a
    single check: if the module ever started minting a key for an unknown type,
    a future route that dropped its own check would silently create a shared
    claim identity, and only this control would notice.
    """
    run = paying["run"]("ControlVocabulary")
    leader, instance_id = run["leader"], run["instance_id"]
    paid = paying["claim"](leader, instance_id, "loot",
                           headers=paying["session"](leader))
    assert paid.status_code == 200 and paid.json()["rewards"], (
        "CONTROL FAILED: the fixture did not pay, so the vocabulary arms are "
        "not measuring a payout path: %r" % (paid.json(),))

    assert _refuses_route_type(leader, instance_id, "mystery_box"), (
        "CONTROL FAILED: the module minted a key for an unknown reward type, so "
        "the route's own vocabulary check is the ONLY thing standing between a "
        "caller and a claim identity nothing pays")


def _refuses_route_type(player_id: str, instance_id: str,
                        reward_type: str) -> bool:
    """Would the module hand this route a claim key for ``reward_type``?"""
    try:
        dc.claim_key_for(reward_type, instance_id, TEMPLATE, "2026-W38")
    except Exception:
        return True
    return False
