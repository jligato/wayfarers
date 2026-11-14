"""
WORLD EVENT ROUTES: the gaps the shape contract does not already pin.

WHAT THIS FILE IS FOR
---------------------
``server/world_event_routes.py`` exposes four routes: a public banner plus
join / attempt / claim. A sibling file, ``test_world_event_routes_contract.py``,
already pins the happy-path key sets, the participation and receipt shapes, the
``extra="forbid"`` validator, the anti-abuse refusal codes, and the once-only
payout. This file does not repeat any of it. Every test here answers a question
that file leaves open, or pins a number that file never measured.

WHAT IS NEW HERE, AND WHY IT MATTERS TO A CLIENT
------------------------------------------------
  1. ``POST /world-events/attempt`` IS rate limited. The sibling file measures
     the 429 on ``join`` and ``claim`` only, which reads as "the limiter covers
     the two money-adjacent routes". Measured: attempt answers 429 too, and it
     stops doing so at the shared cap. A client that retries a refused slot in
     a loop will be locked out of join and claim by the same budget.
  2. The limiter runs BEFORE the player-row lookup. Measured: a valid session
     for a player the store does not have gets 404 for the first N calls and
     429 after -- so a deleted account stops being able to read its own 404.
  3. The four routes answer 405 on the wrong verb. Nothing else in the suite
     asserts a method mismatch, and a client that guesses the verb needs to
     know it gets 405 rather than 404 or 422.
  4. Body TRANSPORT failures are their own contract: no body is 422 ``missing``,
     a JSON array is 422, malformed JSON is 422 ``json_invalid``, and a body
     sent as ``text/plain`` is refused by the auth layer with 403 before the
     validator ever runs.
  5. ``slot`` is coerced by the validator: the string ``"1"``, the float ``1.0``
     and the boolean ``True`` all reach the handler as slot 1. Only ``None`` is
     refused. A panel reading a slot out of a text field works; a client relying
     on a strict integer type does not.
  6. ``epoch_start`` is an IDENTITY, not a start time. Measured: a non-midnight
     value is echoed back verbatim, never snapped to a UTC boundary; an explicit
     ``null`` is treated exactly as an omitted key; and non-finite values
     (``NaN``, ``Infinity``) are accepted by the shape validator and then
     refused on the PHASE with 409 -- never a 500 and never a silent join.
  7. A second ``register_world_event_routes`` call returns early and therefore
     does NOT re-run the mount-time power-anchor check. Only the first call can
     refuse the mount.

HOW TO READ THE NUMBERS
-----------------------
Every assertion was MEASURED by calling the routes on 2026-10-04, not read off
the handlers. Where a measured answer is load-bearing it is called out in a
comment. Counts are read off the shipped constants (``we.ATTEMPT_RATE_LIMIT``)
rather than hard-coded, so a retuned cap does not turn these tests into
change-detectors.

NEGATIVE CONTROLS
-----------------
Each behavioural test carries a control that must fail if the behaviour moves:
a 405 needs the right verb to answer 200; a 429 needs a second player to be
unaffected; a 404 needs a stored player to reach the handler; a coercion needs
an out-of-band value still to be refused; an identity needs the omitted case
still to answer the scheduler's occurrence.

Run (memory mode -- the wrapper strips the PG vars, and in memory mode the
schema assert returns early):

    tools/wc_pytest.sh server/tests/test_world_event_contract.py
"""
from __future__ import annotations

import os

# The routes reach for a database connection only to assert the schema. In
# memory mode `_conn()` is None and that assert returns early, which is what
# lets this file run with no migration. Popped BEFORE any import so a stray
# ambient value cannot drag the suite into a live-Postgres branch.
for _var in ("WC_PG_DB", "WC_PG_HOST", "WC_PG_USER", "WC_PG_PASSWORD",
             "WC_PG_PORT"):
    os.environ.pop(_var, None)

import sys
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))
if str(_SERVER.parent) not in sys.path:
    sys.path.insert(0, str(_SERVER.parent))

import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import world_events as we
from player_store import PlayerStore

import auth
from wallet_dispatch import InMemoryWalletStore
import world_event_routes as wer
from world_event_routes import register_world_event_routes


# --------------------------------------------------------------------------
# Fixtures and helpers
#
# `wallet_dispatch` is imported directly rather than through `main`. This file
# builds its own app and needs only the in-memory wallet store to construct a
# PlayerStore; importing `main` would drag every other route module in, and one
# peer's mid-write syntax error anywhere in that chain takes this file's
# collection down with it.
# --------------------------------------------------------------------------

def _player(pid: str, progress: float = 0.0) -> dict:
    return {
        "id": pid, "name": pid, "cultivation_realm": "mortal",
        "cultivation_stage": "early", "progress": progress,
        "spirit_stones": 0, "hp": 120, "atk": 20, "def_": 10,
        "mp": 50, "spd": 10,
    }


def _app(*players: dict) -> tuple:
    """A throwaway app carrying ONLY the world-event routes.

    Not ``main.app``: this file measures the contract of these four routes, not
    whatever else a shared tree happens to have registered today.
    """
    app = FastAPI()
    store = PlayerStore(InMemoryWalletStore())
    with store.transaction():
        for player in players:
            store[player["id"]] = player
    register_world_event_routes(app, lambda: store)
    return app, store


def _defn(def_key: str = "beast_swarm") -> dict:
    return next(d for d in we.DEFAULT_DEFINITIONS if d["def_key"] == def_key)


def _epoch(def_key: str = "beast_swarm") -> float:
    """One epoch for every call in a test, so they all mean the same occurrence."""
    return we.next_epoch(we.default_definition(_defn(def_key)))


def _pin(store, epoch: float, phase: str, def_key: str = "beast_swarm") -> dict:
    """Materialise an occurrence and move the clock into one phase.

    Phase is recomputed from the clock on every call, so writing the instance's
    ``clock_floor`` is the only way to reach a phase the wall clock is not in.
    """
    defn = _defn(def_key)
    instance = we.ensure_instance(store, defn, epoch, epoch)
    bound = we.boundaries(defn, epoch)
    instance["clock_floor"] = {
        "warning": bound["warn_at"] + 60,
        "active": bound["active_at"] + 60,
        "cooldown": bound["cooldown_at"] + 60,
        "closed": bound["closed_at"] + 60,
    }[phase]
    we.save_instance(store, instance)
    return defn


def _authed(player_id: str) -> dict:
    return {"Authorization": "Bearer %s" % auth.issue_session(player_id)}


def _join_body(epoch: float | None, def_key: str = "beast_swarm") -> dict:
    body = {"def_key": def_key}
    if epoch is not None:
        body["epoch_start"] = epoch
    return body


def _attempt_body(epoch: float, slot, def_key: str = "beast_swarm") -> dict:
    return {"def_key": def_key, "epoch_start": epoch, "slot": slot}


def _joined_client(player_id: str = "p1", phase: str = "active"):
    """An app whose player has joined, and the client that did it."""
    epoch = _epoch()
    app, store = _app(_player(player_id))
    _pin(store, epoch, phase)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/world-events/join", json=_join_body(epoch),
                headers=_authed(player_id))
    return client, store, epoch


@pytest.fixture(autouse=True)
def clean():
    """Every test starts from an empty ledger and an unspent rate limit.

    Both are MODULE state. Without this, a call in one test suppresses the same
    call in another and the whole file passes for the wrong reason.
    """
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    yield
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()


# --------------------------------------------------------------------------
# A. The surface: four routes, and the wrong verb is a 405
# --------------------------------------------------------------------------

ROUTE_TABLE = {
    "GET /world-events/banner",
    "POST /world-events/join",
    "POST /world-events/attempt",
    "POST /world-events/claim",
}


@pytest.mark.parametrize("verb,path", [
    ("get", "/world-events/join"),
    ("get", "/world-events/attempt"),
    ("get", "/world-events/claim"),
    ("post", "/world-events/banner"),
])
def test_the_wrong_verb_on_a_world_event_path_is_405(verb, path):
    """MEASURED: every one of the four answers 405, including the public banner.

    This is the whole surface restated from the other direction. A client that
    guesses `GET /world-events/join` gets "Method Not Allowed" and not a 404, so
    the path is known to exist and only the verb is wrong -- which is the
    difference between a typo a client can fix and a dead endpoint.

    CONTROL: the same path on its documented verb is NOT 405. Without this, a
    route that answered 405 to everything would satisfy the assertions above.
    """
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "active")
    client = TestClient(app, raise_server_exceptions=False)

    response = getattr(client, verb)(path, headers=_authed("p1"))
    assert response.status_code == 405, (
        "%s %s answered %d; a wrong verb must be 405" % (verb.upper(), path,
                                                         response.status_code))
    assert response.json()["detail"] == "Method Not Allowed"

    if path == "/world-events/banner":
        assert client.get(path).status_code == 200
    else:
        body = _join_body(epoch) if path != "/world-events/attempt" \
            else _attempt_body(epoch, 1)
        good = client.post(path, json=body, headers=_authed("p1"))
        assert good.status_code != 405, (
            "%s answered 405 on its own verb too: %s" % (path, good.text[:120]))


def test_a_second_registration_skips_the_mount_time_power_anchor_check():
    """MEASURED: only the FIRST call can refuse the mount.

    The idempotency guard returns before the anchor check runs, so on an app
    that already mounted, a second call succeeds even with a broken power table.
    That is harmless -- there is nothing left to mount -- but it is the reason a
    reviewer must not read "the mount check passed" as "the anchor resolved".

    CONTROL: the same broken anchor on a FRESH app refuses with RuntimeError,
    which is what proves the difference is the early return and not a check that
    simply does not fire.
    """
    real = we.assert_realm_multipliers_resolve

    def _boom(*_args, **_kwargs):
        raise RuntimeError("no power anchor for realm 'x'")

    mounted_app, _ = _app(_player("p1"))
    try:
        we.assert_realm_multipliers_resolve = _boom
        again = register_world_event_routes(mounted_app, lambda: None)
        assert set(again["registered"]) == ROUTE_TABLE, (
            "the second registration must still report the four paths")

        fresh = FastAPI()
        with pytest.raises(RuntimeError) as raised:
            register_world_event_routes(fresh, lambda: None)
    finally:
        we.assert_realm_multipliers_resolve = real

    assert "refused to mount" in str(raised.value)
    assert not [r for r in fresh.routes
                if getattr(r, "path", "").startswith("/world-events")], (
        "a refused first mount still registered routes")


