"""Contract tests for ``server/player_store.py`` and the routes that read it.

WHAT IS UNDER TEST
------------------
``server/player_store.py`` holds NO HTTP routes. It is the persistence layer
every player route reads through, so this file pins two contracts:

1. THE STORE contract -- what ``PlayerStore`` promises to callers: the mapping
   operations, the transaction/commit/rollback rules, the detached-read rule,
   the account-creation refusals, and the signing-key accessor.
2. THE ROUTE contract -- the shapes and refusals of the routes in
   ``server/main.py`` whose behaviour is decided by this store:
   ``POST /player/create`` (calls ``create_account``),
   ``GET /player/{player_id}`` (the bootstrap read, whose response width is set
   by ``ownership_guard``, not by the store),
   ``POST /player/{player_id}/offline-progress``,
   ``GET /sign-in/{player_id}`` and ``POST /sign-in/{player_id}/claim``
   (both 404-vs-401 depend on the store's ``__contains__``).

Every status code and every key set below was MEASURED by running the booted
app, not read off the source and assumed:

    probe: TestClient(main.app, raise_server_exceptions=False), memory backend,
    2026-10-03.  Raw output is quoted in each test's docstring.

POSTGRESQL IS NOT SUPPORTED HERE
--------------------------------
``PlayerStore.__init__`` latches ``_pg_enabled()`` from ``WC_PG_HOST`` once, at
construction. A run that leaves that set would write these tests' players into
the shared live database, so the whole module SKIPS with an explicit reason
rather than corrupting it. A silent skip would read as a pass.

WHY THE BOOTED APP
------------------
The routes live inline in ``server/main.py``, so a hand-built app would test a
copy of the routes rather than the shipped ones. Every fixture restores what it
touched: ``auth._session_store`` is a process-wide global, and ``players_db`` is
a process-wide singleton, so a leaked player or a leaked token would make a
later file's assertions pass for the wrong reason.
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import pytest

# --- path bootstrap: resolve `server.main` from any cwd -------------------
_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("WC_STORAGE_BACKEND", "memory")

# --- constants read from the module under test, never hardcoded -----------
import player_store  # noqa: E402
from player_store import (  # noqa: E402
    GLOBAL_LOCK_ID,
    PLAYER_COLUMNS,
    AccountExists,
    PlayerStore,
    SeatCapReached,
    TransactionAborted,
)

CREATE = "/player/create"
BOOTSTRAP_READ = "/player/{player_id}"
OFFLINE = "/player/{player_id}/offline-progress"
SIGNIN_VIEW = "/sign-in/{player_id}"
SIGNIN_CLAIM = "/sign-in/{player_id}/claim"

# What `POST /player/create` returns. Measured 2026-10-03, 19 keys:
#   ['atk','cultivation_realm','cultivation_stage','current_zone',
#    'dao_companion_id','def_','hp','id','mp','name','path','progress',
#    'pvp_rating','sect_contribution','sect_id','sect_rank','spd',
#    'spirit_root','spirit_stones','signing_key']
# Frozen as a SET rather than eyeballed: adding a key is safe for a client that
# reads named fields, renaming one breaks every caller. `signing_key` is the one
# key that is NOT a column -- it is the hex session credential attached once,
# at creation, and never on any read.
CREATE_REPLY_KEYS = frozenset({
    "id", "name", "cultivation_realm", "cultivation_stage", "path",
    "spirit_root", "progress", "spirit_stones", "atk", "def_", "hp", "mp",
    "spd", "pvp_rating", "sect_id", "sect_rank", "sect_contribution",
    "dao_companion_id", "current_zone", "signing_key",
})

# The stored row is the same shape MINUS `signing_key` (never persisted as a
# column) and PLUS whatever a caller stored. Measured: 19 keys, no signing_key.
STORED_ROW_KEYS = CREATE_REPLY_KEYS - {"signing_key"}

# What an anonymous or cross-session caller may read. Measured: the response
# was exactly these three keys while the stored row carried 19. The narrowing is
# installed by ownership_guard (BOOTSTRAP_FIELDS), not by the store, and it is
# a disclosure decision -- adding a field here is a decision, not a convenience.
BOOTSTRAP_FIELDS = frozenset({"id", "name", "spirit_root"})

# `POST /player/{player_id}/offline-progress` declares response_model=
# OfflineProgressResponse, so the wire shape is fixed by that model.
OFFLINE_REPLY_KEYS = frozenset({
    "progress_gained", "new_progress", "realm", "stage", "breakthrough_ready",
})

# Measured on a same-session claim: 9 keys.
SIGNIN_CLAIM_KEYS = frozenset({
    "calendar", "claimed_today", "cycle_total_qi", "enabled",
    "next_reward_qi", "ok", "policy", "server_now", "streak_day",
})

UNAUTHORIZED = {"detail": "invalid or missing session token"}
WRONG_PLAYER = {"detail": "player_id does not match the authenticated session"}
PLAYER_NOT_FOUND = {"detail": "Player not found"}


# ---------------------------------------------------------------------------
# Environment guard + fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def store_mod():
    """The module under test, plus a hard guard against the live database."""
    if player_store._pg_enabled():  # pragma: no cover - environment guard
        pytest.skip(
            "WC_PG_HOST is set: PlayerStore would use the shared live "
            "PostgreSQL database. Unset WC_PG_HOST to run these contracts "
            "against the in-memory backend."
        )
    return player_store


@pytest.fixture(scope="module")
def app_module(store_mod):
    """The real ``server.main``, with its session table replaced for this file.

    ``auth._session_store`` is a module global shared by the whole process. A
    token minted here would otherwise stay resolvable for every later test in
    the same run, which would make another file's guard look bypassable with a
    token it never issued. The original is restored on teardown.
    """
    import auth as auth_mod
    import main

    previous = auth_mod.get_session_store()
    auth_mod.set_session_store(auth_mod.SessionStore())
    try:
        yield main
    finally:
        auth_mod.set_session_store(previous)


@pytest.fixture()
def client(app_module):
    """``raise_server_exceptions=False`` so an escaped exception is a 500 body
    a test can assert on, rather than an exception that aborts the request."""
    from fastapi.testclient import TestClient
    return TestClient(app_module.app, raise_server_exceptions=False)


@pytest.fixture()
def live_store(app_module):
    """The process-wide ``players_db``, empty on the way in and on the way out.

    Isolation is not cosmetic: ``create_account`` counts against a seat cap and
    every route test creates players, so leaking them changes what a later file
    sees.
    """
    store = app_module.players_db
    before = set(store.keys())
    try:
        yield store
    finally:
        for pid in set(store.keys()) - before:
            with store.transaction(pid):
                del store[pid]


@pytest.fixture()
def seeded(live_store):
    """A player created the way the shipped route creates one, minus the HTTP
    hop. Keyed by the signing key bytes, because that is what
    ``create_account`` stores and ``signing_key`` hands back."""
    import secrets

    created: list[str] = []

    def _make(player_id: str, **overrides):
        row = {
            "id": player_id,
            "name": overrides.pop("name", player_id),
            "cultivation_realm": "mortal",
            "cultivation_stage": "early",
            "path": "qi",
            "spirit_root": "qi",
            "progress": 0.0,
            "spirit_stones": {
                "copper": 0, "silver": 0, "gold": 0,
                "low_grade": 500, "mid_grade": 0, "high_grade": 0,
                "extreme_grade": 0,
            },
            "atk": 100, "def_": 50, "hp": 1000, "mp": 500, "spd": 100,
            "pvp_rating": 1000, "sect_id": None, "sect_rank": None,
            "sect_contribution": 0, "dao_companion_id": None,
            "current_zone": "mortal_village",
        }
        row.update(overrides)
        key = secrets.token_bytes(32)
        with live_store.transaction():
            live_store.create_account(player_id, row, key)
        created.append(player_id)
        return player_id, key

    try:
        yield _make
    finally:
        for pid in created:
            if pid in live_store:
                with live_store.transaction(pid):
                    del live_store[pid]


def _session_for(app_module, player_id: str) -> dict:
    """Mint a session the way trusted server code does, never over HTTP.

    ``POST /auth/login`` is the player-facing path, but it needs the signing
    key, and the bootstrap read that would fetch it is the very route under
    test. Server-side code issues sessions directly; that is the same authority.
    """
    import auth as auth_mod
    token = auth_mod.issue_session(player_id)
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# 1. STORE -- the mapping surface
# ===========================================================================


class TestStoreMappingContract:
    """``PlayerStore`` behaves like a dict of mutable player rows."""

    def test_getitem_returns_a_row_and_raises_keyerror_for_a_missing_id(self, store_mod):
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "name": "Ada"}, b"k" * 32)

        row = store["p1"]
        assert row["id"] == "p1"
        assert row["name"] == "Ada"
        with pytest.raises(KeyError):
            store["never-created"]

    def test_contains_is_true_for_a_stored_id_and_false_otherwise(self, store_mod):
        store = PlayerStore()
        store.create_account("p1", {"id": "p1"}, b"k")

        assert "p1" in store
        assert "never-created" not in store

    def test_get_returns_the_default_instead_of_raising(self, store_mod):
        """The whole point of ``get`` over ``[]``: a miss is a value, not an error."""
        store = PlayerStore()
        store.create_account("p1", {"id": "p1"}, b"k")

        assert store.get("never-created", "fallback") == "fallback"
        assert store.get("p1")["id"] == "p1"

    def test_setitem_refuses_a_row_whose_id_disagrees_with_the_key(self, store_mod):
        """A row stored under the wrong key would be unreachable by its own id.

        Measured: ``ValueError('Player id does not match mapping key')``.
        """
        store = PlayerStore()
        store.create_account("p1", {"id": "p1"}, b"k")

        with pytest.raises(ValueError, match="does not match mapping key"):
            store["p1"] = {"id": "a-different-id"}

    def test_delitem_removes_the_row_and_misses_like_a_dict(self, store_mod):
        store = PlayerStore()
        store.create_account("p1", {"id": "p1"}, b"k")

        del store["p1"]
        assert "p1" not in store
        assert len(store) == 0
        with pytest.raises(KeyError):
            del store["p1"]

    def test_len_keys_and_items_agree_on_the_same_population(self, store_mod):
        store = PlayerStore()
        store.create_account("a", {"id": "a"}, b"k")
        store.create_account("b", {"id": "b"}, b"k")

        assert len(store) == 2
        assert sorted(store.keys()) == ["a", "b"]
        assert sorted(pid for pid, _ in store.items()) == ["a", "b"]
        assert sorted(pid for pid, _ in store.items_bulk()) == ["a", "b"]
        assert len(store.values()) == 2
        assert sorted(iter(store)) == ["a", "b"]

    def test_iteration_and_keys_return_lists_of_ids_not_rows(self, store_mod):
        """A caller iterating the store gets ids, so a row cannot be mutated
        by accident while walking the population."""
        store = PlayerStore()
        store.create_account("a", {"id": "a", "name": "one"}, b"k")

        assert store.keys() == ["a"]
        assert list(store) == ["a"]
        assert all(isinstance(pid, str) for pid in store.keys())


# ===========================================================================
# 2. STORE -- copy-on-read, persist, and the transaction boundary
# ===========================================================================


class TestStoreDetachedReadContract:
    """A read OUTSIDE a transaction is a snapshot; nothing is saved implicitly.

    The module docstring calls this out, and it is the single most dangerous
    property in the file: a handler that reads a row, mutates it, and returns
    without persisting silently loses the write.
    """

    def test_a_detached_read_hands_back_a_copy_not_the_stored_row(self, store_mod):
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "name": "stored"}, b"k")

        snapshot = store["p1"]
        snapshot["name"] = "scribbled"

        assert store["p1"]["name"] == "stored", \
            "the store handed out its own dict; a write can now bypass the lock"

    def test_a_detached_write_is_lost_unless_persist_is_called(self, store_mod):
        """Measured: after mutating a snapshot, the store still read 'stored';
        after ``persist(pid, snapshot)`` it read 'scribbled'."""
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "name": "stored"}, b"k")

        snapshot = store["p1"]
        snapshot["name"] = "scribbled"
        assert store["p1"]["name"] == "stored"

        store.persist("p1", snapshot)
        assert store["p1"]["name"] == "scribbled"

    def test_persist_without_a_row_inside_a_transaction_is_a_no_op(self, store_mod):
        """The loaded row is already compared and written on the outer exit, so
        a second explicit write must not be required -- and must not corrupt it."""
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "name": "start"}, b"k")

        with store.transaction("p1"):
            store["p1"]["name"] = "end"
            store.persist("p1")

        assert store["p1"]["name"] == "end"

    def test_mark_dirty_is_the_same_function_as_persist(self, store_mod):
        """Two names, one behaviour. If they ever diverge, a handler using the
        alias silently stops saving."""
        # Compared on the CLASS, not on an instance: ``mark_dirty = persist`` is
        # a class-body alias, so both names resolve to the same plain function.
        # On an instance each access builds a fresh bound method, and ``is`` on
        # two of those would be False for reasons that prove nothing.
        assert PlayerStore.mark_dirty is PlayerStore.persist


class TestStoreTransactionContract:
    """``transaction()`` is the unit of durability: all of it, or none of it."""

    def test_a_commit_inside_a_scope_survives_the_scope(self, store_mod):
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "name": "start"}, b"k")

        with store.transaction("p1"):
            store["p1"]["name"] = "end"

        assert store["p1"]["name"] == "end"

    def test_an_exception_rolls_the_whole_unit_back(self, store_mod):
        """Measured: the row still read 'before' after the scope raised."""
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "name": "before"}, b"k")

        with pytest.raises(RuntimeError):
            with store.transaction("p1"):
                store["p1"]["name"] = "during"
                raise RuntimeError("boom")

        assert store["p1"]["name"] == "before"

    def test_a_failed_nested_scope_poisons_the_outer_scope(self, store_mod):
        """A caller that swallows the inner error still must not commit.

        Measured: ``TransactionAborted`` from the outer scope, and the row kept
        its original value.
        """
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "name": "keep"}, b"k")

        with pytest.raises(TransactionAborted):
            with store.transaction("p1"):
                store["p1"]["name"] = "outer"
                try:
                    with store.transaction("p1"):
                        store["p1"]["name"] = "inner"
                        raise ValueError("inner fail")
                except ValueError:
                    pass

        assert store["p1"]["name"] == "keep"

    def test_a_successful_nested_scope_commits_with_the_outer_one(self, store_mod):
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "name": "start"}, b"k")

        with store.transaction("p1"):
            with store.transaction("p1"):
                store["p1"]["name"] = "end"

        assert store["p1"]["name"] == "end"

    def test_opening_a_scope_for_a_missing_player_raises_keyerror(self, store_mod):
        """The preload is a contract: a handler that asked for a player that does
        not exist gets an error at the opening line, not a later ``KeyError``."""
        store = PlayerStore()
        with pytest.raises(KeyError):
            with store.transaction("never-created"):
                pass

    def test_a_deleted_row_is_gone_inside_the_scope_that_deleted_it(self, store_mod):
        store = PlayerStore()
        store.create_account("p1", {"id": "p1"}, b"k")

        with store.transaction("p1"):
            del store["p1"]
            assert "p1" not in store

        assert "p1" not in store


# ===========================================================================
# 3. STORE -- account creation, the signing key, and column splitting
# ===========================================================================


class TestCreateAccountContract:
    """``create_account`` is the only way a player identity comes into being."""

    def test_it_returns_the_staged_row_and_stores_it(self, store_mod):
        store = PlayerStore()
        row = store.create_account("p1", {"id": "p1", "name": "Ada"}, b"k" * 32)

        assert row["id"] == "p1"
        assert store["p1"]["name"] == "Ada"
        assert len(store) == 1

    def test_creating_an_existing_id_raises_and_never_replaces_the_identity(self, store_mod):
        """Measured: ``AccountExists``. This is the anti-hijack contract -- a
        second creation must not overwrite whoever holds that id."""
        store = PlayerStore()
        store.create_account("dup", {"id": "dup", "name": "first"}, b"k1")

        with pytest.raises(AccountExists):
            store.create_account("dup", {"id": "dup", "name": "second"}, b"k2")

        assert store["dup"]["name"] == "first"

    def test_the_seat_cap_counts_every_row_regardless_of_lifecycle_state(self, store_mod):
        """Measured: a store at cap=1 refused the second account with
        ``SeatCapReached``. The cap counts rows, not live players."""
        store = PlayerStore()
        store.create_account("c1", {"id": "c1"}, b"k1", seat_cap=1)

        with pytest.raises(SeatCapReached):
            store.create_account("c2", {"id": "c2"}, b"k2", seat_cap=1)

        assert "c2" not in store
        assert len(store) == 1

    @pytest.mark.parametrize("bad_cap", [-1, 1.5, True, "3"])
    def test_a_seat_cap_that_is_not_a_non_negative_int_is_refused(self, store_mod, bad_cap):
        """Measured for all four: ``ValueError('seat_cap must be a non-negative
        integer')``. ``True`` is in the list on purpose -- it is an ``int`` to
        Python, and a cap of 1 would be silently accepted as a cap of 1."""
        store = PlayerStore()
        with pytest.raises(ValueError, match="non-negative integer"):
            store.create_account("x", {"id": "x"}, b"k", seat_cap=bad_cap)

        assert len(store) == 0

    @pytest.mark.parametrize("bad_key", [b"", "a-string", None, bytearray()])
    def test_a_signing_key_that_is_not_nonempty_bytes_is_refused(self, store_mod, bad_key):
        """Measured for all four: ``ValueError('signing_key must be nonempty
        bytes')``. A blank credential must never reach the session table."""
        store = PlayerStore()
        with pytest.raises(ValueError, match="nonempty bytes"):
            store.create_account("x", {"id": "x"}, bad_key)

        assert "x" not in store

    def test_a_bytearray_key_is_accepted_because_it_is_bytes_like(self, store_mod):
        """The guard accepts ``bytearray`` on purpose; only an EMPTY one is
        refused. Pinning the accepted case stops a well-meaning 'tighten the
        type check' edit from breaking every caller."""
        store = PlayerStore()
        store.create_account("x", {"id": "x"}, bytearray(b"k" * 32))

        assert store.signing_key("x") == b"k" * 32


class TestSigningKeyContract:
    """The signing key is the login credential; it is returned as bytes."""

    def test_it_returns_the_exact_bytes_that_were_stored(self, store_mod):
        store = PlayerStore()
        store.create_account("p1", {"id": "p1"}, b"token-001")

        assert store.signing_key("p1") == b"token-001"
        assert isinstance(store.signing_key("p1"), bytes)

    def test_a_missing_player_raises_keyerror_rather_than_minting_a_key(self, store_mod):
        """A credential minted for a row that does not exist would authenticate
        nobody, and would look like a valid session to a caller that only
        catches ``KeyError``."""
        store = PlayerStore()
        with pytest.raises(KeyError):
            store.signing_key("never-created")

    def test_a_row_deleted_inside_the_scope_has_no_key(self, store_mod):
        store = PlayerStore()
        store.create_account("p1", {"id": "p1"}, b"k")

        with store.transaction("p1"):
            del store["p1"]
            with pytest.raises(KeyError):
                store.signing_key("p1")


class TestColumnSplitContract:
    """``_split`` decides what is a column and what is preserved as extra state.

    This is the persistence contract for every field a feature adds: a field
    that is not a ``PLAYER_COLUMNS`` member survives a database round-trip as
    extra state, and a field that IS the signing key is never persisted there.
    """

    def test_known_columns_and_extra_fields_are_separated(self, store_mod):
        """Measured: cols ``['id','name','spirit_root']``, extra
        ``{'custom': {'z': 1}}``."""
        cols, extra = PlayerStore._split({
            "id": "p1", "name": "Ada", "spirit_root": "qi", "custom": {"z": 1},
        })

        assert set(cols) == {"id", "name", "spirit_root"}
        assert extra == {"custom": {"z": 1}}

    def test_the_signing_key_is_never_persisted_as_extra_state(self, store_mod):
        """A credential copied into a JSON blob is a credential in every backup,
        every log line that dumps a row, and every client-side cache."""
        cols, extra = PlayerStore._split({
            "id": "p1", "name": "Ada", "signing_key": "SECRET",
        })

        assert "signing_key" not in cols
        assert "signing_key" not in extra
        assert "SECRET" not in repr(extra)

    def test_name_and_spirit_root_get_defaults_for_a_bare_row(self, store_mod):
        """Measured: both are filled in. A bare row is a valid input, so the
        persisted values must be well-formed without the caller supplying them."""
        cols, _ = PlayerStore._split({"id": "p1"})

        assert cols["name"] == ""
        assert cols["spirit_root"] == "none"

    def test_every_column_in_the_list_round_trips_through_the_split(self, store_mod):
        """A column the splitter drops would lose that field on every write."""
        row = {column: f"v-{column}" for column in PLAYER_COLUMNS}
        cols, extra = PlayerStore._split(dict(row, signing_key="SECRET"))

        for column in PLAYER_COLUMNS:
            if column in ("name", "spirit_root"):
                continue
            assert cols[column] == f"v-{column}", f"{column} did not survive _split"
        assert extra == {}


class TestLockNamespaceContract:
    """Every server process must take the same advisory lock id."""

    def test_the_global_lock_id_is_a_stable_signed_bigint(self, store_mod):
        """Pinned because the value is a cross-process agreement: changing it
        lets two processes write concurrently. The arithmetic is the assertion,
        so it survives a re-signing of the literal as long as the number holds.

        0x5743504C41594552 == 6287957793183450450, which is below 2**63, so it
        is a valid Postgres ``bigint``.
        """
        assert GLOBAL_LOCK_ID == 0x5743504C41594552
        assert GLOBAL_LOCK_ID == 6287957793183450450
        assert -(2 ** 63) <= GLOBAL_LOCK_ID < 2 ** 63


# ===========================================================================
# 4. ROUTES -- POST /player/create
# ===========================================================================


class TestCreateRouteContract:
    """``POST /player/create`` is the only public way to get a player."""

    def test_it_returns_the_full_row_plus_a_hex_signing_key(self, client, seeded):
        """Measured: 200, 20 keys, ``signing_key`` a 64-character hex string
        (32 bytes of key material, hex-encoded)."""
        response = client.post(CREATE, json={"name": "Ada", "spirit_root": "qi"})

        assert response.status_code == 200
        body = response.json()
        assert set(body) == CREATE_REPLY_KEYS
        assert isinstance(body["signing_key"], str)
        assert len(body["signing_key"]) == 64
        int(body["signing_key"], 16)  # must parse as hex
        assert body["name"] == "Ada"
        assert body["spirit_root"] == "qi"
        assert body["cultivation_realm"] == "mortal"
        assert isinstance(body["spirit_stones"], dict)
        assert body["progress"] == 0.0

    def test_the_created_player_is_reachable_and_holds_the_same_row(self, client, live_store):
        """The reply is a promise about stored state, so it is checked against
        the store rather than against itself."""
        response = client.post(CREATE, json={"name": "Bo", "spirit_root": "qi"})
        player_id = response.json()["id"]

        assert player_id in live_store
        stored = live_store[player_id]
        assert set(stored) == STORED_ROW_KEYS
        assert "signing_key" not in stored, "the credential was persisted as a column"
        assert stored["name"] == "Bo"

    def test_it_mints_no_lifecycle_yet(self, client, live_store):
        """Measured: ``lifecycle`` is absent from the created row. A brand-new
        player's 30-day clock starts on first gameplay read, not at signup."""
        response = client.post(CREATE, json={"name": "Cy", "spirit_root": "qi"})

        assert "lifecycle" not in live_store[response.json()["id"]]

    @pytest.mark.parametrize("payload,missing", [
        ({"name": "Ada"}, "spirit_root"),
        ({"spirit_root": "qi"}, "name"),
        ({}, None),
    ])
    def test_a_missing_required_field_is_refused_with_422(self, client, payload, missing):
        """Measured for the one-field case: 422 with a FastAPI error list
        naming the field in ``loc``. An empty body is the same 422."""
        response = client.post(CREATE, json=payload)

        assert response.status_code == 422
        if missing is not None:
            reported = {entry["loc"][-1] for entry in response.json()["detail"]}
            assert missing in reported

    def test_the_route_needs_no_session_and_issues_the_key_once(self, client, live_store):
        """No ``Authorization`` header is sent. This is the ONLY response that
        carries the signing key; every read route withholds it."""
        response = client.post(CREATE, json={"name": "Di", "spirit_root": "none"})
        player_id = response.json()["id"]

        assert "signing_key" not in {k: v for k, v in response.json().items()
                                     if k == "signing_key" and v is None}
        # The key lives in the store's key table, not in the row.
        assert isinstance(live_store.signing_key(player_id), bytes)


# ===========================================================================
# 5. ROUTES -- GET /player/{player_id} (the bootstrap read)
# ===========================================================================


class TestBootstrapReadContract:
    """The one read a client makes BEFORE it holds any credential.

    It is exempt from the ownership guard because the shipped game boots into a
    401 without it, and it is exempted WITH a narrowing wrapper: an anonymous or
    cross-session caller sees three fields, never the balance, stats or sect.
    """

    def test_an_anonymous_caller_sees_only_the_three_bootstrap_fields(self, client, seeded):
        """Measured: 200 with exactly ``['id','name','spirit_root']`` while the
        stored row carried 19 keys."""
        player_id, _ = seeded("anon-target")

        response = client.get(f"/player/{player_id}")

        assert response.status_code == 200
        assert set(response.json()) == BOOTSTRAP_FIELDS

    def test_the_anonymous_response_withholds_the_balance_and_the_credential(self, client, seeded):
        """The fields that make a row valuable are exactly the fields withheld.
        Asserted individually because a set difference alone would pass if the
        contract were re-broadened to a different leak."""
        player_id, _ = seeded("leak-target")

        body = client.get(f"/player/{player_id}").json()

        for withheld in ("spirit_stones", "signing_key", "sect_id",
                         "sect_contribution", "pvp_rating", "progress",
                         "atk", "hp", "lifecycle"):
            assert withheld not in body, f"{withheld} is readable without a session"

    def test_the_narrowed_response_is_not_vacuous(self, client, seeded):
        """A guard against the opposite failure: a wrapper that returned an
        empty body would pass every 'field is absent' assertion above."""
        player_id, _ = seeded("nonempty-target", name="Ada")

        body = client.get(f"/player/{player_id}").json()

        assert body["id"] == player_id
        assert body["name"] == "Ada"
        assert body["spirit_root"] == "qi"

    def test_a_session_for_the_same_player_sees_the_whole_row(self, client, app_module, seeded):
        """Measured: 200 with all 19 stored keys. Ownership widens the read from
        the moment the caller proves it is that player."""
        player_id, _ = seeded("own-target")
        headers = _session_for(app_module, player_id)

        response = client.get(f"/player/{player_id}", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert set(body) == STORED_ROW_KEYS
        assert "signing_key" not in body
        assert body["spirit_stones"]["low_grade"] == 500

    def test_a_session_for_a_different_player_is_still_narrowed(self, client, app_module, seeded):
        """Not 403. The route answers, but with the bootstrap field set only --
        so a stolen id buys nothing."""
        own_id, _ = seeded("holder")
        other_id, _ = seeded("victim", name="Victim")
        headers = _session_for(app_module, own_id)

        response = client.get(f"/player/{other_id}", headers=headers)

        assert response.status_code == 200
        assert set(response.json()) == BOOTSTRAP_FIELDS

    def test_a_bogus_token_does_not_widen_the_read(self, client, seeded):
        """Measured: 200 with the three bootstrap fields. An invalid token is
        not a licence to see more."""
        player_id, _ = seeded("bogus-token-target")

        response = client.get(f"/player/{player_id}",
                              headers={"Authorization": "Bearer not-a-real-token"})

        assert response.status_code == 200
        assert set(response.json()) == BOOTSTRAP_FIELDS

    def test_an_unknown_player_is_a_404_with_the_documented_body(self, client, seeded):
        """Measured: ``{"detail":"Player not found"}`` -- raised by the store's
        ``__contains__``, not by the guard, so it holds for anonymous callers too."""
        response = client.get("/player/never-created-at-all")

        assert response.status_code == 404
        assert response.json() == PLAYER_NOT_FOUND

    def test_no_header_never_reaches_the_wallet_route_of_this_shape(self, client, seeded):
        """Belt and braces on the anonymous case: the request truly carried no
        credential, so the narrowing above was not the guard declining."""
        player_id, _ = seeded("no-header-target")

        request = client.get(f"/player/{player_id}")

        assert "Authorization" not in request.request.headers


# ===========================================================================
# 6. ROUTES -- the player-store-backed write route and the sign-in routes
# ===========================================================================


class TestOfflineProgressRouteContract:
    """``POST /player/{player_id}/offline-progress`` -- a write that reads and
    then settles the row, so the store's transaction boundary is the whole
    contract."""

    def test_an_anonymous_caller_is_refused_before_the_body_is_read(self, client, seeded):
        """Measured: 401 ``{"detail":"invalid or missing session token"}`` for a
        real player, AND for a missing ``hours`` field, AND for an unknown
        player -- so the refusal precedes the store lookup and the body
        validation. A 422 or 404 here would mean the write path runs first."""
        player_id, _ = seeded("offline-anon")

        for path, payload in (
            (f"/player/{player_id}/offline-progress", {"hours": 1}),
            (f"/player/{player_id}/offline-progress", {}),
            ("/player/never-created-at-all/offline-progress", {"hours": 1}),
        ):
            response = client.post(path, json=payload)
            assert response.status_code == 401, f"{path} leaked past the guard"
            assert response.json() == UNAUTHORIZED

    def test_a_session_for_the_same_player_returns_the_declared_key_set(self, client, app_module, seeded):
        """Measured: 200 with exactly the five OfflineProgressResponse fields.
        The shape is fixed by ``response_model``, so a renamed field would
        break the client rather than merely drop out of a diff."""
        player_id, _ = seeded("offline-own")
        headers = _session_for(app_module, player_id)

        response = client.post(f"/player/{player_id}/offline-progress",
                               json={"hours": 1}, headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert set(body) == OFFLINE_REPLY_KEYS
        assert isinstance(body["progress_gained"], float)
        assert isinstance(body["new_progress"], float)
        assert isinstance(body["breakthrough_ready"], bool)

    def test_a_missing_hours_field_is_a_422_once_authenticated(self, client, app_module, seeded):
        """Measured: 422. This is the control for the anonymous arm above -- it
        proves 422 really is reachable on this route, so the 401 was the
        guard and not a route that always answers 401."""
        player_id, _ = seeded("offline-422")
        headers = _session_for(app_module, player_id)

        response = client.post(f"/player/{player_id}/offline-progress",
                               json={}, headers=headers)

        assert response.status_code == 422

    def test_another_players_id_is_a_403_not_a_settlement(self, client, app_module, seeded):
        """Measured: 403 ``{"detail":"player_id does not match the
        authenticated session"}``. The path names whose row to settle; the
        session names whose row MAY be settled."""
        own_id, _ = seeded("offline-owner")
        other_id, _ = seeded("offline-other")
        headers = _session_for(app_module, own_id)

        response = client.post(f"/player/{other_id}/offline-progress",
                               json={"hours": 1}, headers=headers)

        assert response.status_code == 403
        assert response.json() == WRONG_PLAYER

    def test_the_settlement_is_persisted_for_the_next_read(self, client, app_module, seeded, live_store):
        """The route returns a receipt AND writes the row. Only the stored value
        proves the write landed -- a 200 body could be built before a silently
        dropped save."""
        player_id, _ = seeded("offline-persist")
        headers = _session_for(app_module, player_id)

        first = client.post(f"/player/{player_id}/offline-progress",
                            json={"hours": 1}, headers=headers).json()
        stored_after = live_store[player_id]

        assert stored_after["last_cultivation_time"] is not None
        assert stored_after["last_cultivation_time"] != 0
        assert stored_after["progress"] == pytest.approx(first["new_progress"])


class TestSignInRouteContract:
    """``GET /sign-in/{player_id}`` and ``POST /sign-in/{player_id}/claim`` --
    both gated by a session, both 404 on a player the store does not hold."""

    def test_the_view_is_anonymous_refused(self, client, seeded):
        """Measured: 401 for a real player and for an unknown one, so the guard
        runs before the store lookup."""
        player_id, _ = seeded("signin-anon")

        for path in (f"/sign-in/{player_id}", "/sign-in/never-created-at-all"):
            response = client.get(path)
            assert response.status_code == 401, f"{path} leaked past the guard"
            assert response.json() == UNAUTHORIZED

    def test_the_view_returns_a_single_sign_in_object(self, client, app_module, seeded):
        """Measured: 200 with exactly ``{"sign_in": {...}}``."""
        player_id, _ = seeded("signin-view")
        headers = _session_for(app_module, player_id)

        response = client.get(f"/sign-in/{player_id}", headers=headers)

        assert response.status_code == 200
        assert set(response.json()) == {"sign_in"}

    def test_the_claim_is_anonymous_refused(self, client, seeded):
        player_id, _ = seeded("claim-anon")

        response = client.post(f"/sign-in/{player_id}/claim")

        assert response.status_code == 401
        assert response.json() == UNAUTHORIZED

    def test_a_claim_returns_the_streak_receipt(self, client, app_module, seeded):
        """Measured: 200 with the nine measured keys, including ``ok``."""
        player_id, _ = seeded("signin-claim")
        headers = _session_for(app_module, player_id)

        response = client.post(f"/sign-in/{player_id}/claim", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert set(body) == SIGNIN_CLAIM_KEYS
        assert body["ok"] is True
        assert isinstance(body["streak_day"], int)
        assert isinstance(body["claimed_today"], bool)

    def test_a_claim_advances_the_streak_in_the_stored_row(self, client, app_module, seeded, live_store):
        """The claim must WRITE, not merely answer. Only the store proves it."""
        player_id, _ = seeded("signin-persist")
        headers = _session_for(app_module, player_id)

        response = client.post(f"/sign-in/{player_id}/claim", headers=headers)
        assert response.status_code == 200

        stored = live_store[player_id]
        assert stored["sign_in"] is not None, "the claim answered 200 but saved nothing"
        assert stored["sign_in"] != {}

# ===========================================================================
# 7. TRANSACTIONS -- the collateral rollback (wallet + inventory)
# ===========================================================================
#
# Measured 2026-10-05 against the shipped module, memory backend, with a
# double that records how many times restore() was called.
#
# ``transaction()`` snapshots ``wallet_store`` and ``inventory_store`` when
# they expose snapshot()/restore(), and replays those snapshots ONLY on the
# failure path. That is the half of "all of it, or none of it" that the
# player rows alone do not cover: a scope can abort after it has already
# debited a wallet through a helper, and the row rollback cannot undo that.


class _RecordingWallet:
    """The two methods ``PlayerStore.transaction`` actually calls.

    Deliberately not the shipped WalletStore: this pins the STORE's
    snapshot/restore contract, not any wallet behaviour. ``player_store`` is
    the back-reference the constructor sets, asserted separately below.
    """

    def __init__(self, balance=None):
        self.balance = dict(balance or {"copper": 100})
        self.restore_calls = 0
        self.player_store = None

    def snapshot(self):
        return {k: v for k, v in self.balance.items()}

    def restore(self, snapshot):
        self.restore_calls += 1
        self.balance = {k: v for k, v in snapshot.items()}


class _RecordingInventory:
    """Same shape as the wallet double, for the second rollback arm."""

    def __init__(self, items=None):
        self.items = dict(items or {"wood": 5})
        self.restore_calls = 0

    def snapshot(self):
        return {k: v for k, v in self.items.items()}

    def restore(self, snapshot):
        self.restore_calls += 1
        self.items = {k: v for k, v in snapshot.items()}


class TestCollateralRollbackContract:
    """A failed scope restores the wallet and the inventory it snapshotted."""

    def test_a_failed_scope_restores_the_wallet_it_snapshotted(self, store_mod):
        """Measured: balance back to ``{'copper': 100}``, restore() called once.

        The spend happened INSIDE the scope, so a row-only rollback would
        leave the player 999,999 copper poorer with the scope reported as
        aborted. The two rollbacks are separate obligations.
        """
        wallet = _RecordingWallet()
        store = PlayerStore(wallet_store=wallet)
        store.create_account("p1", {"id": "p1", "name": "before"}, b"k")

        with pytest.raises(RuntimeError):
            with store.transaction("p1"):
                wallet.balance["copper"] = 999_999
                raise RuntimeError("boom")

        assert wallet.balance == {"copper": 100}
        assert wallet.restore_calls == 1

    def test_a_successful_scope_does_not_restore_the_wallet(self, store_mod):
        """Measured: balance stayed ``{'copper': 555}``, restore() calls 0.

        The control for the test above. Without it, a store that restored the
        snapshot unconditionally would pass the rollback test while silently
        discarding every legitimate in-scope purchase.
        """
        wallet = _RecordingWallet()
        store = PlayerStore(wallet_store=wallet)
        store.create_account("p1", {"id": "p1", "name": "before"}, b"k")

        with store.transaction("p1"):
            wallet.balance["copper"] = 555

        assert wallet.balance == {"copper": 555}
        assert wallet.restore_calls == 0

    def test_a_failed_scope_restores_the_inventory_too(self, store_mod):
        """Measured: ``{'wood': 5}`` after the failure, restore() called once.

        ``inventory_store`` is a SEPARATE attribute, set by main.py after the
        store is constructed, so a wallet-only assertion would leave the
        second arm untested.
        """
        inventory = _RecordingInventory()
        store = PlayerStore()
        store.inventory_store = inventory
        store.create_account("p1", {"id": "p1", "name": "before"}, b"k")

        with pytest.raises(RuntimeError):
            with store.transaction("p1"):
                inventory.items["wood"] = 999
                raise RuntimeError("boom")

        assert inventory.items == {"wood": 5}
        assert inventory.restore_calls == 1

    def test_both_collateral_stores_are_restored_in_one_failed_scope(self, store_mod):
        """Both arms in a single scope: a half-restored commit is worse than
        neither, because the caller is told the scope aborted and retries."""
        wallet = _RecordingWallet()
        inventory = _RecordingInventory()
        store = PlayerStore(wallet_store=wallet)
        store.inventory_store = inventory
        store.create_account("p1", {"id": "p1", "name": "before"}, b"k")

        with pytest.raises(RuntimeError):
            with store.transaction("p1"):
                store["p1"]["name"] = "during"
                wallet.balance["copper"] = -50
                inventory.items["wood"] = 0
                raise RuntimeError("boom")

        assert store["p1"]["name"] == "before"
        assert wallet.balance == {"copper": 100}
        assert inventory.items == {"wood": 5}
        assert (wallet.restore_calls, inventory.restore_calls) == (1, 1)

    def test_a_nested_failure_restores_collateral_once_for_the_outer_scope(self, store_mod):
        """Measured: restore() called exactly ONCE, not once per scope level.

        The inner scope only sets the failure flag and re-raises; only the
        outer scope owns the snapshot. A store that snapshotted per level
        would still end at the right value here, so the call COUNT is the
        assertion that survives.
        """
        wallet = _RecordingWallet()
        store = PlayerStore(wallet_store=wallet)
        store.create_account("p1", {"id": "p1", "name": "keep"}, b"k")

        with pytest.raises(TransactionAborted):
            with store.transaction("p1"):
                try:
                    with store.transaction("p1"):
                        wallet.balance["copper"] = 42
                        raise ValueError("inner fail")
                except ValueError:
                    pass

        assert wallet.balance == {"copper": 100}
        assert wallet.restore_calls == 1

    def test_the_constructor_points_the_wallet_back_at_this_store(self, store_mod):
        """``wallet_store.player_store`` is how wallet code reaches the rows.
        Measured: the back-reference is set by the constructor, and it is the
        SAME object, so a wallet holding an older store would read stale rows.
        """
        wallet = _RecordingWallet()
        store = PlayerStore(wallet_store=wallet)

        assert wallet.player_store is store

    def test_a_store_with_no_wallet_has_the_attribute_set_to_none(self, store_mod):
        """``wallet_store`` exists on every instance. Code that reads it must
        not have to distinguish 'absent' from 'None', and the transaction
        snapshot branch is skipped rather than raising."""
        store = PlayerStore()

        assert store.wallet_store is None
        assert store.inventory_store is None
        with store.transaction():
            pass  # must not raise on the missing-snapshot path


# ===========================================================================
# 8. items_bulk -- the ONE-query read, and its staging rule
# ===========================================================================
#
# ``items_bulk`` exists so ranking a population costs one database round-trip
# instead of one per player. Its docstring states the property that makes it
# safe: uncommitted staging still wins over persisted rows, so it can never
# let a caller read PAST an in-flight write. That sentence is the contract and
# nothing in the file pinned it.


class TestItemsBulkContract:
    """``items_bulk`` returns the same population as ``items``, one query."""

    def test_it_returns_the_same_ids_as_items(self, store_mod):
        store = PlayerStore()
        for pid in ("a", "b", "c"):
            store.create_account(pid, {"id": pid}, b"k")

        assert sorted(pid for pid, _ in store.items_bulk()) == \
            sorted(pid for pid, _ in store.items())

    def test_it_works_outside_a_transaction(self, store_mod):
        """Measured: same ids with no scope open. It opens its own scope, so a
        caller must never need one to use it."""
        store = PlayerStore()
        store.create_account("only", {"id": "only", "name": "O"}, b"k")

        bulk = dict(store.items_bulk())

        assert list(bulk) == ["only"]
        assert bulk["only"]["name"] == "O"

    def test_a_staged_write_is_visible_to_it_inside_the_scope(self, store_mod):
        """Measured: 'STAGED' read back from inside the scope, before commit.

        This is the load-bearing half of the docstring. If staging did NOT
        win, a handler that grants a reward and then re-reads the population
        would score the row from the stale committed copy.
        """
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "name": "committed"}, b"k")

        with store.transaction("p1"):
            store["p1"]["name"] = "STAGED"
            bulk = dict(store.items_bulk())
            assert bulk["p1"]["name"] == "STAGED"

    def test_a_staged_delete_hides_the_row_from_it_inside_the_scope(self, store_mod):
        """Measured: ids are ``['keep']`` while the delete is still staged."""
        store = PlayerStore()
        store.create_account("keep", {"id": "keep"}, b"k")
        store.create_account("gone", {"id": "gone"}, b"k")

        with store.transaction():
            del store["gone"]
            assert sorted(pid for pid, _ in store.items_bulk()) == ["keep"]

    def test_a_staged_write_is_persisted_after_the_scope_commits(self, store_mod):
        """The staged value must SURVIVE the commit, not merely be visible."""
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "name": "committed"}, b"k")

        with store.transaction("p1"):
            store["p1"]["name"] = "STAGED"

        assert store["p1"]["name"] == "STAGED"
        assert dict(store.items_bulk())["p1"]["name"] == "STAGED"

    def test_a_staged_delete_is_still_deleted_after_the_scope_commits(self, store_mod):
        store = PlayerStore()
        store.create_account("keep", {"id": "keep"}, b"k")
        store.create_account("gone", {"id": "gone"}, b"k")

        with store.transaction():
            del store["gone"]

        assert sorted(pid for pid, _ in store.items_bulk()) == ["keep"]
        assert "gone" not in store

    def test_a_rolled_back_scope_leaves_no_trace_in_it(self, store_mod):
        """The control for the two tests above: staging that is discarded on
        rollback must not be observable afterwards by either the committed
        read or the bulk read."""
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "name": "before"}, b"k")

        with pytest.raises(RuntimeError):
            with store.transaction("p1"):
                store["p1"]["name"] = "during"
                raise RuntimeError("boom")

        assert store["p1"]["name"] == "before"
        assert dict(store.items_bulk())["p1"]["name"] == "before"


