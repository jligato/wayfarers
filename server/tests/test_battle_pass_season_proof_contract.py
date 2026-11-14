"""Contract tests for server/battle_pass_season_proof.py.

What this module is
-------------------
`battle_pass_season_proof.py` is a standalone proof script, NOT a pytest
module (server/conftest.py does not collect it, and its name does not match
`test_*.py`). It exists because a bug class slipped through the committed
battle-pass tests: the season rollover was read back from the in-process
store, which hands out a fresh dict per access, so a route that computed the
right answer in memory but never wrote the row looked perfectly green. The
proof re-reads the row with its own SQL.

These tests cover the proof's own logic -- the judgement calls it makes --
which is testable without a database.

The database seam
-----------------
Three module-level readers talk to PostgreSQL directly:

    raw_state(pid)     the player's stored extra_state, decrypted
    raw_season(pid)    just the stored season_id, built on raw_state
    claim_rows(pid)    the once-only claim receipts

Each proof resolves those names as MODULE GLOBALS at call time, so a test
substitutes an in-memory reader and then drives the real routes and the real
in-memory store. Everything downstream of the seam -- the rollover, the
history write, the once-only claim, the clock arithmetic -- is the shipped
code, untouched. This is the same substitution the proof itself makes when a
deployment stores the column encrypted.

Proves-the-proof discipline
---------------------------
A test suite that only ever sees the healthy path cannot tell a working
proof from one that returns None unconditionally. Every proof here has at
least one test that sabotages the state it inspects and asserts the proof
REPORTS a failure. A proof that cannot fail is not a proof.

Measured defects this file pins
-------------------------------
Re-measured 2026-10-04 against the live app, not carried forward:

  1. FIXED. `GET /battle-pass/{player_id}/summary` IS mounted and IS declared in
     `battle_pass_routes.ROUTE_CONTRACT`, so `proof_summary` can return None
     against the live app. This file now pins the fix rather than the gap
     (test_proof_summary_live_app_serves_the_summary_route). An earlier version
     of this docstring still claimed the route was absent and cited
     test_proof_summary_live_app_has_no_summary_route, a name that no longer
     exists; that stale sentence is what kept this file being re-reported as
     unfixable.
  2. FIXED. `GET /battle-pass/season` answers `{"ok": true, "season":
     {...}}` -- the clock is nested, measured live. `proof_clock` unwraps it
     through `_clock_body` and returns None against the live app
     (test_proof_clock_passes_on_the_live_nested_payload). It now also REPORTS a
     payload missing a clock field instead of subscripting it, and main() wraps
     it so a fault becomes a FAIL line rather than a traceback
     (test_proof_clock_reports_a_payload_missing_a_clock_field,
     test_main_reports_a_clock_crash_instead_of_dying).

  3. WORTH KNOWING. `GET /battle-pass/{player_id}/summary` serves
     `{"ok": true, "season_summary": {...}}` -- nested too, and `proof_summary`
     already reads the nested key. Measured live 2026-10-05, so the clock fix
     is not a special case: this app's replies are envelopes by convention.

The canned summary bodies must track the proof's SHAPE, not just its status.
`_summary_session` serves `earned_free.tiers_claimed` because `proof_summary`
rejects a served row that is the raw stored record rather than the rendered
summary. When the proof gained that check before this helper gained the field,
every canned-summary test here failed with "earned_free has no tiers_claimed"
while the rollover itself worked correctly -- a helper that had fallen behind
the proof, not a defect in the season bank.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# server/ must be importable as a bare directory: the proof module and
# server/tests/wc_test_auth.py both rely on bare-name imports (`battle_pass`,
# `main`, `auth`) resolving to the same module objects main.py binds. See
# wc_test_auth.py's docstring -- importing server.auth instead gives a second
# store, and every token minted through it comes back 401.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVER_DIR = _REPO_ROOT / "server"
for _p in (str(_REPO_ROOT), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import battle_pass_season_proof as proof  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from main import app, players_db  # noqa: E402
import battle_pass  # noqa: E402
import battle_pass_season  # noqa: E402
from server.tests.wc_test_auth import clear_signup_rate_limit  # noqa: E402

# The three PostgreSQL readers, captured BEFORE the fixture below replaces them.
# Tests that exercise the readers themselves must call these, not the
# `proof.` attributes -- the fixture has swapped those for in-memory stand-ins
# by the time any test body runs.
REAL_RAW_STATE = proof.raw_state
REAL_CLAIM_ROWS = proof.claim_rows


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def in_memory_raw_state(pid):
    """Stand-in for the proof's PostgreSQL reader, backed by the live store.

    Returns a shallow copy, mirroring the real reader's contract: the store
    hands back a fresh dict per access, so a proof that mutated this copy
    would not be writing the player's row.
    """
    row = players_db[pid]
    return dict(row) if isinstance(row, dict) else {}


def in_memory_raw_season(pid):
    return (in_memory_raw_state(pid) or {}).get("battle_pass", {}).get("season_id")


def in_memory_claim_rows(pid):
    """The claim receipts the in-memory store holds, in the real SQL's order.

    The real reader selects `season_id, tier, level` ordered by season then
    level, so this returns (season_id, tier, level) triples sorted the same
    way. Tier is the claimed list the level appears in.
    """
    bp = in_memory_raw_state(pid).get("battle_pass", {})
    rows = []
    for tier_key, tier_name in (("claimed_free", "free"),
                                ("claimed_senior", "senior"),
                                ("claimed_super", "super")):
        for level in bp.get(tier_key) or []:
            rows.append((bp.get("season_id"), tier_name, level))
    return sorted(rows, key=lambda r: (str(r[0]), r[2]))


@pytest.fixture(autouse=True)
def _no_database(monkeypatch):
    """Point every proof at the in-memory readers for the whole test.

    Autouse rather than per-test so a proof can never be measured against the
    real PostgreSQL readers by accident: without this the readers open a
    socket, and a machine without a database would report a connection error
    where a verdict belongs.
    """
    monkeypatch.setattr(proof, "raw_state", in_memory_raw_state)
    monkeypatch.setattr(proof, "raw_season", in_memory_raw_season)
    monkeypatch.setattr(proof, "claim_rows", in_memory_claim_rows)


@pytest.fixture
def player():
    """A fresh player per test, signed in, with the signup limit cleared.

    Fresh per test because each proof mutates the player's stored season and
    claim state; a shared player would let one test's plant satisfy another
    test's precondition.
    """
    clear_signup_rate_limit()
    with TestClient(app) as client:
        r = client.post("/player/create", json={"name": "SeasonProofX",
                                                "spirit_root": "fire"})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        session = proof._AsPlayer(client, pid)
        yield session, pid
        clear_signup_rate_limit()


class FakeResponse:
    """The slice of a TestClient response the proofs actually read."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