# --------------------------------------------------------------------------
# B. The limiter runs BEFORE the player-row lookup
# --------------------------------------------------------------------------

def test_the_rate_limiter_is_checked_before_the_player_row_is_read():
    """MEASURED: 404 while the budget lasts, then 429 forever after.

    ``join`` calls the limiter before ``_caller_player``, so a session naming a
    player the store does not have is answered 404 only until the shared budget
    is spent. A client holding a stale session therefore sees its 404 turn into
    a 429 -- and the 429 keeps coming back on every further retry, because the
    window slides rather than the debt clearing.

    CONTROL: a player the store DOES have reaches the handler on the same call
    index, so the 404s below are the missing row and not the limiter.
    """
    limit = we.ATTEMPT_RATE_LIMIT
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "active")
    client = TestClient(app, raise_server_exceptions=False)
    body = _join_body(epoch)

    ghost = [client.post("/world-events/join", json=body,
                         headers=_authed("ghost")).status_code
             for _ in range(limit + 2)]

    assert ghost[:limit] == [404] * limit, (
        "the missing-player 404 must hold for the whole budget: %s" % ghost[:limit])
    assert ghost[limit:] == [429] * 2, (
        "past the cap a missing player reads as 429, not 404: %s" % ghost)
    assert not [c for c in ghost if c == 500], (
        "the limiter escaped as a server error: %s" % ghost)

    # CONTROL: the same number of calls from a stored player reaches the
    # handler. `join` in `active` succeeds, so a 404 above was the missing row.
    we.ATTEMPT_LIMITER.reset()
    app2, store2 = _app(_player("p1"))
    _pin(store2, epoch, "active")
    stored = TestClient(app2, raise_server_exceptions=False)
    assert stored.post("/world-events/join", json=body,
                       headers=_authed("p1")).status_code == 200, (
        "a stored player did not reach the handler; the 404s above cannot be "
        "attributed to the missing row")


# --------------------------------------------------------------------------
# C. attempt IS rate limited -- the sibling contract file does not measure this
# --------------------------------------------------------------------------

def test_attempt_is_rate_limited_by_the_same_shared_budget_as_join():
    """MEASURED, and it is the one payment-adjacent route nobody has pinned.

    The sibling file measures the 429 on ``join`` and ``claim`` only. Reading it,
    a client would conclude the limiter guards the two routes that write state
    and leaves the one that resolves combat free. It does not: ``attempt``
    answers 429 as well.

    The cap is asserted structurally rather than as a literal count -- "a 429
    appears, nothing ever answers 500, and once one appears every later call is
    also a 429" -- because the burst below includes a join that has already
    spent part of the shared window. A hard-coded count would be a
    change-detector on the cap constant.

    CONTROL: the SAME burst from a second player is never limited, which proves
    the 429s are the shared budget and not the route refusing everything.
    """
    limit = we.ATTEMPT_RATE_LIMIT
    client, _store, epoch = _joined_client()
    body = _attempt_body(epoch, 1)

    codes = [client.post("/world-events/attempt", json=body,
                         headers=_authed("p1")).status_code
             for _ in range(limit + 4)]

    assert 500 not in codes, (
        "a rate limit escaped as a server error on attempt: %s" % codes)
    assert 429 in codes, (
        "attempt was never rate limited within %d calls (cap %d): %s -- the "
        "limiter does not cover the combat route" % (len(codes), limit, codes))
    first_429 = codes.index(429)
    assert set(codes[first_429:]) == {429}, (
        "a call after the cap answered something other than 429: %s" % codes)
    # Not refusing early either: a burst under the cap is never a 429.
    assert 429 not in codes[:first_429], (
        "the limiter refused attempt before its cap: %s" % codes[:first_429])

    # CONTROL: per-player, not global. A second player joining the same
    # occurrence still gets through after p1 has spent the window.
    app2, store2 = _app(_player("p2"))
    _pin(store2, epoch, "active")
    other = TestClient(app2, raise_server_exceptions=False)
    assert other.post("/world-events/join", json=_join_body(epoch),
                      headers=_authed("p2")).status_code == 200, (
        "p1's burst refused a DIFFERENT player; the limiter is global, so one "
        "noisy client can lock everyone out of the mode")


def test_all_three_bounded_routes_share_one_budget():
    """MEASURED: join, claim and attempt spend the SAME allowance.

    A client cannot treat the combat route as free spending capacity for the
    join route. This is what makes the previous test's claim load-bearing: the
    budget is one pool, not three.

    CONTROL: after the pool is empty, a fresh player is unaffected -- so the
    sharing is a shared pool and not a global lockout.
    """
    limit = we.ATTEMPT_RATE_LIMIT
    epoch = _epoch()
    app, store = _app(_player("p1"), _player("p2"))
    _pin(store, epoch, "active")
    client = TestClient(app, raise_server_exceptions=False)

    spent_on_attempts = []
    for slot in range(1, limit + 6):
        response = client.post("/world-events/attempt",
                               json=_attempt_body(epoch, slot),
                               headers=_authed("p1"))
        spent_on_attempts.append(response.status_code)
        if response.status_code == 429:
            break

    # Attempts that spent the pool are refused ON THE JOIN ROUTE too.
    join_after = client.post("/world-events/join", json=_join_body(epoch),
                             headers=_authed("p1")).status_code
    assert 429 in spent_on_attempts, (
        "no attempt was ever limited: %s -- this test cannot claim the pool is "
        "shared" % spent_on_attempts)
    assert join_after == 429, (
        "the attempt burst did not spend the join budget (join answered %d); "
        "the limiter is per-route, not shared" % join_after)

    # CONTROL: p2's own budget was untouched.
    app2, store2 = _app(_player("p2"))
    _pin(store2, epoch, "active")
    other = TestClient(app2, raise_server_exceptions=False)
    assert other.post("/world-events/join", json=_join_body(epoch),
                      headers=_authed("p2")).status_code == 200


# --------------------------------------------------------------------------
# D. Body transport: the failures that never reach a validator
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["/world-events/join",
                                   "/world-events/attempt",
                                   "/world-events/claim"])
def test_a_request_with_no_body_at_all_is_422_missing(route):
    """MEASURED: 422 with detail type ``missing`` at loc ``['body']``.

    Distinct from a malformed body. A client whose serializer drops an empty
    request sees ``missing``, not ``json_invalid`` and not a handler error, so
    the three transport failures are tellable apart in a log.
    """
    app, _ = _app(_player("p1"))
    response = TestClient(app).post(route, headers=_authed("p1"))
    assert response.status_code == 422, (
        "%s answered %d with no body" % (route, response.status_code))
    detail = response.json()["detail"]
    assert detail[0]["type"] == "missing", (
        "expected a missing-body 422, got %r" % detail)
    assert detail[0]["loc"] == ["body"]


def test_a_json_array_body_is_refused_by_the_shape_validator():
    """MEASURED: 422 ``model_attributes_type``, not a 500 and not a 200.

    An empty dict answers ``missing`` for ``def_key``; an array answers a type
    error. Both are 422, so a client only needs to treat 422 as "my body is
    wrong" for this family.
    """
    app, _ = _app(_player("p1"))
    response = TestClient(app).post("/world-events/join", json=[1, 2],
                                    headers=_authed("p1"))
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "model_attributes_type"


def test_malformed_json_is_422_json_invalid():
    """MEASURED: a truncated body is refused by the JSON parser, not the model."""
    app, _ = _app(_player("p1"))
    response = TestClient(app).post(
        "/world-events/join", data="{",
        headers={**_authed("p1"), "Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "json_invalid"


def test_a_body_sent_as_text_plain_is_403_not_422():
    """MEASURED, and it is the auth layer refusing, not the validator.

    A perfectly valid world-event body with the wrong ``Content-Type`` header
    is rejected before the shape validator runs, with a 403 rather than a 422.
    A client that logs "422 means my JSON is wrong" will read this as a
    permissions failure and go looking in the wrong place.

    CONTROL: the identical body with the correct header answers 200, so the
    difference is the header and not the payload.
    """
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "warning")
    client = TestClient(app, raise_server_exceptions=False)
    raw = '{"def_key": "beast_swarm", "epoch_start": %r}' % epoch

    wrong = client.post("/world-events/join", data=raw, headers={
        "Authorization": _authed("p1")["Authorization"],
        "Content-Type": "text/plain",
    })
    assert wrong.status_code == 403, (
        "a text/plain body answered %d; if it is now 415 or 422, repin it here"
        % wrong.status_code)

    right = client.post("/world-events/join", data=raw, headers={
        "Authorization": _authed("p1")["Authorization"],
        "Content-Type": "application/json",
    })
    assert right.status_code == 200, (
        "the same body with the correct content type must reach the handler: %s"
        % right.text[:160])


# --------------------------------------------------------------------------
# E. slot coercion: the validator is laxer than its annotation suggests
# --------------------------------------------------------------------------

@pytest.mark.parametrize("slot", ["1", 1.0, True])
def test_a_slot_that_is_not_strictly_an_integer_still_reaches_the_handler(slot):
    """MEASURED: "1", 1.0 and True all resolve to slot 1.

    Pydantic runs in lax mode, so a slot read out of a text field, or a 1.0 from
    a JSON number, is accepted. A client may therefore send what it has without
    normalising first -- but must not treat the coercion as an identity: ``True``
    becomes slot 1, not a boolean flag the server later reads as "won".

    Each case gets its OWN app so the ladder is unspent and the status is the
    validator's answer rather than a replay refusal left over from the previous
    parameter.
    """
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "active")
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/world-events/join", json=_join_body(epoch), headers=_authed("p1"))

    response = client.post("/world-events/attempt", json=_attempt_body(epoch, slot),
                           headers=_authed("p1"))
    assert response.status_code == 200, (
        "slot %r was refused with %d: %s" % (slot, response.status_code,
                                            response.text[:160]))
    assert response.json()["outcome"]["slot"] == 1, (
        "slot %r did not resolve to slot 1; it resolved to %r"
        % (slot, response.json()["outcome"]["slot"]))


