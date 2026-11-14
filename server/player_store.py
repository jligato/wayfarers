"""Transactional player mapping.

Wrap each synchronous request in ``transaction()``. Returned rows are ordinary
mutable dicts; nested changes are saved automatically on successful exit. PG
reads outside a transaction are fresh, detached snapshots: to save those, pass
the snapshot to ``persist(pid, snapshot)`` (prefer a transaction for mutations).
All participating PG requests share GLOBAL_LOCK_ID, not a per-player lock.
Wallet SQL must use current_connection and must NOT commit/rollback it itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
import secrets
from threading import RLock, local
import sys
import time
from typing import Dict, Iterator, List, Optional, Tuple

from server.save_encryption import (
    encrypt_extra_state,
    decrypt_extra_state,
    get_master_key,
)

PLAYER_COLUMNS = [
    "id", "name", "cultivation_realm", "cultivation_stage", "path",
    "spirit_root", "progress", "spirit_stones", "atk", "def_", "hp", "mp",
    "spd", "pvp_rating", "sect_id", "sect_rank", "sect_contribution",
    "dao_companion_id",
]
_EXTRA_COL = "extra_state"
_EXTRA_KEYS = ("beasts", "beast_formation", "inventory", "wallet", "tower_state", "_last_tame_key")
# Stable signed bigint: every server process must use this same lock namespace.
GLOBAL_LOCK_ID = 0x5743504C41594552
_MISSING = object()


class SeatCapReached(ValueError):
    """The account cap includes every row, irrespective of lifecycle state."""


class AccountExists(ValueError):
    """Account creation never replaces an existing identity."""


class TransactionAborted(RuntimeError):
    """A nested scope failed, so the outer scope cannot commit."""


def _pg_enabled() -> bool:
    return bool(os.environ.get("WC_PG_HOST"))


def _get_conn():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("WC_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("WC_PG_PORT", 5432)),
        dbname=os.environ.get("WC_PG_DB", "wayfarers"),
        user=os.environ.get("WC_PG_USER", "wayfarers"),
        password=os.environ.get("WC_PG_PASSWORD", ""),
    )


class PlayerStore:
    def __init__(self, wallet_store=None):
        self.wallet_store = wallet_store
        self.inventory_store = None  # set by main.py after init_inventory_store
        self._mem: Dict[str, dict] = {}
        self._known: set = set()
        self._keys: Dict[str, bytes] = {}
        self._pg = _pg_enabled()
        self._local = local()
        self._lock = RLock()
        if wallet_store is not None:
            wallet_store.player_store = self

    @property
    def _state(self):
        return getattr(self._local, "state", None)

    @property
    def current_connection(self):
        """Dedicated PG connection in the calling thread's active scope, else None."""
        return self._state["conn"] if self._state is not None else None

    def _get_db(self):
        if self.current_connection is None:
            raise RuntimeError("Database access requires store.transaction()")
        return self.current_connection

    @contextmanager
    def transaction(self, player_id=None):
        """Join nested scopes; an exception in any scope poisons the whole unit.

        player_id optionally preloads a fresh row (missing IDs raise KeyError).
        This is thread-local and intended for synchronous request handlers.
        """
        state = self._state
        if state is not None:
            try:
                if player_id is not None:
                    self[player_id]
                yield self
            except BaseException:
                state["failed"] = True
                raise
            return
        with self._lock:
            conn = None
            wallet_snapshot = None
            inventory_snapshot = None
            try:
                if self.wallet_store is not None and hasattr(self.wallet_store, "snapshot"):
                    wallet_snapshot = self.wallet_store.snapshot()
                if self.inventory_store is not None and hasattr(self.inventory_store, "snapshot"):
                    inventory_snapshot = self.inventory_store.snapshot()
                if self._pg:
                    conn = _get_conn()
                    # READ COMMITTED guarantees a fresh read after waiting for lock.
                    conn.set_session(isolation_level="READ COMMITTED", autocommit=False)
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_xact_lock(%s)", (GLOBAL_LOCK_ID,))
                state = {"conn": conn, "rows": {} if self._pg else deepcopy(self._mem),
                         "originals": {}, "deleted": set(), "keys": deepcopy(self._keys),
                         "failed": False}
                self._local.state = state
                if player_id is not None:
                    self[player_id]
                yield self
                if state["failed"]:
                    raise TransactionAborted("Nested transaction failed")
                if self._pg:
                    for pid in state["deleted"]:
                        self._pg_delete_row(pid)
                    for pid, row in state["rows"].items():
                        if row != state["originals"].get(pid, _MISSING):
                            self._upsert(pid, row)
                    conn.commit()
                    # No process-wide PG row cache: never serve stale state.
                else:
                    # Publish detached copies only after every operation succeeded.
                    rows, keys = deepcopy(state["rows"]), deepcopy(state["keys"])
                    self._mem, self._keys = rows, keys
                    self._known = set(rows)
            except BaseException as exc:
                # Every participant is rolled back independently. One
                # step failing must not skip the rest: a wallet restore
                # that raises would otherwise leave the database
                # transaction open, still holding the advisory lock.
                # Secondary failures are attached to the original error,
                # never substituted for it.
                for label, err in self._rollback_all(
                        conn, wallet_snapshot, inventory_snapshot):
                    self._attach(exc, "rollback of %s failed: %r"
                                     % (label, err))
                raise
            finally:
                self._local.state = None
                if conn is not None:
                    try:
                        conn.close()
                    except BaseException as close_err:
                        pending = sys.exc_info()[0]
                        if pending is None:
                            raise
                        # An error is already unwinding: keep it as the
                        # single error the caller sees.
                        self._attach(pending,
                                         "database close failed: %r"
                                         % (close_err,))

    @staticmethod
    def _attach(exc: BaseException, message: str) -> None:
        """Record a secondary failure without replacing the original."""
        try:
            exc.rollback_notes.append(message)
        except AttributeError:
            try:
                exc.rollback_notes = [message]
            except Exception:
                return
        add_note = getattr(exc, "add_note", None)
        if add_note is not None:
            add_note(message)

    def _rollback_all(self, conn, wallet_snapshot, inventory_snapshot):
        """Undo every participant independently; report what failed.

        Returns a list of ``(label, error)`` for the steps that could not
        complete, so the caller can surface them without losing the
        original failure that triggered the rollback.
        """
        failures = []
        for label, store, snapshot in (
                ("wallet_store", self.wallet_store, wallet_snapshot),
                ("inventory_store", self.inventory_store, inventory_snapshot),
        ):
            if snapshot is None:
                continue
            try:
                store.restore(snapshot)
            except BaseException as err:
                failures.append((label, err))
        if conn is not None:
            try:
                conn.rollback()
            except BaseException as err:
                failures.append(("database", err))
        return failures

    @staticmethod
    def _split(player: dict) -> Tuple[dict, dict]:
        """Preserve all non-column fields in extra_state, except signing_key."""
        cols, extra = {}, {}
        for k, v in player.items():
            if k in PLAYER_COLUMNS:
                cols[k] = v
            elif k != "signing_key":
                extra[k] = v
        cols.setdefault("name", "")
        cols.setdefault("spirit_root", "none")
        return cols, extra

    def _row_to_dict(self, row) -> dict:
            d = dict(row)
            extra = d.pop(_EXTRA_COL, None)
            if extra:
                if isinstance(extra, str):
                    # Check if it's encrypted (base64 with version byte)
                    if extra and len(extra) > 20:  # minimal encrypted length
                        try:
                            master_key = get_master_key()
                            player_id = d.get("id", "")
                            if player_id:
                                extra = decrypt_extra_state(extra, player_id, master_key)
                            else:
                                extra = json.loads(extra)
                        except Exception:
                            extra = {}
                    else:
                        extra = json.loads(extra)
                d.update(extra)
            d.pop("signing_key", None)
            d.pop("created_at", None)
            d.pop("updated_at", None)
            return d

    def __getitem__(self, player_id: str) -> dict:
        state = self._state
        if state is None:
            if not self._pg:
                # COPY-ON-READ. A detached read must not hand out the stored
                # dict, or a caller can rewrite a row in place with no lock,
                # no transaction and no write-back. A caller that genuinely
                # wants to save uses `persist(pid, row)` or the transaction
                # path, which compare-then-upsert explicitly.
                with self._lock:
                    return deepcopy(self._mem[player_id])
            with self.transaction():
                return self[player_id]
        if player_id in state["deleted"]:
            raise KeyError(player_id)
        if player_id not in state["rows"]:
            row = self._fetch_row(player_id) if self._pg else None
            if row is None:
                raise KeyError(player_id)
            state["rows"][player_id] = row
            state["originals"][player_id] = deepcopy(row)
        return state["rows"][player_id]

    def __setitem__(self, player_id: str, value: dict) -> None:
        if self._state is None:
            with self.transaction():
                self[player_id] = value
            return
        if value.get("id", player_id) != player_id:
            raise ValueError("Player id does not match mapping key")
        if player_id not in self._state["originals"] and self._pg:
            old = self._fetch_row(player_id)
            self._state["originals"][player_id] = deepcopy(old) if old is not None else _MISSING
        self._state["deleted"].discard(player_id)
        self._state["rows"][player_id] = deepcopy(dict(value))

    def __contains__(self, player_id: object) -> bool:
        try:
            self[player_id]
            return True
        except KeyError:
            return False

    def __delitem__(self, player_id: str) -> None:
        if self._state is None:
            with self.transaction():
                del self[player_id]
            return
        self[player_id]  # Match dict's missing-key behavior.
        self._state["rows"].pop(player_id)
        self._state["keys"].pop(player_id, None)
        self._state["deleted"].add(player_id)

    def __len__(self) -> int:
        if self._state is None:
            with self.transaction():
                return len(self)
        if not self._pg:
            return len(self._state["rows"])
        # Count ALL account rows, then apply staged additions/deletions.
        ids = set(self._pg_ids())
        return self._pg_count() + len(set(self._state["rows"]) - ids) - len(ids & self._state["deleted"])

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def keys(self):
        if self._state is None:
            with self.transaction():
                return self.keys()
        ids = dict.fromkeys(self._pg_ids() if self._pg else [])
        ids.update(dict.fromkeys(self._state["rows"]))
        return [pid for pid in ids if pid not in self._state["deleted"]]

    def items(self) -> List[Tuple[str, dict]]:
        if self._state is None:
            with self.transaction():
                return self.items()
        return [(pid, self[pid]) for pid in self.keys()]

    def items_bulk(self) -> List[Tuple[str, dict]]:
        """Like :meth:`items`, but ONE database query instead of one per player.

        Uncommitted staging still wins over persisted rows, so this is only a
        read-path speedup, never a way to see past an in-flight write.
        """
        if self._state is None:
            with self.transaction():
                return self.items_bulk()
        by_id = {row["id"]: row for row in (self._pg_all_rows() if self._pg else [])}
        for pid in self._state.get("rows", {}):
            by_id[pid] = self._state["rows"][pid]
        for pid in self._state.get("deleted", set()):
            by_id.pop(pid, None)
        return list(by_id.items())

    def values(self):
        return [row for _, row in self.items()]

    def get(self, player_id: str, default=None):
        try:
            return self[player_id]
        except KeyError:
            return default

    def persist(self, player_id: str, player: Optional[dict] = None) -> None:
        if player is not None:
            if self._state is None or self._state["rows"].get(player_id) is not player:
                self[player_id] = player
        elif self._state is not None:
            self[player_id]  # Loaded rows are compared and saved on outer exit.
        elif self._pg:
            raise ValueError("Detached PG reads require persist(pid, player) or a transaction")
        else:
            # In-memory mode outside transaction: directly update _mem
            # This is needed for thread-local transaction state that TestClient worker threads can't share
            if player_id in self._mem:
                self._mem[player_id] = self._mem[player_id]  # No-op, but ensures _mem is current
            self[player_id]

    mark_dirty = persist

    def signing_key(self, player_id: str) -> bytes:
        if self._state is None:
            with self.transaction():
                return self.signing_key(player_id)
        if player_id in self._state["deleted"]:
            raise KeyError(player_id)
        if not self._pg:
            return self._state["keys"][player_id]
        with self._get_db().cursor() as cur:
            cur.execute("SELECT signing_key FROM players WHERE id = %s", (player_id,))
            row = cur.fetchone()
        if not row:
            raise KeyError(player_id)
        return bytes(row[0])

    def create_account(self, player_id: str, player_dict: dict, signing_key: bytes,
                       seat_cap: int = 10000) -> dict:
        """Insert identity + empty PG wallet atomically; return staged player dict.

        Raises SeatCapReached / AccountExists. In memory mode the caller creates
        the wallet inside this scope; a bound wallet_store snapshot rolls back
        creation, balance and idempotency together. Nested scopes never commit.
        """
        with self.transaction():
            if not isinstance(seat_cap, int) or isinstance(seat_cap, bool) or seat_cap < 0:
                raise ValueError("seat_cap must be a non-negative integer")
            if not isinstance(signing_key, (bytes, bytearray, memoryview)) or not signing_key:
                raise ValueError("signing_key must be nonempty bytes")
            if player_id in self:
                raise AccountExists(player_id)
            if len(self) >= seat_cap:
                raise SeatCapReached("Account seat cap reached")
            self[player_id] = player_dict
            row = self[player_id]
            if self._pg:
                self._upsert(player_id, row, signing_key=bytes(signing_key), insert_only=True)
                now = time.time()
                with self._get_db().cursor() as cur:
                    cur.execute(
                        "INSERT INTO wallets (player_id, balance, version, last_online_at, last_offline_claimed_at) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (player_id, psycopg2_json({}), 0, now, now),
                    )
                self._state["originals"][player_id] = deepcopy(row)
            else:
                self._state["keys"][player_id] = bytes(signing_key)
            return row

    def _fetch_row(self, player_id) -> Optional[dict]:
        import psycopg2.extras
        with self._get_db().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {', '.join(PLAYER_COLUMNS)}, {_EXTRA_COL} FROM players WHERE id = %s", (player_id,))
            row = cur.fetchone()
        return self._row_to_dict(row) if row is not None else None

    def _upsert(self, player_id: str, player: dict, signing_key=None, insert_only=False) -> None:
            cols, extra = self._split(player)
        
            # Encrypt extra_state before storing
            if extra and self._pg:
                master_key = get_master_key()
                extra = encrypt_extra_state(extra, player_id, master_key)
        
            with self._get_db().cursor() as cur:
                key = signing_key if signing_key is not None else self._existing_signing_key(cur, player_id)
                assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in PLAYER_COLUMNS if c != "id")
                conflict = "" if insert_only else f" ON CONFLICT (id) DO UPDATE SET {assignments}, extra_state = EXCLUDED.extra_state"
                cur.execute(
                    f"INSERT INTO players ({', '.join(PLAYER_COLUMNS)}, {_EXTRA_COL}, signing_key) "
                    f"VALUES ({', '.join(['%s'] * (len(PLAYER_COLUMNS) + 2))}){conflict}",
                    (player_id, cols.get("name", ""), cols.get("cultivation_realm", "mortal"),
                     cols.get("cultivation_stage", "early"), cols.get("path", "qi"),
                     cols.get("spirit_root", "none"), cols.get("progress", 0.0),
                     psycopg2_json(cols.get("spirit_stones", {})), cols.get("atk", 100),
                     cols.get("def_", 50), cols.get("hp", 1000), cols.get("mp", 500),
                     cols.get("spd", 100), cols.get("pvp_rating", 1000), cols.get("sect_id"),
                     cols.get("sect_rank"), cols.get("sect_contribution", 0),
                     cols.get("dao_companion_id"), psycopg2_json(extra), key),
                )

    def _existing_signing_key(self, cur, player_id: str):
        cur.execute("SELECT signing_key FROM players WHERE id = %s", (player_id,))
        row = cur.fetchone()
        return bytes(row[0]) if row and row[0] else secrets.token_bytes(32)

    def _pg_ids(self) -> List[str]:
        with self._get_db().cursor() as cur:
            cur.execute("SELECT id FROM players ORDER BY created_at DESC")
            return [r[0] for r in cur.fetchall()]

    def _pg_delete_row(self, player_id: str) -> None:
        """Delete a player and the rows that point at it.

        The dependent tables declare ``REFERENCES players(id)`` with no cascade,
        so deleting the player row alone raises ForeignKeyViolation and the
        whole delete rolls back. That is why earlier cleanup attempts silently
        left rows behind: the statement never succeeded. Delete children first.
        """
        with self._get_db().cursor() as cur:
            cur.execute(
                """
                SELECT tc.table_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND kcu.position_in_unique_constraint IS NOT NULL
                  AND tc.constraint_name IN (
                    SELECT constraint_name FROM information_schema.constraint_column_usage
                    WHERE table_name = 'players' AND column_name = 'id')
                """
            )
            children = [row[0] for row in cur.fetchall() if row[0] != "players"]
            for table in children:
                cur.execute(f'DELETE FROM "{table}" WHERE player_id = %s', (player_id,))
            cur.execute("DELETE FROM players WHERE id = %s", (player_id,))

    def _pg_all_rows(self) -> List[dict]:
        """Every player row in ONE query.

        Feature code that must score or rank the whole population used to call
        ``__getitem__`` per player, costing one database round-trip each. With
        thousands of saved players that is minutes of wall clock and looks
        exactly like a hang. Fetch once, then serve every read from the result.
        """
        import psycopg2.extras
        with self._get_db().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT {', '.join(PLAYER_COLUMNS)}, {_EXTRA_COL} FROM players ORDER BY created_at DESC")
            return [self._row_to_dict(row) for row in cur.fetchall()]

    def _pg_count(self) -> int:
        with self._get_db().cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM players")
            return cur.fetchone()[0]


def psycopg2_json(value):
    """Wrap a dict/list for a JSONB parameter. Lazy import for memory mode."""
    import psycopg2.extras
    return psycopg2.extras.Json(value)