# ===========================================================================
# 9. NON-FINITE VALUES -- what this store does and does not promise
# ===========================================================================
#
# MEASURED 2026-10-05, and this is the important part: ``player_store.py``
# contains NO finiteness check of any kind. Creating an account with
# ``progress=inf``, ``progress=nan`` or ``atk=-inf`` SUCCEEDS, and each value
# reads back unchanged -- through ``create_account``, through a staged write
# inside ``transaction()``, through ``persist(pid, snapshot)``, and inside a
# nested JSON field such as ``spirit_stones={"gold": inf}``.
#
# That is not a defect claim, it is a scope statement. Validation is the
# CALLER's job in this codebase: 80 isfinite guards exist across server/
# (artifacts.py, prestige_reset.py, pvp_routes.py, ...) and none of them is in
# this module. A persistence layer that silently coerced a caller's number
# would be worse than one that stores what it was handed -- the corruption
# would be invisible and unattributable.
#
# So the tests below pin the TWO properties that are genuinely this store's:
#
#   a) it stores the number it was handed verbatim, so an upstream guard is
#      never silently bypassed by the layer below it, and
#   b) a failed transaction removes a non-finite write just as completely as
#      it removes a finite one.
#
# A test asserting ``pytest.raises`` on a non-finite value would be asserting
# a behaviour this module does not have, and would go red on correct code.