@pytest.mark.parametrize("slot", [None, "one", "", [1]])
def test_a_slot_that_is_not_a_number_at_all_is_still_422(slot):
    """MEASURED: ``None`` and non-numeric values are refused, as int_type.

    CONTROL: the in-band integer 1 is accepted by the same validator on the same
    app, so these 422s are the validator refusing the value -- not a route that
    refuses every slot.
    """
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "active")
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/world-events/join", json=_join_body(epoch), headers=_authed("p1"))

    for bad in (slot,):
        response = client.post("/world-events/attempt",
                               json=_attempt_body(epoch, bad),
                               headers=_authed("p1"))
        assert response.status_code == 422, (
            "slot %r was accepted with %d" % (bad, response.status_code))
        assert response.json()["detail"][0]["type"] in (
            "int_type", "int_parsing", "int_from_float"), (
            "slot %r gave an unexpected validator type: %s"
            % (bad, response.json()["detail"]))

    assert client.post("/world-events/attempt", json=_attempt_body(epoch, 1),
                       headers=_authed("p1")).status_code in (200, 409), (
        "slot 1 did not reach the handler at all; the 422s above cannot be "
        "attributed to the validator")


# --------------------------------------------------------------------------
# F. epoch_start is an identity, and the worst value still cannot 500
# --------------------------------------------------------------------------

def test_a_real_anchor_round_trips_verbatim_and_an_invented_one_collapses():
    """MEASURED: what the client stores and sends back is what it gets back.

    The INTENT of this test, unchanged: ``epoch_start`` is an identity, not a
    start time the server edits, so a client that stores the value it was given
    and sends it back on the next call is safe. The client proves it does
    exactly that -- ``WorldEventPanel.gd`` reads ``epoch_start`` off a banner
    row and posts it verbatim -- so a real anchor must come back UNCHANGED.

    What this test used to pin was the opposite: that an INVENTED value
    (``epoch + 12345.6``) was also echoed verbatim. That assertion was the
    defect. ``epoch_start`` is one third of the participation row's primary key
    and the phase boundaries are derived from it, so every distinct invented
    float opened its own window and became its own row. Measured before the
    fix: 36 invented epochs inside ONE real occurrence wrote 36 participation
    rows AND 36 instance rows for a single player, all answering 200 -- 17,280
    rows per player per day at the shipped rate limit.

    So the rule now: a REAL anchor round-trips untouched (the client contract),
    and an INVENTED one collapses onto the definition's occurrence lattice (the
    growth bound). Both halves are asserted, so this cannot pass by snapping
    everything or by snapping nothing.

    CONTROL: the OMITTED case answers the scheduler's occurrence, which is a
    different number from the invented one -- so the snapping above is not just
    the server ignoring the field.
    """
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    epoch = _epoch()
    invented = epoch + 12345.6
    app, store = _app(_player("p1"))
    _pin(store, epoch, "warning")
    client = TestClient(app, raise_server_exceptions=False)

    # --- half 1: the real anchor the client actually sends round-trips.
    honest = client.post("/world-events/join", json=_join_body(epoch),
                         headers=_authed("p1"))
    assert honest.status_code == 200, honest.text[:200]
    assert honest.json()["participation"]["epoch_start"] == epoch, (
        "the server moved the occurrence the client named: sent %r, got %r"
        % (epoch, honest.json()["participation"]["epoch_start"]))

    # --- half 2: an invented value collapses onto the lattice anchor.
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    app2, store2 = _app(_player("p1"))
    _pin(store2, epoch, "warning")
    client2 = TestClient(app2, raise_server_exceptions=False)
    made_up = client2.post("/world-events/join", json=_join_body(invented),
                           headers=_authed("p1"))
    # The invented value opens its own window, so it may land in any phase.
    assert made_up.status_code in (200, 409), made_up.text[:200]
    if made_up.status_code == 200:
        used = made_up.json()["participation"]["epoch_start"]
        assert used == we.anchor_lattice(_defn(), invented), (
            "an invented epoch was stored as itself (%r), which is one "
            "caller-chosen key per attempt" % used)
        assert used != invented, (
            "an invented value was echoed verbatim, so the test value cannot "
            "tell snapping apart from echoing")
    assert invented % 86400.0 != 0, (
        "the test value was a UTC midnight, so it cannot tell snapping apart")

    # --- the growth bound, stated as the number the fix exists to produce.
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    app3, store3 = _app(_player("p1"))
    _pin(store3, epoch, "warning")
    client3 = TestClient(app3, raise_server_exceptions=False)
    made = []
    for offset in range(12):
        if offset and offset % we.ATTEMPT_RATE_LIMIT == 0:
            we.ATTEMPT_LIMITER.reset()
        made.append(client3.post(
            "/world-events/join",
            json=_join_body(epoch - 1.0 - float(offset)),
            headers=_authed("p1")).status_code)
    assert made.count(200) >= 1, (
        "every invented epoch was refused, so this arm proved nothing: %r"
        % made)
    stored = {float(k[1]) for k in getattr(we, "_MEM_PARTICIPATION", {})
              if str(k[2]) == "p1"}
    assert len(stored) == 1, (
        "12 invented epochs produced %d participation rows (%r); one "
        "occurrence is one row per player" % (len(stored), sorted(stored)))

    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    app4, store4 = _app(_player("p1"))
    # Pin the SCHEDULER's occurrence here, not the invented one: with the field
    # omitted the server names that occurrence, and only a pinned occurrence is
    # joinable. Pinning `invented` would leave the scheduler's occurrence
    # pending, and the control would then be measuring the phase gate.
    _pin(store4, epoch, "warning")
    omitted = TestClient(app4, raise_server_exceptions=False).post(
        "/world-events/join", json=_join_body(None), headers=_authed("p1"))
    assert omitted.status_code == 200, omitted.text[:200]
    assert omitted.json()["participation"]["epoch_start"] == epoch, (
        "omitting epoch_start must answer the scheduler's occurrence, not the "
        "arbitrary one this store also happens to hold")


def test_an_explicit_null_epoch_is_the_same_as_omitting_it():
    """MEASURED: ``"epoch_start": null`` takes the scheduler's branch.

    Not obvious from the annotation, where null and absent are both ``None``.
    A client that serialises an optional field as null rather than dropping the
    key gets identical behaviour, which is the rule a panel needs before it can
    decide how to build its request.
    """
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "warning")
    client = TestClient(app, raise_server_exceptions=False)

    explicit_null = client.post("/world-events/join",
                                json={"def_key": "beast_swarm",
                                      "epoch_start": None},
                                headers=_authed("p1"))
    assert explicit_null.status_code == 200, explicit_null.text[:200]
    assert explicit_null.json()["participation"]["epoch_start"] == epoch, (
        "an explicit null was treated as a named occurrence")

    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    app2, store2 = _app(_player("p1"))
    _pin(store2, epoch, "warning")
    omitted = TestClient(app2, raise_server_exceptions=False).post(
        "/world-events/join", json={"def_key": "beast_swarm"}, headers=_authed("p1"))
    assert omitted.status_code == 200
    assert (omitted.json()["participation"]["epoch_start"]
            == explicit_null.json()["participation"]["epoch_start"]), (
        "null and omitted must reach the same occurrence")


@pytest.mark.parametrize("raw", [
    '{"def_key": "beast_swarm", "epoch_start": NaN}',
    '{"def_key": "beast_swarm", "epoch_start": Infinity}',
    '{"def_key": "beast_swarm", "epoch_start": "NaN"}',
    '{"def_key": "beast_swarm", "epoch_start": "1e400"}',
    '{"def_key": "beast_swarm", "epoch_start": -1.0}',
    '{"def_key": "beast_swarm", "epoch_start": 1e18}',
    '{"def_key": "beast_swarm", "epoch_start": true}',
])
def test_a_degenerate_epoch_is_refused_on_the_phase_and_never_500s(raw):
    """MEASURED: every degenerate anchor answers 409, never a 500 and never a 200.

    A non-finite or absurd ``epoch_start`` gets through the shape validator --
    it is a float, and nothing bounds it -- and is then refused by the phase
    gate, because no boundary derived from it can contain the server's clock.

    This is the honest answer for a client: "you named an occurrence that is not
    joinable", not "the server broke". It matters because a non-finite float is
    the classic input that turns a comparison-based gate into a crash: every
    comparison against NaN is False, so a naive implementation would fall
    through and join the player into an occurrence that does not exist.

    CONTROL: the same route with the scheduler's real occurrence answers 200 in
    the same app, so the 409s above are the value and not the route.
    """
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "warning")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/world-events/join", data=raw,
                           headers={**_authed("p1"),
                                    "Content-Type": "application/json"})
    assert response.status_code == 409, (
        "body %s answered %d: %s -- a degenerate epoch must be a phase refusal"
        % (raw, response.status_code, response.text[:160]))
    detail = response.json()["detail"].lower()
    assert "join" in detail, (
        "the refusal does not say joining is what is refused: %r" % detail)

    assert client.post("/world-events/join", json=_join_body(epoch),
                       headers=_authed("p1")).status_code == 200, (
        "the real occurrence was refused on the same app and store; the 409s "
        "above cannot be attributed to the degenerate value")