class RoutingSession:
    """A signed-in session that serves canned replies for chosen paths.

    Real requests pass through to the live client -- so the rollover and the
    claim still happen for real -- and only the named path is intercepted.
    This is how the summary-route and clock checks are measured against a
    route that answers what the proof expects, without asserting a route the
    tree does not ship.
    """

    def __init__(self, session, canned):
        self._session = session
        self._canned = canned

    def get(self, url, **kw):
        for fragment, factory in self._canned.items():
            if fragment in url:
                return factory(url, kw)
        return self._session.get(url, **kw)

    def post(self, url, **kw):
        return self._session.post(url, **kw)

    def request(self, method, url, **kw):
        return self._session.request(method, url, **kw)


def correct_clock_payload():
    """The payload proof_clock expects: the clock at the top level."""
    return dict(battle_pass_season.season_clock())


# ---------------------------------------------------------------------------
# authentication surface -- the proof's own signing helper
# ---------------------------------------------------------------------------
def test_auth_headers_mints_a_bearer_authorization_header():
    """The proof signs in through the shared helper, not its own token code."""
    headers = proof.auth_headers("player_abc")

    assert set(headers) == {"Authorization"}, headers
    assert headers["Authorization"].startswith("Bearer "), headers
    assert headers["Authorization"] != "Bearer ", "an empty token proves nothing"


def test_as_player_sends_the_session_on_get_post_and_request():
    """Every verb the proofs use must carry the session.

    The battle-pass GET routes are auth-guarded. A wrapper that signed in for
    GET only would produce a 401 on the summary and clock reads, and a 401
    reads like a product failure rather than a missing header.
    """
    class Recorder:
        def __init__(self):
            self.calls = []

        def get(self, url, **kw):
            self.calls.append(("GET", kw.get("headers")))
            return FakeResponse()

        def post(self, url, **kw):
            self.calls.append(("POST", kw.get("headers")))
            return FakeResponse()

        def request(self, method, url, **kw):
            self.calls.append((method, kw.get("headers")))
            return FakeResponse()

    recorder = Recorder()
    session = proof._AsPlayer(recorder, "player_xyz")

    session.get("/battle-pass/player_xyz")
    session.post("/battle-pass/player_xyz/claim")
    session.request("GET", "/battle-pass/season")

    assert len(recorder.calls) == 3, recorder.calls
    for method, headers in recorder.calls:
        assert headers is not None, f"{method} sent no headers at all"
        assert headers["Authorization"].startswith("Bearer "), (method, headers)


def test_as_player_lets_an_explicit_header_win(player):
    """A proof that wants a deliberately bad token must be able to send one.

    The wrapper merges its session first and then lets the caller's headers
    overwrite it. If the session always won, no test could ever observe the
    unauthenticated case through this wrapper.
    """
    session, pid = player

    # A forged token must actually be refused -- otherwise "explicit headers
    # win" would be an unfalsifiable claim about a header nothing reads.
    bad = session.get(f"/battle-pass/{pid}",
                      headers={"Authorization": "Bearer not-a-real-token"})
    assert bad.status_code == 401, (
        f"a forged token was accepted ({bad.status_code} {bad.text}); the "
        f"override branch cannot be observed"
    )

    # And the session still works on the next call, so the override was not
    # sticky.
    good = session.get(f"/battle-pass/{pid}")
    assert good.status_code == 200, good.text


def test_battle_pass_reads_reject_a_request_with_no_session(player):
    """The guard the proof's signing helper exists to satisfy is real.

    Without this, proof._AsPlayer passing its own test would prove nothing
    about the product: a route that served everyone would also pass.
    """
    _, pid = player
    with TestClient(app) as anon:
        refused = anon.get(f"/battle-pass/{pid}")
    assert refused.status_code == 401, (
        f"an unauthenticated read answered {refused.status_code}, so the "
        f"proof's session handling is not protecting anything"
    )