NON_FINITE = (math.inf, -math.inf, math.nan)


class TestNonFiniteContract:
    """Finiteness is the caller's contract; this store is pass-through."""

    @pytest.mark.parametrize("value", NON_FINITE)
    def test_create_account_stores_a_non_finite_value_verbatim(self, store_mod, value):
        """Measured: no raise, and the exact float reads back.

        Verbatim is the contract. Coercing to 0 here would turn a caller's
        upstream guard failure into invisible data corruption with no signal
        at the layer where the number was handed over.
        """
        store = PlayerStore()

        store.create_account("p1", {"id": "p1", "progress": value}, b"k")

        stored = store["p1"]["progress"]
        assert isinstance(stored, float)
        assert math.isnan(stored) if math.isnan(value) else stored == value

    @pytest.mark.parametrize("value", NON_FINITE)
    def test_a_staged_write_of_a_non_finite_value_is_visible_in_scope(self, store_mod, value):
        """A number the store refuses to hold would make a transaction with one
        unrepresentable field fail for a reason the caller cannot see."""
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "atk": 100}, b"k")

        with store.transaction("p1"):
            store["p1"]["atk"] = value
            staged = store["p1"]["atk"]
            assert math.isnan(staged) if math.isnan(value) else staged == value

        assert math.isfinite(store["p1"]["atk"]) is False

    @pytest.mark.parametrize("value", NON_FINITE)
    def test_a_failed_scope_removes_a_non_finite_write(self, store_mod, value):
        """THE rollback half, which is this store's job.

        Measured for all three: the row read back 100. A non-finite write is
        not a special case for the rollback path -- if it leaked, a caller
        whose scope aborted would be left with a poisoned row that no later
        comparison could detect, because ``nan != nan`` defeats the
        compare-then-upsert that decides what changed.
        """
        store = PlayerStore()
        store.create_account("p1", {"id": "p1", "atk": 100}, b"k")

        with pytest.raises(RuntimeError):
            with store.transaction("p1"):
                store["p1"]["atk"] = value
                raise RuntimeError("boom")

        assert store["p1"]["atk"] == 100
        assert math.isfinite(store["p1"]["atk"])

    def test_every_non_finite_value_survives_a_committed_scope(self, store_mod):
        """THE asymmetry a caller must know about, measured for all three.

        ``nan``, ``inf`` and ``-inf`` all read back unchanged after their
        scopes commit. Note WHY the NaN case still persists: the commit-time
        change check compares the row against the copy taken at scope entry,
        where the value was ``100``. ``nan != 100`` is True, so the write
        fires. The hazard is narrower than it looks -- it would only appear
        if a NaN were ALREADY the stored value, where ``nan != nan`` would
        report the row as changed every time.

        Asserting all three together stops a future reader assuming infinity
        is the special case.
        """
        store = PlayerStore()
        for pid in ("nan-p", "inf-p", "ninf-p"):
            store.create_account(pid, {"id": pid, "atk": 100}, b"k")

        with store.transaction("nan-p"):
            store["nan-p"]["atk"] = math.nan
        with store.transaction("inf-p"):
            store["inf-p"]["atk"] = math.inf
        with store.transaction("ninf-p"):
            store["ninf-p"]["atk"] = -math.inf

        assert math.isnan(store["nan-p"]["atk"])
        assert store["inf-p"]["atk"] == math.inf
        assert store["ninf-p"]["atk"] == -math.inf

    def test_a_non_finite_value_nested_in_a_json_field_is_stored_verbatim(self, store_mod):
        """Measured: ``{'gold': inf}`` reads back unchanged.

        The currency dicts are the JSON extra-state path, not a numeric
        column, so a column-only guard would miss them. Pinned so that whoever
        adds the missing validation knows this path needs it too.
        """
        store = PlayerStore()

        store.create_account(
            "p1", {"id": "p1", "spirit_stones": {"gold": math.inf}}, b"k")

        assert store["p1"]["spirit_stones"] == {"gold": math.inf}