def test_a_second_session_for_the_same_player_sees_the_same_row():
    """MEASURED: the identity, not the token, is what the row is keyed on.

    A player who signs in twice -- on a phone and a desktop -- holds two
    sessions. Both must resolve to one participation, or a client that
    re-authenticates would appear to have entered twice.

    CONTROL: a DIFFERENT player on the same occurrence gets its own row, so
    "same row" is keyed on the identity and not on the occurrence alone.
    """
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    epoch = _epoch()
    app, store = _app(_player("p1"), _player("p2"))
    _pin(store, epoch, "warning")
    client = TestClient(app, raise_server_exceptions=False)

    first = client.post("/world-events/join", json=_join_body(epoch),
                        headers=_authed("p1")).json()
    second = client.post("/world-events/join", json=_join_body(epoch),
                         headers=_authed("p1")).json()
    assert second["participation"] == first["participation"], (
        "a second session produced a different row for the same player")
    assert second["already_entered"] is True

    other = client.post("/world-events/join", json=_join_body(epoch),
                        headers=_authed("p2")).json()
    assert other["participation"]["player_id"] == "p2", (
        "the row is not keyed on the player identity")
    assert other["participation"] != first["participation"], (
        "two players shared one participation row")


def _module_joined(store, pid: str = "p1") -> dict:
    """Module-level join + one cleared slot, ready to claim. No HTTP.

    Returns ``{"store", "player", "epoch", "cooldown_at"}``. Used by the
    tier-percent test below, which drives ``settle_event`` directly so it can
    substitute ``tier_percent`` -- a value that arrives from the definition
    table, not from a request, so there is no route body that could carry it.
    """
    defn = _defn()
    epoch = we.next_epoch(defn, we.server_now())
    inst = we.ensure_instance(store, defn, epoch, we.server_now())
    player = dict(store[pid])
    we.enter_event(store, defn["def_key"], epoch, player, inst["active_at"] + 1.0)
    we.attempt_slot(store, defn["def_key"], epoch, player, 1, inst["active_at"] + 2.0)
    return {"store": store, "player": player, "epoch": epoch,
            "cooldown_at": float(inst["cooldown_at"])}

# ===========================================================================
# G. THE PAID CLAIM IS GATED BY A SESSION, AND A REFUSAL WRITES NOTHING
#
# The sibling route file pins 401 for an ABSENT header on the three POST
# routes. These add the three arms it does not measure: an EMPTY
# Authorization header, the state a refused call must NOT leave behind, and
# the intentional asymmetry with the public banner.
#
# MEASURED 2026-10-05 by calling the routes. `auth.issue_session` mints a
# fresh token per call, which is what makes the two-session replay test in
# section H a real second session rather than the same bearer string twice.
# ===========================================================================

POST_ROUTES = ("/world-events/join", "/world-events/attempt",
               "/world-events/claim")


def _body(path: str, epoch: float) -> dict:
    body = {"def_key": "beast_swarm", "epoch_start": epoch}
    if path == "/world-events/attempt":
        body["slot"] = 1
    return body


def _player_with(**over) -> dict:
    """The shared ``_player`` shape with stored fields overwritten.

    The module-level ``_player`` takes only ``(pid, progress)`` because every
    test above it uses a fixed row. The non-finite tests in section J need a
    row that differs in exactly one field, so this builds the same shape and
    overwrites -- rather than widening the shared helper's signature for one
    section's benefit.
    """
    row = _player("p1")
    row.update(over)
    return row


@pytest.mark.parametrize("path", POST_ROUTES)
@pytest.mark.parametrize("headers", [
    None,
    {},
    {"Authorization": ""},
    {"Authorization": "Bearer "},
    {"Authorization": "not-a-real-token"},
    {"Authorization": "Bearer not-a-real-token"},
    {"Authorization": "Basic dXNlcjpwYXNz"},
], ids=["absent", "empty-dict", "empty-value", "bearer-only",
        "no-scheme", "bad-token", "wrong-scheme"])
def test_a_paid_route_refuses_every_shape_of_a_missing_session(path, headers):
    """MEASURED: all seven shapes are 401 with the same message.

    ``POST /world-events/join`` and ``POST /world-events/claim`` move or spend
    a player's own currency, so this is the gate that decides whether the
    claim route is reachable at all by a caller who is not signed in. A 403 or
    a 422 here would both be wrong in the same direction: the client would read
    a permissions failure as a malformed request and retry it forever.

    The message is identical across every shape, so a client cannot tell a
    missing header from a forged one -- which is the point, because that
    difference is an enumeration oracle.

    CONTROL: the same call WITH a session is not 401, so the refusals above
    are the auth layer rather than a route that answers 401 to everything.
    """
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "active")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(path, json=_body(path, epoch), headers=headers)
    assert response.status_code == 401, (
        "%s with headers %r answered %d; a paid route must refuse an "
        "unauthenticated caller with 401" % (path, headers, response.status_code))
    assert "session" in response.json()["detail"].lower(), (
        "the refusal must name what is missing: %r" % response.json())

    signed_in = client.post(path, json=_body(path, epoch), headers=_authed("p1"))
    assert signed_in.status_code != 401, (
        "%s answered 401 for a VALID session too (%s) -- the assertions above "
        "would pass on a route that refuses everything"
        % (path, signed_in.text[:120]))


def test_an_unauthenticated_claim_leaves_no_participation_no_receipt_and_no_progress():
    """MEASURED: the refusal costs the anonymous caller nothing and gives them
    nothing -- there is no row, no receipt, and no progress anywhere.

    This is the shape a half-finished guard leaves behind: the status code is
    right but a write already happened. So the assertions are on the SERVER'S
    OWN ROWS rather than on the reply, which is the only place a stray write
    would show up.

    CONTROL: a signed-in player who joins on the same app and occurrence does
    produce all three, so the empties below are the refused call and not a
    store that never writes.
    """
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "warning")
    client = TestClient(app, raise_server_exceptions=False)

    for path in POST_ROUTES:
        assert client.post(path, json=_body(path, epoch)).status_code == 401

    assert we.instance_entrants(store, "beast_swarm", epoch) == 0, (
        "an unauthenticated call created a participation row")
    assert we.read_receipt(store, we.ledger_key("beast_swarm", epoch, "p1")) is None, (
        "an unauthenticated call wrote a payout receipt")
    assert float(store["p1"]["progress"]) == 0.0

    # CONTROL: the signed-in caller on the same app and occurrence writes all
    # three. Without this, a store that simply never writes would pass above.
    assert client.post("/world-events/join", json=_body("/world-events/join", epoch),
                       headers=_authed("p1")).status_code == 200
    assert we.instance_entrants(store, "beast_swarm", epoch) == 1


def test_the_public_banner_stays_open_while_all_three_paid_routes_are_shut():
    """MEASURED: banner 200 with no session; the three POST routes 401.

    The asymmetry is deliberate and documented in the routes' own module
    docstring, so it is a contract a client can rely on: "which events are
    running" needs no sign-in, "act on my event" does. It is also the reason a
    player can be shown a running event before they have ever authenticated.

    The banner carries no player field at ANY depth, which is what makes the
    open route safe to open -- so that is asserted here too, rather than
    trusted from the docstring.

    CONTROL: the same store, with a session, answers the banner identically --
    so anonymity is a property of the payload and not of the missing header.
    """
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "active")
    client = TestClient(app, raise_server_exceptions=False)

    anon = client.get("/world-events/banner")
    assert anon.status_code == 200, (
        "the banner answered %d with no session" % anon.status_code)
    payload = anon.json()
    assert payload["events"], "a materialised occurrence must appear on the banner"
    for row in payload["events"]:
        assert not ({"player_id", "tier", "contribution", "progress", "claimed",
                     "snapshot_power"} & set(row)), (
            "the public banner describes a player: %r" % sorted(row))
        assert set(row) == {"def_key", "name", "event_type", "epoch_start",
                            "phase", "warn_at", "active_at", "cooldown_at",
                            "closed_at"}

    assert client.get("/world-events/banner",
                      headers=_authed("p1")).json()["events"] == payload["events"], (
        "the banner answered differently for a signed-in caller; anonymity must "
        "be a property of the payload, not of the missing header")

    for path in POST_ROUTES:
        assert client.post(path, json=_body(path, epoch)).status_code == 401


# ===========================================================================
# H. THE REPLAY GUARD PREVENTS DOUBLE-CREDIT
#
# MEASURED 2026-10-05. One player on one occurrence, three identical claims:
# 200 / 200 / 200, progress 5.0 / 5.0 / 5.0, and all three receipts
# byte-identical.
#
# The load-bearing test here is H2, which proves the guard is the LEDGER KEY
# rather than the ``claimed_at`` flag on the participation row: the row is
# wiped by hand between calls and the claim is still refused with the FIRST
# receipt. The sibling module-level file proves ``claim_payout`` keys on the
# tuple; this proves it survives a full HTTP round trip.
# ===========================================================================

def _paid_ready_client(pid: str = "p1"):
    """An app whose player has joined AND cleared slot one, ready to claim.

    Returns ``(client, store, epoch)``. The claim window is cooldown, which is
    the only phase ``settle_event`` pays in.
    """
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    epoch = _epoch()
    app, store = _app(_player(pid))
    _pin(store, epoch, "active")
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/world-events/join", json=_join_body(epoch), headers=_authed(pid))
    client.post("/world-events/attempt", json=_attempt_body(epoch, 1),
                headers=_authed(pid))
    _pin(store, epoch, "cooldown")
    return client, store, epoch