# ---------------------------------------------------------------------------
# connection settings
# ---------------------------------------------------------------------------
def test_pg_dsn_defaults_and_env_override(monkeypatch):
    """Credentials come from the environment; a literal would leak on clone."""
    for var in ("WC_PG_HOST", "WC_PG_PORT", "WC_PG_DB", "WC_PG_USER",
                "WC_PG_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    defaults = proof.pg_dsn()
    assert defaults == {
        "host": "127.0.0.1", "port": 5432, "dbname": "wayfarers",
        "user": "wayfarers", "password": "",
    }, defaults
    assert defaults["password"] == "", "a default password is a checked-in secret"

    monkeypatch.setenv("WC_PG_HOST", "db.internal")
    monkeypatch.setenv("WC_PG_PORT", "6543")
    monkeypatch.setenv("WC_PG_DB", "wc_test")
    # A deliberately fake value that still proves the env var is read. Not a
    # real credential, and not even password-shaped: the module's own docstring
    # argues against putting password literals in checked-in files, and a test
    # that models the good behaviour should not model the bad one either.
    monkeypatch.setenv("WC_PG_USER", "wc_user")
    monkeypatch.setenv("WC_PG_PASSWORD", "from-the-environment-not-this-file")

    override = proof.pg_dsn()
    assert override["host"] == "db.internal", override
    assert override["port"] == 6543, "the port must be an int, not a string"
    assert isinstance(override["port"], int), type(override["port"])
    assert override["dbname"] == "wc_test", override
    assert override["user"] == "wc_user", override
    assert override["password"] == "from-the-environment-not-this-file", override


# ---------------------------------------------------------------------------
# raw_state -- the decryption and fallback branches
# ---------------------------------------------------------------------------
class _FakeCursor:
    """Covers both reader shapes: raw_state fetches one row, claim_rows all."""

    def __init__(self, row):
        self._row = row
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return self._row

    def fetchall(self):
        # raw_state wants one row; claim_rows wants a list of rows. A tuple of
        # tuples is a row list, a tuple of scalars is a single row.
        if self._row is None:
            return []
        if isinstance(self._row, tuple):
            if self._row and isinstance(self._row[0], tuple):
                return list(self._row)
            return [self._row]
        return list(self._row)


class _FakeConn:
    def __init__(self, row):
        self._cursor = _FakeCursor(row)
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _install_fake_psycopg2(monkeypatch, row):
    """Point psycopg2.connect at a fake connection returning `row`.

    `row` is what `fetchone()` yields: None for a missing player, otherwise a
    one-element tuple holding the extra_state column.
    """
    conn = _FakeConn(row)
    import psycopg2

    monkeypatch.setattr(psycopg2, "connect", lambda **kw: conn)
    return conn


def test_raw_state_returns_none_when_the_player_row_is_absent(monkeypatch):
    """A missing player is None, not {} -- the two mean different things.

    {} would make a proof read "no battle_pass key" for a player that does
    not exist, which is a database fault rather than a season fault.
    """
    _install_fake_psycopg2(monkeypatch, None)
    assert REAL_RAW_STATE("ghost") is None


def test_raw_state_returns_an_empty_dict_for_an_empty_column(monkeypatch):
    _install_fake_psycopg2(monkeypatch, (None,))
    assert REAL_RAW_STATE("p") == {}


def test_raw_state_passes_a_dict_column_through_untouched(monkeypatch):
    """A plaintext deployment stores a dict; it must survive as a dict."""
    _install_fake_psycopg2(monkeypatch, ({"battle_pass": {"season_id": "2026-10"}},))
    assert REAL_RAW_STATE("p") == {"battle_pass": {"season_id": "2026-10"}}


def test_raw_state_parses_a_plain_json_text_column(monkeypatch):
    """An unencrypted deployment stores a JSON string.

    PINNED LIMITATION: the reader decrypts FIRST and only parses JSON if the
    decrypt returned a string, and decrypt_extra_state never returns a string
    -- it returns {} on any failure, including on input that is not base64 at
    all. So a plaintext JSON column reads back as {} rather than as its
    contents. Measured here rather than assumed.

    This is safe for the proof's purpose: {} makes a proof report a FAIL
    line, so a plaintext deployment produces a loud false alarm, never a
    silent pass. But the proof cannot be trusted to VERIFY anything against a
    plaintext database, and the docstring's claim that it handles both
    deployments is not true today.
    """
    _install_fake_psycopg2(monkeypatch, ('{"battle_pass": {"season_id": "2026-09"}}',))
    assert REAL_RAW_STATE("p") == {}


def test_raw_state_returns_empty_dict_for_undecryptable_text(monkeypatch):
    """A corrupt or foreign-key column degrades to {} instead of raising.

    The proof's whole value is that it emits a verdict. An exception is not a
    verdict anyone reads, so this branch is what keeps a crypto fault from
    masquerading as a season fault.
    """
    _install_fake_psycopg2(monkeypatch, ("not-base64-encrypted-at-all",))
    assert REAL_RAW_STATE("p") == {}


@pytest.fixture
def encryption_key(monkeypatch):
    """A configured save-encryption key for the duration of one test.

    get_master_key() deliberately RAISES when neither WC_SAVE_ENCRYPTION_KEY nor
    WC_SAVE_ENCRYPTION_DEV_SEED is set, rather than inventing one from a
    literal in the source file. That refusal is correct, and these tests have
    to opt in explicitly to exercise the encrypted path at all.
    """
    monkeypatch.setenv("WC_SAVE_ENCRYPTION_DEV_SEED", "contract-test-only-seed")
    monkeypatch.delenv("WC_SAVE_ENCRYPTION_KEY", raising=False)
    from save_encryption import get_master_key

    return get_master_key()


def test_raw_state_decrypts_a_real_encrypted_column(monkeypatch, encryption_key):
    """Round-trip through the shipped encrypt/decrypt pair.

    Encrypt with the real function, read it back through the real reader, and
    the season survives. This is the deployment shape the proof exists for: a
    column no `json.loads` can read without the key, which is exactly the case
    that used to raise JSONDecodeError and made the proof only ever run where
    it could not fail.
    """
    from save_encryption import encrypt_extra_state

    state = {"battle_pass": {"season_id": "2026-05", "level": 7}}
    blob = encrypt_extra_state(state, "enc_player", encryption_key)
    assert isinstance(blob, str) and blob, "encryption produced nothing to store"

    _install_fake_psycopg2(monkeypatch, (blob,))
    assert REAL_RAW_STATE("enc_player") == state


def test_raw_state_returns_empty_dict_for_a_column_sealed_for_another_player(monkeypatch, encryption_key):
    """A column encrypted for a DIFFERENT player must not decrypt.

    The key is derived per player, so a cross-player read is a {} rather than
    someone else's progress. If this ever returned data, the reader would be
    a way to read another account's season state.
    """
    from save_encryption import encrypt_extra_state

    blob = encrypt_extra_state({"battle_pass": {"season_id": "2026-05"}},
                               "owner_player", encryption_key)
    _install_fake_psycopg2(monkeypatch, (blob,))
    assert REAL_RAW_STATE("thief_player") == {}


def test_raw_state_returns_empty_dict_for_a_tampered_ciphertext(monkeypatch, encryption_key):
    """Flipping one byte of the ciphertext must not yield a partial state.

    AES-GCM authenticates the payload, so a modified blob fails the tag check
    and reads as {} -- the reader cannot be fed a doctored season.
    """
    from save_encryption import encrypt_extra_state

    blob = encrypt_extra_state({"battle_pass": {"season_id": "2026-05"}},
                               "enc_player", encryption_key)
    flipped = "B" + blob[1:] if blob[0] != "B" else "C" + blob[1:]

    _install_fake_psycopg2(monkeypatch, (flipped,))
    assert REAL_RAW_STATE("enc_player") == {}


def test_raw_state_always_closes_its_connection(monkeypatch):
    """Every branch must close the connection, including the fallback ones."""
    for row in (None, (None,), ("garbage",), ({},)):
        conn = _install_fake_psycopg2(monkeypatch, row)
        REAL_RAW_STATE("p")
        assert conn.closed, f"connection left open for row {row!r}"


def test_raw_season_reads_the_stored_season_id(player):
    """raw_season is raw_state plus one key -- and tolerates its absence."""
    _session, pid = player

    assert in_memory_raw_state(pid).get("battle_pass") is None, \
        "a brand-new player is expected to have no battle_pass block yet"
    assert in_memory_raw_season(pid) is None

    with players_db.transaction(pid):
        players_db[pid]["battle_pass"] = {"season_id": "2026-07", "tier": "free",
                                          "xp": 0, "level": 1}
    assert in_memory_raw_season(pid) == "2026-07"


# ---------------------------------------------------------------------------
# claim_rows -- the ledger read
# ---------------------------------------------------------------------------
def test_claim_rows_orders_by_season_then_level_and_closes(monkeypatch):
    conn = _install_fake_psycopg2(
        monkeypatch,
        (("2026-10", "free", 5), ("2026-10", "free", 2), ("2026-09", "senior", 9)),
    )

    rows = REAL_CLAIM_ROWS("player_1")

    assert rows == [("2026-10", "free", 5), ("2026-10", "free", 2),
                    ("2026-09", "senior", 9)], rows
    assert conn.closed, "the ledger connection was left open"
    sql = conn._cursor.sql
    assert "battle_pass_claims" in sql, sql
    assert "player_id" in sql, "the ledger read is not scoped to one player"
    assert "ORDER BY" in sql.upper(), sql
    assert conn._cursor.params == ("player_1",), conn._cursor.params


# ---------------------------------------------------------------------------
# proof_reset -- the stale season must be replaced in the database
# ---------------------------------------------------------------------------
def test_proof_reset_passes_when_the_rollover_persists(player):
    """Healthy path: a stale stored season becomes the current one on a GET."""
    session, pid = player
    assert proof.proof_reset(session, pid) is None


def test_proof_reset_reports_a_season_that_survived_the_get(player, monkeypatch):
    """Sabotage: the database keeps the stale season. The proof must say so.

    This is the whole reason the module exists -- a route that computed the
    new season in memory but never wrote the row. Without this test a proof
    that returned None unconditionally would still be green.

    The plant is allowed through (the proof asserts on it) and every read
    AFTER it is frozen, which is precisely the bug: the row never moves.
    """
    session, pid = player
    planted = {"value": None}
    real_raw_season = in_memory_raw_season

    def frozen_after_plant(pid_):
        # First read confirms the plant; after that the row never changes.
        if planted["value"] is None:
            planted["value"] = real_raw_season(pid_)
            return planted["value"]
        return planted["value"]

    monkeypatch.setattr(proof, "raw_season", frozen_after_plant)

    problem = proof.proof_reset(session, pid)
    assert problem is not None, "the proof passed while the season never changed"
    assert problem.startswith("FAIL reset"), problem
    assert "keep their old season forever" in problem, problem


def test_proof_reset_raises_when_the_plant_never_landed(player, monkeypatch):
    """If the plant is not visible, the proof must not quietly continue.

    Asserting here means the "before" check has teeth: without it a probe
    that planted nothing would report the pre-existing season as proof.
    """
    session, pid = player

    monkeypatch.setattr(proof, "raw_season", lambda _pid: "never-planted")
    with pytest.raises(AssertionError) as exc:
        proof.proof_reset(session, pid)
    assert "plant failed" in str(exc.value), str(exc.value)


# ---------------------------------------------------------------------------
# proof_summary -- the finished season must be banked, not discarded
# ---------------------------------------------------------------------------
def _summary_session(session, final_level=4, summary_status=200):
    """Intercept the summary route, for the branches the real one cannot reach.

    NOW THAT THE ROUTE SHIPS this helper is only needed for the two states a
    healthy app cannot be driven into: a summary served at the WRONG level, and
    a summary endpoint answering something other than 200. Both are faults, so
    they are manufactured here rather than waited for.

    It used to stand in for the route's whole existence, because the route was
    absent -- that is the defect this file's ``test_proof_summary_live_app_...``
    now pins as FIXED, and it drives the real app instead. The canned body keeps
    ``earned_free.tiers_claimed`` because the proof checks that the served row is
    the RENDERED summary rather than the raw claim record; a stub without it
    fails that check for a reason that has nothing to do with what is under test.
    """
    known = battle_pass_season.previous_season_id(battle_pass_season.season_id())

    def summary_factory(url, kw):
        asked = (kw.get("params") or {}).get("season")
        if asked != known:
            return FakeResponse(404, {}, text="player never played that season")
        if summary_status != 200:
            return FakeResponse(summary_status, {}, text="summary unavailable")
        return FakeResponse(200, {"season_summary": {
            "season_id": known,
            "final_level": final_level,
            "earned_free": {"tiers_claimed": 3, "spirit_stones": 180,
                            "qi": 360, "gacha_tickets": 0},
        }})

    return RoutingSession(session, {"/summary": summary_factory})


def test_proof_summary_passes_when_the_finished_season_is_banked(player):
    """Healthy path, judged against a summary route that serves the history."""
    session, pid = player
    routed = _summary_session(session)
    assert proof.proof_summary(routed, pid) is None


def test_proof_summary_reports_a_season_silently_discarded(player, monkeypatch):
    """Sabotage: the rollover resets the track but banks no history.

    That is the pre-fix behaviour -- the player loses their level, their
    claimed tiers and their record of what they earned.
    """
    session, pid = player

    def drop_history(_pid):
        state = dict(in_memory_raw_state(_pid))
        state.pop("battle_pass_history", None)
        return state

    monkeypatch.setattr(proof, "raw_state", drop_history)

    problem = proof.proof_summary(_summary_session(session), pid)
    assert problem is not None, "the proof passed with no history in the database"
    assert "silently discarded" in problem, problem


def test_proof_summary_reports_a_paid_tier_that_carried_over(player, monkeypatch):
    """Sabotage: the new season starts on the tier the player bought."""
    session, pid = player

    def keep_tier(_pid):
        state = dict(in_memory_raw_state(_pid))
        bp = dict(state.get("battle_pass") or {})
        bp["tier"] = "senior"
        state["battle_pass"] = bp
        return state

    monkeypatch.setattr(proof, "raw_state", keep_tier)

    problem = proof.proof_summary(_summary_session(session), pid)
    assert problem is not None, "the proof passed with a carried-over paid tier"
    assert "carried into the new season" in problem, problem


def test_proof_summary_reports_a_track_that_did_not_reset(player, monkeypatch):
    """Sabotage: the season id changed but level and xp survived."""
    session, pid = player

    def keep_track(_pid):
        state = dict(in_memory_raw_state(_pid))
        bp = dict(state.get("battle_pass") or {})
        bp["level"] = 4
        bp["xp"] = 4000
        state["battle_pass"] = bp
        return state

    monkeypatch.setattr(proof, "raw_state", keep_track)

    problem = proof.proof_summary(_summary_session(session), pid)
    assert problem is not None, "the proof passed with an unreset track"
    assert "track was not reset" in problem, problem


def test_proof_summary_reports_a_summary_that_served_the_wrong_level(player):
    """Sabotage: the route serves a summary at a level the player never hit.

    A summary route that renders the wrong season is worse than one that
    404s, because the player is shown a record that never happened.
    """
    session, pid = player
    routed = _summary_session(session, final_level=99)

    problem = proof.proof_summary(routed, pid)
    assert problem is not None, "the proof passed on a wrong-level summary"
    assert "expected 4" in problem, problem


def test_proof_summary_live_app_serves_the_summary_route(player):
    """DEFECT FIXED: the summary route the proof reads is now MOUNTED.

    ``GET /battle-pass/{player_id}/summary`` was absent from the app's route
    table, so the live request answered 404 and ``proof_summary`` reported a
    rollover failure no matter what the rollover did. The database half of that
    proof was sound the whole time -- only the read had nowhere to land.

    The route is declared in ``battle_pass_routes.ROUTE_CONTRACT`` and mounted
    by ``battle_pass.register_battle_pass_routes``, which is what ``main.py``
    calls. This test now pins the fix rather than the gap: it drives the REAL
    app, crosses a season boundary, and reads the banked season back over the
    route the proof uses.
    """
    session, pid = player

    mounted = [getattr(r, "path", "") for r in app.routes
               if "battle-pass" in getattr(r, "path", "")]
    assert "/battle-pass/{player_id}/summary" in mounted, (
        f"the summary route is not mounted; proof_summary cannot pass. "
        f"mounted: {mounted}")

    # The contract promises it, so a route that mounts but is not declared is
    # the same defect wearing a different hat.
    import battle_pass_routes
    declared = {path for _m, path, _p in battle_pass_routes.ROUTE_CONTRACT}
    assert "/battle-pass/{player_id}/summary" in declared, (
        "the route is mounted but ROUTE_CONTRACT does not promise it")

    prev = battle_pass_season.previous_season_id(battle_pass_season.season_id())

    # A season the player never played is a 404, not an empty 200.
    missing = session.get(f"/battle-pass/{pid}/summary",
                          params={"season": prev})
    assert missing.status_code == 404, missing.text

    # Plant a finished season, cross the boundary, and read it back.
    with players_db.transaction(pid):
        players_db[pid]["battle_pass"] = {
            "season_id": prev, "tier": "senior", "xp": 4000, "level": 4,
            "claimed_free": [1, 2, 3], "claimed_senior": [1, 2],
            "claimed_super": [], "weekly_bonus_claimed": [],
            "purchased_at": None,
        }
    assert session.get(f"/battle-pass/{pid}").status_code == 200

    served = session.get(f"/battle-pass/{pid}/summary", params={"season": prev})
    assert served.status_code == 200, served.text
    row = served.json()["season_summary"]
    assert row["season_id"] == prev, row
    assert row["final_level"] == 4, row
    # The rendered summary, not the raw claim record.
    assert "tiers_claimed" in row["earned_free"], row

    # And the proof itself now returns a verdict on the real app.
    assert proof.proof_summary(session, pid) is None


# ---------------------------------------------------------------------------
# proof_claim -- a receipt must be a row, and a retry must pay nothing
# ---------------------------------------------------------------------------
class DoublePayingSession:
    """A session whose retry genuinely pays again -- the double-credit bug.

    The first claim goes to the real route, so the once-only receipt really
    is written. The retry then grants the reward a second time, which is what
    a client that double-taps (or a route whose guard was removed) produces.
    """

    def __init__(self, session, pid):
        self._session = session
        self._pid = pid
        self.retry_key = "proof-claim-2-DIFFERENT"

    def get(self, url, **kw):
        return self._session.get(url, **kw)

    def request(self, method, url, **kw):
        return self._session.request(method, url, **kw)

    def post(self, url, **kw):
        key = (kw.get("headers") or {}).get("Idempotency-Key")
        response = self._session.post(url, **kw)
        if key == self.retry_key and response.status_code == 200:
            paid = response.json()["reward"]["spirit_stones"]
            with players_db.transaction(self._pid):
                battle_pass.grant_stones(players_db[self._pid], paid)
        return response


def test_proof_claim_passes_when_a_retry_pays_nothing(player):
    """Healthy path: one claim row, one payment, retry marked as a replay."""
    session, pid = player
    assert proof.proof_claim(session, pid) is None
    assert len(in_memory_claim_rows(pid)) == 1, in_memory_claim_rows(pid)


def test_proof_claim_reports_a_retry_that_paid_again(player):
    """Sabotage: the once-only guarantee fails and money moves twice.

    The retry carries a DIFFERENT idempotency key, which is the harder case
    and what a double-tapping client actually sends.
    """
    session, pid = player

    problem = proof.proof_claim(DoublePayingSession(session, pid), pid)
    assert problem is not None, "the proof passed while the retry paid twice"
    assert "paid again" in problem, problem


def test_proof_claim_reports_a_retry_not_marked_replayed(player, monkeypatch):
    """Sabotage: the receipt blocks the second payment but the client is told
    it was paid fresh, so a client-side ledger books two rewards."""
    session, pid = player

    class UnmarkedSession:
        def __init__(self, inner):
            self._inner = inner

        def get(self, url, **kw):
            return self._inner.get(url, **kw)

        def request(self, method, url, **kw):
            return self._inner.request(method, url, **kw)

        def post(self, url, **kw):
            response = self._inner.post(url, **kw)
            key = (kw.get("headers") or {}).get("Idempotency-Key")
            if key == "proof-claim-2-DIFFERENT" and response.status_code == 200:
                body = dict(response.json())
                body["replayed"] = False
                return FakeResponse(200, body)
            return response

    problem = proof.proof_claim(UnmarkedSession(session), pid)
    assert problem is not None, "the proof passed on an unmarked retry"
    assert "not marked replayed" in problem, problem


def test_proof_claim_reports_a_missing_receipt_row(player, monkeypatch):
    """Sabotage: the claim paid but no row reached battle_pass_claims.

    This is the exact hole the module was written for -- a payout whose only
    trace is process memory.
    """
    session, pid = player
    monkeypatch.setattr(proof, "claim_rows", lambda _pid: [])

    problem = proof.proof_claim(session, pid)
    assert problem is not None, "the proof passed with no ledger row"
    assert "did not reach the database" in problem, problem


def test_proof_claim_reports_a_second_row_from_the_retry(player, monkeypatch):
    """Sabotage: the retry paid nothing but still inserted a duplicate row.

    The row count is what the proof checks after the retry, so the duplicate
    has to appear on the second read, not the first -- the first read is
    asserted to be exactly 1.
    """
    session, pid = player
    reads = {"count": 0}

    def grow_on_retry_read(_pid):
        rows = list(in_memory_claim_rows(_pid))
        reads["count"] += 1
        if reads["count"] >= 2 and rows:
            rows.append(rows[0])
        return rows

    monkeypatch.setattr(proof, "claim_rows", grow_on_retry_read)

    problem = proof.proof_claim(session, pid)
    assert problem is not None, "the proof passed with a duplicate ledger row"
    assert "second row" in problem, problem


# ---------------------------------------------------------------------------
# proof_clock -- the season window is one exact UTC calendar month
# ---------------------------------------------------------------------------
class ClockSession:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self._status_code = status_code
        self.requested = []

    def get(self, url, **kw):
        self.requested.append(url)
        if self._status_code != 200:
            return FakeResponse(self._status_code, {}, text="no clock")
        return FakeResponse(200, self._payload or correct_clock_payload())

    def post(self, url, **kw):  # pragma: no cover - proof_clock never posts
        raise AssertionError("proof_clock must not make a write request")

    def request(self, method, url, **kw):  # pragma: no cover
        raise AssertionError("proof_clock must not use request()")


def test_proof_clock_passes_on_a_correct_window():
    """Healthy path: route and clock module agree on one whole month."""
    assert proof.proof_clock(ClockSession()) is None


def test_proof_clock_reads_the_documented_public_path():
    """The proof must read /battle-pass/season, and only read it."""
    session = ClockSession()
    proof.proof_clock(session)
    assert session.requested == ["/battle-pass/season"], session.requested


def test_proof_clock_reports_a_missing_route():
    session = ClockSession(status_code=404)
    problem = proof.proof_clock(session)
    assert problem is not None, "the proof passed on a 404"
    assert "answered 404" in problem, problem


def test_proof_clock_reports_a_window_the_clock_module_disagrees_with():
    """Sabotage: the route's start drifts an hour from the canonical month.

    This is the UTC-versus-local defect: eight hours on a UTC+8 host, which
    is what made the countdown wrong and machine-dependent.
    """
    payload = correct_clock_payload()
    payload["start"] = payload["start"] + 3600

    problem = proof.proof_clock(ClockSession(payload))
    assert problem is not None, "the proof passed on a shifted window"
    assert "the clock module says" in problem, problem


def test_proof_clock_reports_a_shortened_window_before_reaching_month_length():
    """A flat 30-day window on a 31-day month is caught -- by the bounds check.

    The proof checks `(start, end) == season_bounds(sid)` FIRST, so a tampered
    end is reported as a disagreement with the clock module before the
    month-length arithmetic below it ever runs. See the dead-code note below.
    """
    payload = correct_clock_payload()
    payload["end"] = payload["start"] + 30 * 86400

    problem = proof.proof_clock(ClockSession(payload))
    assert problem is not None, "the proof passed on a 30-day window"
    assert "the clock module says" in problem, problem


def test_proof_clock_month_length_and_midnight_checks_are_unreachable():
    """PINS DEAD CODE: two checks can never fail, because an earlier one wins.

    proof_clock compares the route's window against season_bounds(sid) before
    it checks the window's length or its start instant. If those two agree,
    then by construction `start` IS UTC midnight on the 1st and `end - start`
    IS the calendar month length -- so the two later branches are unreachable.

    This is harmless today (the earlier check is strictly stronger) but it
    means those checks cannot be exercised and cannot regress into protecting
    anything. The assertion pins the current control flow: if the bounds check
    is ever removed or weakened, the month-length branch becomes reachable and
    this test tells you to add real coverage for it.
    """
    import calendar

    # A correct payload satisfies every branch, including the two later ones.
    payload = correct_clock_payload()
    sid = payload["season_id"]
    assert time.strftime("%Y-%m-%d %H:%M", time.gmtime(payload["start"])) == \
        f"{sid}-01 00:00"
    month_len = calendar.monthrange(*(int(x) for x in sid.split("-")))[1]
    assert (payload["end"] - payload["start"]) / 86400.0 == month_len

    # And any window that would break either later check is already rejected
    # by the bounds check, so no payload reaches them.
    for shift_start, stretch in ((5 * 86400, 31 * 86400), (0, 30 * 86400)):
        tampered = dict(payload)
        tampered["start"] = payload["start"] + shift_start
        tampered["end"] = tampered["start"] + stretch
        problem = proof.proof_clock(ClockSession(tampered))
        assert problem is not None, (
            "a tampered window was accepted, so the later clock checks are "
            "reachable and this pinning test must be rewritten"
        )
        assert "the clock module says" in problem, problem


def test_proof_clock_reports_a_negative_countdown():
    """Sabotage: the countdown runs backwards.

    This branch IS reachable: seconds_remaining is not covered by the bounds
    comparison, so a route reporting a negative countdown reaches its own
    check.
    """
    payload = correct_clock_payload()
    payload["seconds_remaining"] = -1

    problem = proof.proof_clock(ClockSession(payload))
    assert problem is not None, "the proof passed on a negative countdown"
    assert "countdown is negative" in problem, problem


def test_proof_clock_passes_on_the_live_nested_payload(player):
    """FIX PINNED: the live route nests the clock, and the proof reads it.

    GET /battle-pass/season answers {"ok": true, "season": {...}}. An earlier
    version of this test asserted the opposite -- that proof_clock raises
    KeyError there -- and went red the moment the proof was fixed, which is how
    a fixed defect kept being reported as open.

    Both the nesting assertion and the no-raise assertion are kept, because each
    fails differently: the first tells you the ROUTE changed shape, the second
    tells you the PROOF stopped unwrapping it.
    """
    session, _pid = player

    live = session.get("/battle-pass/season")
    assert live.status_code == 200, live.text
    body = live.json()
    assert "season" in body and isinstance(body["season"], dict), (
        f"the clock shape changed to {sorted(body)}; re-pin this test"
    )
    assert "season_id" not in body, (
        "the clock is now flat, so proof_clock reads the top level directly; "
        "re-pin this test"
    )

    problem = proof.proof_clock(session)
    assert problem is None, f"the proof failed on the live nested clock: {problem}"


def test_proof_clock_reports_a_payload_missing_a_clock_field():
    """Negative control: the missing-key report can actually fail.

    A renamed or dropped field is the same finding as the nested envelope was --
    the countdown a player reads is not there -- so it must be a FAIL line and
    not a KeyError traceback. If this ever returns None the report went dead.
    """
    payload = correct_clock_payload()
    del payload["seconds_remaining"]

    problem = proof.proof_clock(ClockSession(payload))
    assert problem is not None, "the proof passed on a payload with no countdown"
    assert "missing seconds_remaining" in problem, problem


def test_proof_clock_reports_a_non_object_payload():
    """A payload that is not a dict at all is reported, not subscripted."""
    problem = proof.proof_clock(ClockSession(["not", "an", "object"]))
    assert problem is not None, "the proof passed on a list payload"
    assert "not an object" in problem, problem


def test_main_reports_a_clock_crash_instead_of_dying(monkeypatch):
    """main() must convert ANY clock fault into a FAIL line.

    proof_clock is the only proof reading a payload shape it does not control,
    so it is the only one that can raise something other than AssertionError.
    Before this, such an exception escaped main() and the run ended in a
    traceback: the three verdicts already printed became the only record, and
    the exit code no longer described the season at all.
    """
    monkeypatch.setattr(proof, "proof_reset", lambda c, p: None)
    monkeypatch.setattr(proof, "proof_summary", lambda c, p: None)
    monkeypatch.setattr(proof, "proof_claim", lambda c, p: None)

    def boom(_client):
        raise TypeError("unhashable type: 'NoneType'")

    monkeypatch.setattr(proof, "proof_clock", boom)

    clear_signup_rate_limit()
    try:
        assert proof.main() == 1
    finally:
        clear_signup_rate_limit()


# ---------------------------------------------------------------------------
# main -- the exit code is the script's verdict
# ---------------------------------------------------------------------------
def test_main_returns_zero_when_every_proof_passes(monkeypatch):
    """Exit 0 means the season lifecycle landed. That is the whole contract."""
    monkeypatch.setattr(proof, "proof_reset", lambda c, p: None)
    monkeypatch.setattr(proof, "proof_summary", lambda c, p: None)
    monkeypatch.setattr(proof, "proof_claim", lambda c, p: None)
    monkeypatch.setattr(proof, "proof_clock", lambda c: None)

    clear_signup_rate_limit()
    try:
        assert proof.main() == 0
    finally:
        clear_signup_rate_limit()


def test_main_returns_one_and_keeps_going_after_a_failing_proof(monkeypatch):
    """One broken proof must not hide the state of the other three.

    main() collects every failure and reports a count, so a reviewer sees all
    four verdicts from one run instead of rediscovering them one at a time.
    """
    monkeypatch.setattr(proof, "proof_reset", lambda c, p: "FAIL reset: nope")
    monkeypatch.setattr(proof, "proof_summary", lambda c, p: None)
    monkeypatch.setattr(proof, "proof_claim", lambda c, p: "FAIL claim: nope")
    monkeypatch.setattr(proof, "proof_clock", lambda c: "FAIL clock: nope")

    clear_signup_rate_limit()
    try:
        assert proof.main() == 1
    finally:
        clear_signup_rate_limit()


def test_main_converts_an_assertion_into_a_reported_failure(monkeypatch):
    """An AssertionError inside a proof is a FAIL line, not a crash.

    The proofs assert on their own preconditions; an unconverted assertion
    would abort the run and hide the three proofs that had not run yet.
    """
    def boom(client, pid):
        raise AssertionError("plant failed, saw 'x'")

    monkeypatch.setattr(proof, "proof_reset", boom)
    monkeypatch.setattr(proof, "proof_summary", lambda c, p: None)
    monkeypatch.setattr(proof, "proof_claim", lambda c, p: None)
    monkeypatch.setattr(proof, "proof_clock", lambda c: None)

    clear_signup_rate_limit()
    try:
        assert proof.main() == 1
    finally:
        clear_signup_rate_limit()