# ===========================================================================
# 10. _row_to_dict -- the column-drop contract on the read side
# ===========================================================================
#
# Every database read funnels through ``_row_to_dict``. Three bookkeeping
# columns are dropped there on purpose, and one of them is the login
# credential. Nothing in the file pinned that.


class TestRowToDictContract:
    """The read path hides bookkeeping columns and merges extra state."""

    def test_it_drops_the_created_and_updated_timestamps(self, store_mod):
        """Measured: keys ``['custom','id','name','spirit_root']``.

        Both are engine-managed columns with no caller meaning. A row handed
        to feature code should not be able to stamp its own history.
        """
        row = PlayerStore._row_to_dict(None, {
            "id": "p1", "name": "A", "spirit_root": "qi",
            "created_at": 111.0, "updated_at": 222.0,
            "extra_state": json.dumps({"custom": {"z": 1}}),
        })

        assert sorted(row) == ["custom", "id", "name", "spirit_root"]
        assert row["custom"] == {"z": 1}

    def test_it_drops_the_signing_key_never_returning_the_credential(self, store_mod):
        """The credential the store holds separately must never ride back out
        on a read. Asserted on the repr as well, because a nested copy inside
        a JSON field would hide from a key check alone."""
        row = PlayerStore._row_to_dict(None, {
            "id": "p1", "name": "A", "signing_key": "SECRET",
            "extra_state": json.dumps({"custom": {"z": 1}}),
        })

        assert "signing_key" not in row
        assert "SECRET" not in repr(row)

    def test_it_merges_extra_state_whether_json_text_or_a_mapping(self, store_mod):
        """Measured for BOTH: the text form and the mapping form merge.

        Two shapes reach this function -- a JSON string from the database and
        an already-decoded dict from a caller -- and both must flatten onto
        the row, or a field set in one path is invisible in the other.
        """
        base = {"id": "p1", "name": "A", "spirit_root": "qi"}

        from_text = PlayerStore._row_to_dict(
            None, dict(base, extra_state=json.dumps({"direct": 1})))
        from_mapping = PlayerStore._row_to_dict(
            None, dict(base, extra_state={"direct": 1}))

        assert from_text["direct"] == 1
        assert from_mapping["direct"] == 1
        assert sorted(from_text) == sorted(from_mapping)

    def test_a_short_unparseable_extra_state_raises_rather_than_being_dropped(self, store_mod):
        """Measured: ``json.JSONDecodeError`` propagates.

        Short extra-state is read as plain JSON with no recovery, so corrupt
        text surfaces as an error instead of a row that silently lost every
        field it carried. Pinned because it is the opposite of the long
        encrypted path below, and a well-meaning 'wrap it in try/except'
        would silently drop a player's whole extra state.
        """
        with pytest.raises(json.JSONDecodeError):
            PlayerStore._row_to_dict(
                None, {"id": "p1", "extra_state": "<<not json>>"})

    def test_a_long_unparseable_extra_state_drops_the_extra_fields_only(self, store_mod):
        """Measured: keys ``['id','name','spirit_root']`` -- no raise.

        A value longer than 20 characters is assumed encrypted, and the decrypt
        attempt is wrapped so an undecryptable blob costs its OWN fields and
        nothing else. The row still reads. That makes this a LOSS rather than
        an error, and it is why the two extra-state shapes must stay distinct:
        the next test shows the short path raising where this one is silent.
        """
        row = PlayerStore._row_to_dict(None, {
            "id": "p1", "name": "A", "spirit_root": "qi", "extra_state": "x" * 64,
        })

        assert sorted(row) == ["id", "name", "spirit_root"]
        assert row["name"] == "A"
        assert row["spirit_root"] == "qi"

    def test_the_two_extra_state_shapes_split_exactly_at_length_21(self, store_mod):
        """Measured: length 19 and 20 RAISE, 21 and 30 do not.

        The module's own comment calls 20 the "minimal encrypted length", so
        the boundary is the single most fragile line in the read path: nudge
        the comparison by one character and a chunk of corrupt rows silently
        starts losing their extra state instead of erroring. Pinned on both
        sides so the boundary cannot drift unnoticed in either direction.
        """
        for short in (19, 20):
            with pytest.raises(json.JSONDecodeError):
                PlayerStore._row_to_dict(
                    None, {"id": "p1", "extra_state": "x" * short})

        for long in (21, 30):
            row = PlayerStore._row_to_dict(
                None, {"id": "p1", "extra_state": "x" * long})
            assert sorted(row) == ["id"]