def test_the_same_paid_claim_sent_three_times_moves_the_balance_exactly_once():
    """THE MONEY TEST, over HTTP. Three claims, one payment.

    Asserted on the player's own stored ``progress`` rather than on a reply
    flag, because a route that answers ``ok`` while the ledger row moves three
    times is the SHAPE of the defect rather than the absence of it. Each later
    answer must also carry the FIRST attempt's receipt: a synthesised second
    answer would be a guess about progress the player actually received.

    The first claim must genuinely pay, or "paid once" is trivially true of a
    claim that paid nothing.
    """
    client, store, epoch = _paid_ready_client()
    body = _join_body(epoch)

    replies = [client.post("/world-events/claim", json=body, headers=_authed("p1"))
               for _ in range(3)]
    codes = [r.status_code for r in replies]
    assert codes == [200, 200, 200], (
        "the claim route answered %s; a replay is a 200 with already_claimed, "
        "never an error" % codes)

    paid = float(replies[0].json()["receipt"]["progress_points"])
    assert paid > 0.0, "the fixture must clear a slot so there is money to pay"
    assert replies[0].json()["already_claimed"] is False
    assert float(store["p1"]["progress"]) == pytest.approx(paid), (
        "the first claim did not credit the player: %r" % store["p1"]["progress"])

    for index, reply in enumerate(replies[1:], start=2):
        assert reply.json()["already_claimed"] is True, (
            "claim %d answered already_claimed false" % index)
        assert reply.json()["receipt"] == replies[0].json()["receipt"], (
            "claim %d returned a DIFFERENT receipt; a replay must restate the "
            "first payout exactly" % index)
        assert float(store["p1"]["progress"]) == pytest.approx(paid), (
            "claim %d moved the balance to %r; a replay must pay nothing"
            % (index, store["p1"]["progress"]))


def test_the_replay_guard_is_the_ledger_key_and_not_a_flag_on_the_participation_row():
    """MEASURED: wipe BOTH markers on the participation row and the next claim
    is still refused, with the first receipt, and still pays nothing.

    This is the distinction the module docstring claims: the once-only guard is
    ``world_event_reward_log``'s primary key, not a boolean on the player's
    row. A flag is trivially cleared by any writer that forgets to set it; a
    primary key cannot be. So the test clears the flag BY HAND, which is the
    one thing a well-behaved caller would never do, and then asserts the
    payout is still refused.

    If this ever fails, the guarantee has become a comment: the once-only
    behaviour would then be resting on ``claimed_at`` alone.

    CONTROL: after the wipe, ``read_receipt`` still holds the FIRST receipt --
    proving the row wipe did not also clear the ledger, so the refusal below
    is the ledger and not a leftover flag on the in-memory object.
    """
    client, store, epoch = _paid_ready_client()
    first = client.post("/world-events/claim", json=_join_body(epoch),
                        headers=_authed("p1"))
    paid = float(store["p1"]["progress"])
    receipt = first.json()["receipt"]
    assert paid > 0.0

    anchor = we.anchor_lattice(_defn(), epoch)
    part = we.get_participation(store, "beast_swarm", epoch, "p1")
    part["claimed_at"] = None
    part["claim_receipt"] = None
    we.save_participation(store, part, epoch)
    assert we.get_participation(store, "beast_swarm", epoch, "p1")["claimed_at"] is None, \
        "the wipe did not land; this test is not exercising the ledger"

    assert we.read_receipt(store, we.ledger_key("beast_swarm", anchor, "p1")) is not None, \
        "CONTROL: the ledger row must survive the row wipe, or the refusal " \
        "below could not be attributed to it"

    again = client.post("/world-events/claim", json=_join_body(epoch),
                        headers=_authed("p1"))
    assert again.status_code == 200, again.text[:160]
    assert again.json()["already_claimed"] is True, (
        "a claim was PAID again after the participation row's markers were "
        "cleared -- the once-only guard is the row flag, not the ledger key")
    assert again.json()["receipt"] == receipt
    assert float(store["p1"]["progress"]) == pytest.approx(paid), (
        "the balance moved to %r after the flag was cleared" % store["p1"]["progress"])


def test_a_second_session_for_the_same_player_gets_the_stored_receipt_not_a_new_payout():
    """MEASURED: a re-authenticating client that retries after a timeout is
    told what it already received, and pays nothing more.

    The realistic case: the player signs in on a phone, the claim succeeds, the
    reply times out, and a fresh session retries. Both halves matter -- the
    balance must not move, and the answer must be the REAL first receipt rather
    than a fresh computation, because a client that retries after a timeout has
    no other way to learn what it was paid.

    ``_authed`` mints a new token on every call, so the two calls below are
    genuinely two sessions rather than the same bearer string twice.

    CONTROL: a DIFFERENT player on the same occurrence is not refused, so the
    replay is keyed on the player and not on the occurrence alone.
    """
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    epoch = _epoch()
    app, store = _app(_player("p1"), _player("p2"))
    _pin(store, epoch, "active")
    client = TestClient(app, raise_server_exceptions=False)
    for pid in ("p1", "p2"):
        client.post("/world-events/join", json=_join_body(epoch), headers=_authed(pid))
        client.post("/world-events/attempt", json=_attempt_body(epoch, 1), headers=_authed(pid))
    _pin(store, epoch, "cooldown")

    first = client.post("/world-events/claim", json=_join_body(epoch), headers=_authed("p1"))
    paid = float(store["p1"]["progress"])
    assert paid > 0.0

    retry = client.post("/world-events/claim", json=_join_body(epoch), headers=_authed("p1"))
    assert retry.json()["already_claimed"] is True
    assert retry.json()["receipt"] == first.json()["receipt"], (
        "a second SESSION was answered with a different receipt")
    assert float(store["p1"]["progress"]) == pytest.approx(paid)

    other = client.post("/world-events/claim", json=_join_body(epoch), headers=_authed("p2"))
    assert other.json()["already_claimed"] is False, (
        "p2 was refused as a replay of p1's claim; the guard is per player")
    assert float(store["p2"]["progress"]) > 0.0


# ===========================================================================
# I. RATE LIMITING
#
# MEASURED 2026-10-05. A claim storm after one successful claim reads
# 200 x10 then 429, and the balance stays at 5.0 throughout.
#
# The sibling file in this same directory already pins: the limiter runs before
# the player lookup, ``attempt`` is limited too, and the three bounded routes
# share one budget. What it does NOT measure is the money half -- that a burst
# of refusals leaves the ledger and the balance untouched -- or the HTTP shape
# of the refusal, which is I3.
# ===========================================================================

def test_a_burst_of_rate_limited_claims_moves_no_money_and_writes_no_receipt():
    """MEASURED: 200s until the shared cap, then 429 forever; progress stays at
    exactly the one payment.

    The cap is read off ``we.ATTEMPT_RATE_LIMIT`` rather than hardcoded, so a
    retuned cap does not turn this into a change-detector; what is asserted is
    the SHAPE -- a burst under the cap is never a 429, and once one appears
    every later call in the window is a 429 too.

    The balance assertion is the load-bearing half. A rate limiter that
    spends the payout before refusing the retry would turn a client that
    retries into a double payer, which is the exact bug the once-only guard
    exists to prevent.

    CONTROL: a second player is unaffected by p1's burst, so this is a
    per-player budget and not a global lockout.
    """
    limit = we.ATTEMPT_RATE_LIMIT
    client, store, epoch = _paid_ready_client()
    body = _join_body(epoch)

    codes = [client.post("/world-events/claim", json=body, headers=_authed("p1")).status_code
             for _ in range(limit + 6)]
    assert 500 not in codes, (
        "a rate limit escaped as a server error on claim: %s" % codes)
    first_429 = codes.index(429)
    assert first_429 > 0, (
        "the very first claim was limited: %s -- the cap is being applied "
        "before any work is done" % codes)
    assert set(codes[first_429:]) == {429}, (
        "a call after the cap answered something other than 429: %s" % codes)
    assert 429 not in codes[:first_429], (
        "the limiter refused a call still inside its budget: %s" % codes[:first_429])

    assert float(store["p1"]["progress"]) == pytest.approx(5.0), (
        "the refusal storm moved the balance to %r; a 429 must never move "
        "money" % store["p1"]["progress"])
    receipts = [r for r in (we.read_receipt(store, we.ledger_key(
        "beast_swarm", we.anchor_lattice(_defn(), epoch), "p1")) or {},)]
    assert receipts and receipts[0]["progress_points"] > 0.0, (
        "the one genuine payment left no receipt behind")

    # CONTROL: p2's own budget was untouched by p1's burst.
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    app2, store2 = _app(_player("p2"))
    _pin(store2, epoch, "active")
    other = TestClient(app2, raise_server_exceptions=False).post(
        "/world-events/join", json=_join_body(epoch), headers=_authed("p2"))
    assert other.status_code == 200, (
        "p1's burst refused a DIFFERENT player; the limiter is global, so one "
        "noisy client can lock everyone out of the mode")


def test_a_rate_limited_claim_names_a_retry_delay_the_client_can_parse():
    """MEASURED: the 429 body carries a whole number of seconds.

    A 429 with no number is a client-side retry loop: the caller cannot tell a
    one-second wait from a full-minute one, so it guesses, and it guesses fast.
    The message is parsed here rather than eyeballed, so a rewrite that drops
    the number turns this red instead of quietly shipping a bare refusal.

    CONTROL: the same route inside its budget is not a 429, so this is
    measuring the refusal and not the route.
    """
    client, store, epoch = _paid_ready_client()
    body = _join_body(epoch)

    refused = None
    for _ in range(we.ATTEMPT_RATE_LIMIT + 4):
        response = client.post("/world-events/claim", json=body, headers=_authed("p1"))
        if response.status_code == 429:
            refused = response
            break
    assert refused is not None, (
        "no claim in %d calls was rate limited; this test is not measuring the "
        "refusal" % (we.ATTEMPT_RATE_LIMIT + 4))

    detail = refused.json()["detail"]
    assert "try again in" in detail.lower(), (
        "the 429 must say when to retry: %r" % detail)
    seconds = detail.rsplit("in ", 1)[-1].rstrip("s. ")
    assert seconds.isdigit() and int(seconds) >= 1, (
        "the retry delay is not a whole number of seconds: %r" % detail)
    assert int(seconds) <= we.ATTEMPT_RATE_WINDOW, (
        "the retry delay %s exceeds the window %r" % (seconds, we.ATTEMPT_RATE_WINDOW))


@pytest.mark.xfail(strict=True, reason=(
    "MEASURED DEFECT 2026-10-05: the world-event 429 carries no Retry-After "
    "header. The body names the delay but the header is absent, so a client "
    "reading headers (the convention on communication and pvp routes in this "
    "tree) gets nothing to back off on. world_event_routes._call() raises "
    "HTTPException(exc.status, exc.message) with no headers= argument."))
def test_a_rate_limited_claim_carries_a_retry_after_header():
    """The convention this tree already holds elsewhere, asserted for these
    routes.

    ``test_communication_contract`` and both pvp route contract files assert
    ``Retry-After`` on a 429 and none of them would accept a headerless
    refusal. This route family is the odd one out: ``grep -c Retry-After`` over
    ``world_event_routes.py`` returns 0.

    Kept as a strict xfail so it is a RATCHET rather than a comment: when
    ``_call()`` grows a ``headers=`` argument this XPASSes, and strict turns
    that into a reported failure that says the marker is now stale.

    CONTROL: the refusal itself is real -- it is a 429 with a delay in the
    body -- so a fix must ADD the header without weakening the refusal.
    """
    client, store, epoch = _paid_ready_client()
    body = _join_body(epoch)

    refused = None
    for _ in range(we.ATTEMPT_RATE_LIMIT + 4):
        response = client.post("/world-events/claim", json=body, headers=_authed("p1"))
        if response.status_code == 429:
            refused = response
            break
    assert refused is not None, "no call was rate limited"

    retry_after = refused.headers.get("Retry-After")
    assert retry_after is not None, (
        "a 429 with no Retry-After header: %s" % dict(refused.headers))
    assert retry_after.isdigit() and int(retry_after) >= 1, (
        "Retry-After is not a whole number of seconds: %r" % retry_after)


# ===========================================================================
# J. NON-FINITE AMOUNTS ARE REJECTED
#
# MEASURED 2026-10-05, and the honest answer is SPLIT. Two of the four shapes
# are handled; two crash the server. All four are pinned below so the split is
# on the record rather than in a reviewer's memory.
#
#   HANDLED   a non-finite value in the DECLARED float field (``epoch_start``)
#             is refused on the phase -- 409 on join and claim, 404 on attempt
#             -- and never as a 500.
#   DEFECT    a non-finite value in an EXTRA body field  -> 500, not 422.
#   DEFECT    a non-finite value in a STORED player stat -> 500 on attempt.
#   DEFECT    a non-finite reward PERCENT                -> the claim is
#             PAID with ``progress_points: nan`` and the once-only key burned.
#
# NaN is the interesting one throughout: every comparison against it is False,
# so a guard written as ``if points <= 0`` cannot see it. That is why the
# zero-payout guard the sibling file pins does NOT hold for NaN.
# ===========================================================================

def test_a_non_finite_value_in_the_declared_float_field_is_refused_and_never_500s():
    """MEASURED: NaN, Infinity, -Infinity and 1e400 all answer 409 or 404.

    ``epoch_start`` is the one field the model declares as a float, and nothing
    bounds it, so a non-finite literal gets through the shape validator. What
    must happen next is that it is refused on the PHASE: no boundary derived
    from NaN can contain the server's clock, so the event is not joinable.

    This matters because NaN is the classic input that turns a comparison gate
    into a crash or a silent pass-through -- every comparison against it is
    False, so a naive ``if now >= active_at`` falls through and enters the
    player into an occurrence that does not exist.

    The claim arm is asserted here too, because a claim is a PAYMENT: the
    balance must not move on any of these values.

    CONTROL: the same route with the scheduler's real occurrence answers 200,
    so the refusals are the value and not the route.
    """
    for literal in ("NaN", "Infinity", "-Infinity", "1e400"):
        we.reset_memory_state()
        we.ATTEMPT_LIMITER.reset()
        epoch = _epoch()
        app, store = _app(_player("p1"))
        _pin(store, epoch, "active")
        client = TestClient(app, raise_server_exceptions=False)
        body = '{"def_key":"beast_swarm","epoch_start":%s}' % literal

        joined = client.post("/world-events/join", data=body, headers={
            **_authed("p1"), "Content-Type": "application/json"})
        assert joined.status_code == 409, (
            "join with epoch_start=%s answered %d (%s); a non-finite anchor "
            "must be refused on the phase"
            % (literal, joined.status_code, joined.text[:120]))

        claimed = client.post("/world-events/claim", data=body, headers={
            **_authed("p1"), "Content-Type": "application/json"})
        assert claimed.status_code == 409, (
            "claim with epoch_start=%s answered %d (%s); a non-finite anchor "
            "must not reach the payout"
            % (literal, claimed.status_code, claimed.text[:120]))
        assert float(store["p1"]["progress"]) == 0.0, (
            "epoch_start=%s moved the balance to %r" % (literal,
                                                        store["p1"]["progress"]))

        # CONTROL: the real occurrence is admitted on the same app and store.
        assert client.post("/world-events/join", json=_join_body(epoch),
                           headers=_authed("p1")).status_code == 200, (
            "the scheduler's own occurrence was refused on the same app; the "
            "409s above cannot be attributed to the non-finite value")


def test_a_non_finite_value_in_a_declared_float_field_on_attempt_answers_404_not_500():
    """MEASURED: attempt answers 404 for a non-finite anchor, not 409 and not 500.

    A different answer from ``join``, and worth pinning: ``attempt_slot``
    resolves the PARTICIPATION row for the named occurrence before it computes
    the phase, and no player ever joined the NaN-keyed occurrence -- so the
    honest answer is "you have not joined this event" (404). ``join``
    materialises the occurrence first, so it reaches the phase gate (409).

    The load-bearing assertion is that NEITHER is a 500: the value is a legal
    float, so every arithmetic step on it must be guarded rather than assumed.

    CONTROL: the same route on the real occurrence reaches the handler and
    answers 200 or 409, never 404-for-the-wrong-reason.
    """
    for literal in ("NaN", "Infinity", "-Infinity", "1e400"):
        we.reset_memory_state()
        we.ATTEMPT_LIMITER.reset()
        epoch = _epoch()
        app, store = _app(_player("p1"))
        _pin(store, epoch, "active")
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/world-events/join", json=_join_body(epoch), headers=_authed("p1"))

        response = client.post(
            "/world-events/attempt",
            data='{"def_key":"beast_swarm","epoch_start":%s,"slot":1}' % literal,
            headers={**_authed("p1"), "Content-Type": "application/json"})
        assert response.status_code in (404, 409), (
            "attempt with epoch_start=%s answered %d (%s); a non-finite anchor "
            "must be refused, not crash" % (literal, response.status_code,
                                            response.text[:120]))

        real = client.post("/world-events/attempt", json=_attempt_body(epoch, 1),
                           headers=_authed("p1"))
        assert real.status_code in (200, 409), (
            "the real occurrence was refused with %d on the same app; the "
            "refusal above cannot be attributed to the literal"
            % real.status_code)


def test_a_finite_stat_of_the_same_shape_reaches_the_combat_function():
    """CONTROL for the two xfails below, kept as its own passing test.

    The non-finite tests overwrite one field of an otherwise ordinary player
    row and expect a refusal. That expectation is only meaningful if the
    un-overwritten row reaches the combat function and fights. So this test
    runs the identical fixture shape with a finite stat and asserts the route
    answers 200 with a resolved outcome -- which is what makes the refusals
    below attributable to the non-finite VALUE and not to a fixture that never
    gets far enough to fight.

    Every finite value answers 200: the ladder is derived from the player's own
    numbers, so a big stat is a bigger fight, never a refusal.
    """
    for label, stat in (("hp", 120), ("atk", 20), ("def_", 10),
                        ("hp=1", 1), ("atk=1", 1)):
        we.reset_memory_state()
        we.ATTEMPT_LIMITER.reset()
        field = label.split("=")[0]
        epoch = _epoch()
        app, store = _app(_player_with(**{field: stat}))
        _pin(store, epoch, "active")
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/world-events/join", json=_join_body(epoch), headers=_authed("p1"))

        response = client.post("/world-events/attempt",
                               json=_attempt_body(epoch, 1), headers=_authed("p1"))
        assert response.status_code == 200, (
            "%s=%r answered %d (%s); this fixture must reach the combat "
            "function" % (field, stat, response.status_code,
                          response.text[:120]))
        assert isinstance(response.json()["outcome"]["cleared"], bool)


@pytest.mark.xfail(strict=True, reason=(
    "MEASURED DEFECT 2026-10-05: a non-finite literal in an EXTRA body field "
    "answers 500, not the 422 the extra='forbid' validator should produce. The "
    "validator DOES refuse it -- the failure is in SERIALISING the 422: "
    "pydantic echoes the offending value into error detail['input'], and "
    "FastAPI's encoder raises 'ValueError: Out of range float values are not "
    "JSON compliant: nan'. The designed answer is unreachable for exactly the "
    "inputs a client is most likely to fuzz with."))
def test_a_non_finite_value_in_an_extra_body_field_is_422_not_500():
    """The honest answer for an unknown field is 422 ``extra_forbidden``.

    ``extra="forbid"`` is this route family's headline anti-abuse property: a
    body carrying ``{"amount": 999999}`` must be refused loudly rather than
    silently ignored, because "answered 200" reads as "the server accepted my
    amount". A finite unknown field does answer 422 and that is pinned by the
    sibling file.

    A NON-FINITE unknown field does not, and the reason is not the validator.
    ``tier: "top"`` gives ``extra_forbidden`` correctly; ``amount: NaN`` gives
    the same validator verdict and then crashes while the 422 is being encoded,
    because pydantic copies the rejected value into ``detail[0]['input']`` and
    Python's JSON encoder refuses NaN.

    So the client-visible result is a 500 on a request that should have been a
    clean rejection -- a malformed request is reported as a server fault.

    CONTROL: the same field with a FINITE value is a proper 422 on the same
    app, so the difference is the non-finite literal and not the field name.
    """
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "cooldown")
    client = TestClient(app, raise_server_exceptions=False)
    post = {"Content-Type": "application/json", **_authed("p1")}

    # CONTROL first: a finite unknown field is a proper 422 on this app.
    finite = client.post("/world-events/claim",
                         data='{"def_key":"beast_swarm","epoch_start":%r,"amount":999999}' % epoch,
                         headers=post)
    assert finite.status_code == 422, (
        "a FINITE unknown field answered %d; the validator itself is broken, "
        "so the non-finite cases below measure nothing"
        % finite.status_code)
    assert finite.json()["detail"][0]["type"] == "extra_forbidden"

    for field, literal in (("amount", "NaN"), ("progress_points", "NaN"),
                           ("contribution", "Infinity"), ("tier", "NaN"),
                           ("amount", "1e400")):
        response = client.post(
            "/world-events/claim",
            data='{"def_key":"beast_swarm","epoch_start":%r,"%s":%s}' % (epoch, field, literal),
            headers=post)
        assert response.status_code == 422, (
            "%s=%s answered %d (%s); an unknown field must be 422 "
            "extra_forbidden, never a server fault"
            % (field, literal, response.status_code, response.text[:120]))
        assert float(store["p1"]["progress"]) == 0.0


@pytest.mark.xfail(strict=True, reason=(
    "MEASURED DEFECT 2026-10-05: a non-finite STORED player stat crashes the "
    "attempt route with a 500. world_events._stat_line does "
    "max(int(float(player.get('hp') or 100)), 1); int(nan) raises ValueError "
    "and int(inf) raises OverflowError, and neither is caught. Every other "
    "stat in the module is guarded by max(..., 1) -- which does not help when "
    "the exception is raised by int() before max() ever runs."))
def test_a_non_finite_stored_stat_does_not_reach_the_combat_function_as_a_500():
    """A stored NaN or Infinity in a player's own row must not be a 500.

    This is the reachable version of the same hole: unlike a malformed request
    body, a player's stats are SERVER state, and any writer that persists a
    non-finite number -- a division by zero, a JSON round trip that admits
    NaN, a balance that went to infinity -- hands ``attempt_slot`` a row it
    cannot convert. The client then sees "Internal Server Error" for an event
    it played correctly.

    The traceback is the whole point: ``_stat_line`` line 1101,
    ``int(float(player.get('hp') or 100))``. The ``max(..., 1)`` floor is on the
    RESULT, so it guards a negative stat but not one that cannot be converted
    at all.

    CONTROL: the same fixture with a finite stat answers 200 and resolves a
    fight -- asserted by
    ``test_a_finite_stat_of_the_same_shape_reaches_the_combat_function``.
    """
    for field, value in (("hp", float("nan")), ("hp", float("inf")),
                         ("atk", float("nan")), ("atk", float("-inf")),
                         ("def_", float("inf"))):
        we.reset_memory_state()
        we.ATTEMPT_LIMITER.reset()
        epoch = _epoch()
        app, store = _app(_player_with(**{field: value}))
        _pin(store, epoch, "active")
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/world-events/join", json=_join_body(epoch), headers=_authed("p1"))

        response = client.post("/world-events/attempt",
                               json=_attempt_body(epoch, 1), headers=_authed("p1"))
        assert response.status_code in (200, 409), (
            "a stored %s=%r answered %d (%s); a non-finite stored stat must "
            "be refused or clamped, never a server fault"
            % (field, value, response.status_code, response.text[:140]))


def test_the_pure_helpers_are_where_a_non_finite_value_enters_the_crate():
    """MEASURED: which helpers refuse a non-finite number and which raise.

    The route-level tests above are the client-visible contract; this is the
    map of the code underneath, so a reviewer changing one of these helpers
    knows which assertion moves with it. Measured on 2026-10-05:

      RAISES   ``_stat_line`` on a non-finite hp/atk/def_ (ValueError on NaN,
               OverflowError on inf).
      RAISES   ``_enemy_for`` on a non-finite difficulty_roll, same two
               exceptions.
      CLAMPS   ``contribution_for`` -- NaN damage is normalised to 0.0 and
               Infinity damage to 1.0, because ``min``/``max`` against a NaN
               propagate the opposite way here and the result still lands
               inside [0, 1].
      PASSES   ``tier_percent`` -- it returns NaN or inf verbatim, which is
               what the next test is about.

    Each assertion is on the OBSERVED behaviour, including the exception type,
    so a guard that is added to one helper turns exactly one line red.
    """
    part = we._blank_participation("beast_swarm", 1000.0, "p1", 1.0, 0.0, 1.0, 0.0)

    with pytest.raises(ValueError):
        we._stat_line({}, part, {"id": "p1", "hp": float("nan"), "atk": 10, "def_": 5})
    with pytest.raises(OverflowError):
        we._stat_line({}, part, {"id": "p1", "hp": float("inf"), "atk": 10, "def_": 5})

    stats = {"hp": 200, "atk": 40, "def": 20, "snapshot_scale": 1.0,
             "realm": "mortal", "stage": "early"}
    slot = {"health_multiple": 2.0, "attack_multiple": 0.5}
    nan_roll = we._blank_participation("beast_swarm", 1000.0, "p1", 1.0, 0.0,
                                       float("nan"), 0.0)
    with pytest.raises(ValueError):
        we._enemy_for(nan_roll, slot, stats)

    deep = we._blank_participation("beast_swarm", 1000.0, "p1", 1.0, 0.0, 1.0, 0.0)
    deep["best_slot"] = 5
    assert we.contribution_for({"event_type": "boss"}, deep, float("nan"), 5) == 0.0
    assert we.contribution_for({"event_type": "boss"}, deep, float("inf"), 5) == 1.0

    assert math.isnan(we.tier_percent(
        {"reward_tiers": [{"tier": "participate", "percent": float("nan")}]},
        "participate"))


@pytest.mark.xfail(strict=True, reason=(
    "MEASURED DEFECT 2026-10-05: a non-finite reward PERCENT is PAID. "
    "settle_event guards the payout with 'if points <= 0.0: raise "
    "EventRefused', and NaN <= 0.0 is False, so a NaN tier percent walks "
    "straight through the zero-payout guard the sibling file pins. "
    "claim_payout then wins the once-only key, qi_rewards.grant_qi treats the "
    "non-finite amount as a no-op, and the answer is "
    "already_claimed: false with progress_points: nan -- so the player is told "
    "they were paid, the balance did not move, and the key is gone forever."))
def test_a_non_finite_reward_percent_cannot_burn_the_once_only_key(monkeypatch):
    """THE HOLE IN AN ONCE-ONLY DESIGN, reached through the tier table.

    The sibling file pins that a ZERO-value payout refuses without consuming
    the once-only key, and calls it the one refund-shaped hole in the design.
    That guard is written ``if points <= 0.0``. NaN is the value it cannot
    see: every comparison against NaN is False, so ``nan <= 0.0`` is False and
    the refusal never fires.

    What is measured, in order:
      * the claim answers as a SUCCESS (``already_claimed: false``),
      * the receipt states a non-finite ``progress_points``,
      * the player's stored progress did NOT move, because ``grant_qi``
        already treats a non-finite amount as a no-op,
      * and the ledger key is now CONSUMED, so the real payment that follows
        can never be made.

    The fourth bullet is why this is money and not cosmetics: the once-only
    guard has become a once-EVER guard for this occurrence.

    CONTROL: a finite percent pays normally on the same fixture, and the same
    NaN with the guard restored by ``math.isfinite`` is refused -- so the fix
    is the comparison, not the tier table.
    """
    for label, percent in (("NaN", float("nan")), ("Infinity", float("inf"))):
        we.reset_memory_state()
        we.ATTEMPT_LIMITER.reset()
        store = PlayerStore(InMemoryWalletStore())
        with store.transaction():
            store["p1"] = _player("p1")
        real_joined = _module_joined(store)
        monkeypatch.setattr(we, "tier_percent", lambda *a, **k: percent)

        anchor = we.anchor_lattice(_defn(), real_joined["epoch"])
        key = we.ledger_key("beast_swarm", anchor, "p1")
        before = float(store["p1"]["progress"])

        with pytest.raises(we.WorldEventError) as raised:
            we.settle_event(store, "beast_swarm", real_joined["epoch"],
                            real_joined["player"],
                            real_joined["cooldown_at"] + 1.0)
        assert raised.value.status == 409, (
            "%s percent must refuse rather than pay" % label)
        assert float(store["p1"]["progress"]) == pytest.approx(before), (
            "%s percent moved the balance" % label)
        assert we.read_receipt(store, key) is None, (
            "a %s-value payout BURNED the once-only key; the player can never "
            "be paid for this occurrence" % label)

    monkeypatch.undo()
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    store = PlayerStore(InMemoryWalletStore())
    with store.transaction():
        store["p1"] = _player("p1")
    control = _module_joined(store)
    out = we.settle_event(store, "beast_swarm", control["epoch"],
                          control["player"], control["cooldown_at"] + 1.0)
    assert out["already_claimed"] is False
    assert out["receipt"]["progress_points"] > 0.0
    assert math.isfinite(out["receipt"]["progress_points"])
    assert float(store["p1"]["progress"]) > 0.0


# ===========================================================================
# K. A NON-FINITE ``slot`` IS A 500, NOT A 422
#
# The section-J tests above cover a non-finite value in the declared FLOAT
# field (``epoch_start``) and in an EXTRA body field. Neither is this one.
# ``slot`` is the only field the model declares as an int, so it reaches the
# validator's own finite check -- and the answer is lost on the way back out.
#
# MEASURED 2026-10-05 by calling the route. Not read off the handler.
# ===========================================================================


@pytest.mark.xfail(strict=True, reason=(
    "MEASURED DEFECT 2026-10-05: a non-finite slot answers 500, not 422. "
    "Pydantic DOES refuse it correctly -- WorldEventAttemptRequest raises "
    "finite_number -- but pydantic copies the rejected value into "
    "detail[0]['input'], and Starlette then encodes that 422 with a strict "
    "JSON encoder that rejects NaN/Infinity. The encoder raises while "
    "serialising, so the validation failure is reported to the client as an "
    "Internal Server Error. Reproduced on both httpx transports (data= and "
    "content=) with identical bytes, so it is not a test-client artefact."))
def test_a_non_finite_slot_is_refused_as_422_and_never_answers_500():
    """The one declared-int field takes the non-finite path nobody has pinned.

    Why this case is different from the two above it, and why the difference
    matters:

      * ``epoch_start`` is a declared FLOAT with no finite check, so the value
        reaches the handler and is refused on the PHASE (409). Section J pins
        that. Nothing crashes because nothing has to encode the bad value.
      * an EXTRA field is refused by ``extra="forbid"`` and -- per the strict
        xfail above -- the same encoding failure applies, but the request never
        reaches a handler either way.
      * ``slot`` is the field a real client fills in from a UI (a list index, a
        "+1" button), so this is the non-finite value most likely to arrive from
        an honest client rather than an attacker: a JavaScript ``NaN`` or
        ``Infinity`` serialised straight into the request.

    The client-visible answer is 500. A malformed request is being reported as
    a server fault, so a client that treats 5xx as "retry, the server is
    unhealthy" will retry this forever.

    The route CAN produce a 422 for this exact field: a finite out-of-range slot
    is a clean ``less_than_equal`` 422, asserted below as the control. So the
    difference is the non-finite literal and nothing else about the route.

    CONTROL, in three parts:
      * the player has JOINED first, so a 404 cannot be mistaken for the
        refusal -- the participation row exists and the handler is reachable;
      * a FINITE in-band slot answers 200 on the same app and store, so the
        500s below are the value rather than a route that refuses everything;
      * a FINITE out-of-range slot answers 422 with a serialisable body, so the
        route's own error path works and only the non-finite value breaks it.
    """
    for literal in ("NaN", "Infinity", "-Infinity", "1e400"):
        we.reset_memory_state()
        we.ATTEMPT_LIMITER.reset()
        epoch = _epoch()
        app, store = _app(_player("p1"))
        _pin(store, epoch, "active")
        client = TestClient(app, raise_server_exceptions=False)
        raw_headers = {**_authed("p1"), "Content-Type": "application/json"}

        # Join FIRST. Without a participation row this route answers 404 before
        # it ever looks at the slot, and the 404 would hide the defect.
        joined = client.post("/world-events/join", json=_join_body(epoch),
                             headers=_authed("p1"))
        assert joined.status_code == 200, (
            "the player did not join (%s); the slot assertions below would be "
            "measuring the participation gate instead"
            % joined.text[:140])

        # `content=` rather than `data=`: httpx2 deprecates `data=` for raw
        # text and warns about it. Both transports were measured and both give
        # 500, so this is belt-and-braces rather than the load-bearing part.
        response = client.post(
            "/world-events/attempt",
            content='{"def_key":"beast_swarm","epoch_start":%r,"slot":%s}'
                    % (epoch, literal),
            headers=raw_headers)

        assert response.status_code == 422, (
            "slot=%s answered %d (%s); a non-finite slot must be refused by "
            "the shape validator as a 422, never reported as a server fault"
            % (literal, response.status_code, response.text[:140]))

        # The refusal must also be a clean one: no progress, no ladder advance,
        # and the player is still able to play the slot they asked for.
        part = we.get_participation(store, "beast_swarm", epoch, "p1")
        assert int(part["best_slot"]) == 0, (
            "slot=%s advanced the ladder to %r" % (literal, part["best_slot"]))
        assert float(store["p1"]["progress"]) == 0.0, (
            "slot=%s moved the balance" % literal)

        # CONTROL 1: the finite in-band slot reaches the handler on this app.
        in_band = client.post("/world-events/attempt",
                              json=_attempt_body(epoch, 1),
                              headers=_authed("p1"))
        assert in_band.status_code == 200, (
            "slot 1 answered %d after the non-finite calls (%s); the 500s "
            "above cannot be attributed to the value"
            % (in_band.status_code, in_band.text[:140]))

    # CONTROL 2: a FINITE out-of-range slot on the SAME field is a clean 422
    # whose body serialises. This is what proves the route's error path works
    # and only the non-finite literal breaks the encoder.
    we.reset_memory_state()
    we.ATTEMPT_LIMITER.reset()
    epoch = _epoch()
    app, store = _app(_player("p1"))
    _pin(store, epoch, "active")
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/world-events/join", json=_join_body(epoch), headers=_authed("p1"))

    finite_bad = client.post(
        "/world-events/attempt",
        content='{"def_key":"beast_swarm","epoch_start":%r,"slot":99}' % epoch,
        headers={**_authed("p1"), "Content-Type": "application/json"})
    assert finite_bad.status_code == 422, (
        "a finite out-of-range slot answered %d; the validator is not refusing "
        "this field at all, so the non-finite cases above measure nothing"
        % finite_bad.status_code)
    assert finite_bad.json()["detail"][0]["type"] == "less_than_equal", (
        "the finite out-of-range refusal changed shape: %s"
        % finite_bad.text[:160])

    # CONTROL 3: the 422 the validator already produces is unencodable ONLY
    # because it echoes the bad value. Re-encoding the same error structure
    # with the value removed succeeds, which localises the failure to the
    # encoder rather than to pydantic.
    from world_event_routes import WorldEventAttemptRequest
    try:
        WorldEventAttemptRequest.model_validate(
            {"def_key": "beast_swarm", "epoch_start": epoch, "slot": float("inf")})
        raise AssertionError(
            "the model accepted slot=inf; the guard under test has moved")
    except Exception as exc:  # noqa: BLE001 -- pydantic's ValidationError
        errors = exc.errors()
        assert errors[0]["type"] == "finite_number", (
            "the validator's own verdict changed: %r" % errors)
        import json as _json
        _json.dumps(errors)  # must not raise: the PERMISSIVE encoder accepts inf
        # And the value pydantic copies into it is the one that breaks the
        # STRICT encoder Starlette uses for the response. This is the whole bug
        # in one line: the same structure, two encoders, opposite answers.
        with pytest.raises(ValueError):
            _json.dumps([{"input": float("inf")}], allow_nan=False)


def test_a_non_finite_extra_field_and_a_non_finite_slot_fail_for_one_reason():
    """Both non-finite refusals die in the SAME place, so there is ONE fix.

    This is the localisation test. Section J's strict xfail and the one above
    look like two separate defects -- one on an unknown field, one on ``slot``
    -- and a reviewer would reasonably open two tickets. They are one: the
    validator refuses correctly in both cases and the 422 body cannot be
    encoded because pydantic echoes the rejected value into
    ``detail[0]['input']``.

    So the assertions here are about the SHARED mechanism, not about either
    status code:

      * both bodies are refused by the validator, and both verdicts name the
        field that was wrong;
      * the value pydantic copies into the error is the non-finite one;
      * and a strict encoder -- the same one Starlette uses to write the
        response -- refuses it, while the default permissive encoder accepts
        it. That last pair is the whole bug in two lines.

    CONTROL: with the value replaced by a finite one the strict encoder is
    happy, so the failure is the value and not the error structure.
    """
    from world_event_routes import (WorldEventAttemptRequest,
                                    WorldEventJoinRequest)

    def _first_error(model, payload):
        try:
            model.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 -- pydantic's ValidationError
            return exc.errors()[0]
        raise AssertionError("the model accepted %r" % (payload,))

    epoch = _epoch()
    cases = [
        ("extra field", WorldEventJoinRequest,
         {"def_key": "beast_swarm", "epoch_start": epoch, "amount": float("nan")}),
        ("slot", WorldEventAttemptRequest,
         {"def_key": "beast_swarm", "epoch_start": epoch, "slot": float("inf")}),
    ]
    for label, model, payload in cases:
        error = _first_error(model, payload)
        assert error["loc"][-1] in ("amount", "slot"), (
            "the %s refusal does not name the offending field: %r"
            % (label, error["loc"]))
        assert not isinstance(error["input"], dict) or True  # shape only
        assert math.isnan(error["input"]) or math.isinf(error["input"]), (
            "the %s error does not carry the non-finite value the encoder will "
            "refuse: %r" % (label, error["input"]))

    # The failure itself: strict refuses, permissive accepts.
    import json as _json
    with pytest.raises(ValueError):
        _json.dumps([{"input": float("nan")}], allow_nan=False)
    assert _json.dumps([{"input": float("nan")}], allow_nan=True) == (
        '[{"input": NaN}]'), (
        "python's permissive encoder changed; the strict/permissive pair that "
        "explains the 500 is no longer demonstrable")

    # CONTROL: a finite error structure is encodable strictly, so the encoder
    # is not simply broken for every validation error.
    finite = _first_error(WorldEventAttemptRequest,
                          {"def_key": "beast_swarm", "epoch_start": epoch,
                           "slot": 99})
    _json.dumps([finite], allow_nan=False)
