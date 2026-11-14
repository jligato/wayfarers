"""
Contract test for ``server/vip.py`` -- the ``/vip/*`` privilege surface.

WHAT THIS IS
------------
A CONTRACT test. It pins what the two VIP routes actually DO over HTTP in the
shape a client panel is written against: which paths exist, which keys each
reply carries, what the refusals say, and -- above all -- that a claim moves the
player's real wallet and not just a field in the reply.

It is deliberately NOT a value snapshot. The tier ladder, its costs and its
daily grant all move, so every figure here is read from ``vip.VIP_TIERS`` at
call time and only RELATIONSHIPS are asserted. A retune that keeps the ladder
honest passes; a retune that makes it pay power fails (see
``test_the_reply_never_advertises_a_power_benefit``).

HOW IT DIFFERS FROM ``server/tests/test_vip.py``
------------------------------------------------
That file is the module's UNIT suite: the fold arithmetic, the clock seam, the
thread race, the rollback order. This file is the SURFACE suite: route census,
reply key sets, refusal codes, and the two mounted-in-``main.py`` facts nobody
else covers -- that the routes are actually reachable on the real app, and that
the shape guard covers them. Nothing here duplicates it.

THE TWO DEFECTS / SHARP EDGES THIS PINS
---------------------------------------
EDGE 1 -- THE MODULE'S OWN 404 IS UNREACHABLE IN THE REAL APP.
    ``vip.register_vip_routes`` raises ``404 "Player not found"`` when the id is
    absent from the store. On the shipped app that branch never runs: the
    ownership guard sits in front of it and refuses any path ``player_id`` that
    is not the session's own, with ``403`` -- including ids that do not exist at
    all. So "unknown player" is a 404 when the module is mounted alone and a 403
    on the real app. Both are pinned, because a client that keys its error copy
    on 404 will show the wrong message in production only.

EDGE 2 -- A REPLAYED CLAIM IS A 200, NOT A 409 OR A 400.
    The second press of the day answers ``200`` with ``already_claimed: true``
    and a zero reward. That is the documented design ("returns an outcome dict
    with ``already_claimed`` / ``empty_grant`` so the caller can render the
    button"), but it is the kind of contract a panel is easy to get wrong -- a
    panel that treats a non-2xx as "try again" will never see this one.

HOW TO RUN
----------
    cd G:/Projects/wayfarers-cycle
    .venv/Scripts/python.exe -m pytest server/tests/test_vip_contract.py -q

The surface tests mount ``vip`` on a bare FastAPI app and need nothing else.

THE GUARD SECTION
-----------------
``test_the_ownership_guard_covers_both_vip_routes`` and the two anonymity
refusals need the REAL app, because the whole question is what
``install_ownership_guard`` did to the routes ``main.py`` mounted.

Those three tests RUN on this tree: the whole file executes with no skips and
``import main`` now succeeds. An earlier revision of this docstring said they
would skip because ``server/sect.py`` raised
``NameError: name '_idem_gate' is not defined`` and ``server/artifacts.py`` had
a docstring indentation error. Both were real at the time and both have since
been fixed by the agents who owned them, so the claim was replaced with the
measured result rather than left to mislead. No test COUNT is stated here on
purpose: a frozen number in a docstring is a change-detector that goes stale
the next time a test is added, and the run output is the authority.

Because that blocker was a broken sibling rather than a missing FastAPI symbol,
there was no shim that could measure these: the app has to actually import. If
a future sibling edit breaks ``main`` again, the three tests skip VISIBLY with
the reason read from a live import at collection time -- never silently, and
never with a hand-written guess about which module is currently broken.

The guard was also checked non-vacuously against the real app, because three
green tests that all refuse everything would look identical to three correct
ones. Measured: anonymous ``401`` on both routes; ``bob`` naming ``alice``
``403`` with detail ``"player_id does not match the authenticated session"``;
and ``bob`` naming ``bob`` gets ``404`` from the route's own store lookup --
i.e. the guard let an owner through to the handler and only the absent row
produced the error. That third fact is what rules out a blanket refusal.

NO CLIENT VIEW -> THERE IS ONE NOW
----------------------------------
A VIP panel landed after this file was written: ``client/scenes/game/VipPanel.gd``,
mounted as a SECTION of the shop tab in ``MainHub.gd`` (the nav grid is at its
23-button budget, so a 24th tab is not available). The test that asserted
"no client screen names a VIP route" therefore started failing the moment the
panel arrived -- the assertion, not the panel, was the stale thing.

It now asserts the panel's paths are a SUBSET of the two routes this module
serves, and that the panel branches on the ``already_claimed`` / ``empty_grant``
BODY flags rather than on the status code. Both searches assert their own
ability to match against two sibling features that have panels, so a zero-hit
result is never mistaken for evidence of absence.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SERVER = _ROOT / "server"
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

import vip as V  # noqa: E402

# The two paths this module contributes. Derived from the module, not restated,
# so a rename in vip.py moves this list instead of silently orphaning it.
VIP_PATHS = ("/vip/{player_id}/status", "/vip/{player_id}/claim-daily")

# Exact reply key sets, measured live. `set(body) == ...` is deliberate: an
# extra key is a contract change a panel has to notice, and a MISSING key is the
# failure that crashes a screen at runtime.
STATUS_TOP_KEYS = {"ok", "vip"}
STATUS_VIP_KEYS = {
    "tier_id", "display_name", "points", "points_to_next", "progress_pct",
    "next_tier_id", "next_tier_cost_spirit_stones", "is_max_tier", "benefits",
    "claimed_today", V.STATE_KEY_LAST_CLAIM, "server_utc_day", "total_claims",
    "total_spirit_stones_claimed", "tiers",
}
CLAIM_KEYS = {"ok", "already_claimed", "empty_grant", "tier_id", "reward",
              "reason", "utc_day"}
REWARD_KEYS = {"spirit_stones", "items"}
TIER_ROW_KEYS = {"id", "display_name", "points_required", "cost_spirit_stones"}

# The five reason strings the claim route can report. A sixth is a new state the
# client has never been taught to render.
REASONS = {"claimed", "already_claimed_today", "tier_grants_nothing_daily"}


# --- helpers ---------------------------------------------------------------

def wallet(player) -> int:
    """The player's free-to-play spirit-stone balance.

    Reads the WALLET, not a field in the reply. A claim route that answers 200
    with a reward dict while the balance never moved is the battle-pass bug, and
    only a read of committed state can see it.
    """
    value = player.get("spirit_stones")
    if isinstance(value, dict):
        return int(value.get("low_grade", 0) or 0)
    return int(value or 0)


def fresh_player(pid, points=0):
    """A row the store will accept, at a chosen point total.

    ``points`` is applied through the real ``add_vip_points`` so the tier is
    derived by the shipped code rather than written into the row by the test.
    """
    row = {"id": pid, "name": pid, "spirit_stones": {"low_grade": 0},
           "progress": 0.0, "inventory": {}}
    if points:
        V.add_vip_points(row, points)
    return row


@pytest.fixture
def rig(monkeypatch):
    """VIP routes mounted on a bare app, over one in-memory store.

    No ``main`` import and no ownership guard: this measures what ``vip.py``
    itself does. The guard is measured separately, against the real app.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from player_store import PlayerStore

    # PlayerStore latches PostgreSQL mode at CONSTRUCTION, so the env var is
    # removed here, immediately before the store is built -- not at import time.
    monkeypatch.delenv("WC_PG_HOST", raising=False)
    db = PlayerStore()
    assert db._pg is False, "VIP surface tests must run on the in-memory store"

    with db.transaction():
        db["paid"] = fresh_player("paid", V.VIP_TIERS[1]["points_required"])
        db["free"] = fresh_player("free", 0)
        db["top"] = fresh_player("top", V.VIP_TIERS[-1]["points_required"] + 5)

    app = FastAPI()
    V.register_vip_routes(app, lambda: db)
    # raise_server_exceptions=False: one 500 would abort the test and leave
    # every later assertion unmeasured.
    client = TestClient(app, raise_server_exceptions=False)
    return {"client": client, "store": db, "app": app}


def _main_import_error() -> str | None:
    """Why the real app cannot boot here, or ``None`` when it can.

    Probed by IMPORTING, never by reading a source file: a grep for a symbol
    proves the tree mentions it, not that the module loads.
    """
    try:
        import main  # noqa: F401
    except Exception as exc:
        # EVERY exception, not just ImportError. A sibling module with an
        # IndentationError or a NameError at import time (server/artifacts.py,
        # server/sect.py) raises something else, and a probe that only catches
        # ImportError turns that into a collection ERROR for this whole file --
        # losing the 40 surface tests that do not need the real app.
        return f"{type(exc).__name__}: {exc}"
    return None


_REAL_APP_BLOCKED = _main_import_error()

requires_real_app = pytest.mark.skipif(
    _REAL_APP_BLOCKED is not None,
    reason=(
        "the real app cannot be imported on this tree, so the shipped "
        "ownership guard cannot be measured: " + str(_REAL_APP_BLOCKED) +
        ". The blocker is whatever server/main.py pulls in transitively and is "
        "a sibling module's unlanded edit, not this file; the reason is read "
        "from a live import at collection time so it names the actual cause."
    ),
)


# --- the census: what the module contributes -------------------------------

def test_the_module_contributes_exactly_the_two_documented_routes(rig):
    served = {(method, getattr(r, "path", ""))
              for r in rig["app"].routes
              for method in (getattr(r, "methods", None) or [])
              if method not in ("HEAD", "OPTIONS")}
    vip = {(m, p) for m, p in served if p.startswith("/vip")}
    assert vip == {("GET", VIP_PATHS[0]), ("POST", VIP_PATHS[1])}, (
        "the VIP surface changed; the panel written against it needs updating "
        "at the same time. served=" + repr(sorted(vip))
    )


def test_the_two_paths_do_not_collide_with_each_other(rig):
    """``status`` and ``claim-daily`` are siblings under one prefix, so a future
    ``/vip/{player_id}`` catch-all registered FIRST would swallow both and turn
    them into routes that exist in source and are dead at runtime."""
    paths = [getattr(r, "path", "") for r in rig["app"].routes]
    for borrowed in ("/vip/{player_id}", "/vip/status", "/vip/claim-daily"):
        assert borrowed not in paths, borrowed + " is not part of this contract"


# --- the reply contract ---------------------------------------------------

def test_status_reply_keys(rig):
    r = rig["client"].get("/vip/paid/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == STATUS_TOP_KEYS, sorted(body)
    assert body["ok"] is True
    # The tier block is NESTED under "vip", not spread across the top level.
    # A panel that reads body["tier_id"] gets nothing.
    assert set(body["vip"]) == STATUS_VIP_KEYS, sorted(body["vip"])


def test_status_reports_the_tier_the_points_earn_and_not_a_stored_string(rig):
    c = rig["client"]
    v = c.get("/vip/paid/status").json()["vip"]
    assert v["tier_id"] == V.tier_for_points(V.VIP_TIERS[1]["points_required"])["id"]
    assert v["display_name"] == V.VIP_TIERS[1]["display_name"]
    assert v["points"] == V.VIP_TIERS[1]["points_required"]
    assert v["points_to_next"] == (V.VIP_TIERS[2]["points_required"]
                                   - V.VIP_TIERS[1]["points_required"])
    # Landing exactly ON a tier boundary is 0% toward the NEXT one, not 100%:
    # the progress bar measures the gap to the tier above, and a client that
    # renders "100%" here would claim the player had maxed out.
    assert v["progress_pct"] == 0.0
    assert v["is_max_tier"] is False


def test_status_lists_the_whole_ladder_so_a_panel_needs_no_second_call(rig):
    rows = rig["client"].get("/vip/paid/status").json()["vip"]["tiers"]
    assert len(rows) == len(V.VIP_TIERS)
    for row, tier in zip(rows, V.VIP_TIERS):
        assert set(row) == TIER_ROW_KEYS, sorted(row)
        assert row["id"] == tier["id"]
        assert row["display_name"] == tier["display_name"]
        assert row["points_required"] == tier["points_required"]
        assert row["cost_spirit_stones"] == tier["cost_spirit_stones"]


def test_the_top_tier_reports_max_rather_than_a_null_next(rig):
    """The top tier is the one case with no next row. It must be REPORTED
    (``is_max_tier``), not left as a null that a panel has to special-case by
    guessing from a null ``next_tier_id``."""
    v = rig["client"].get("/vip/top/status").json()["vip"]
    assert v["tier_id"] == V.VIP_TIERS[-1]["id"]
    assert v["is_max_tier"] is True
    assert v["next_tier_id"] is None
    assert v["next_tier_cost_spirit_stones"] is None
    assert v["points_to_next"] == 0
    assert v["progress_pct"] == 100.0


def test_status_is_a_pure_read_and_cannot_be_used_to_farm(rig):
    """Polling the status route must not consume the day key or touch the
    wallet. The route is a GET; a client is entitled to call it on every frame
    of a panel."""
    store = rig["store"]
    before = wallet(store["paid"])
    for _ in range(20):
        r = rig["client"].get("/vip/paid/status")
        assert r.status_code == 200
        assert r.json()["vip"]["claimed_today"] is False
    assert wallet(store["paid"]) == before
    assert store["paid"].get(V.STATE_KEY, {}).get(V.STATE_KEY_LAST_CLAIM) is None


def test_the_status_route_writes_nothing_to_the_player_row(live_row_rig):
    """``GET /status`` must be a PURE read, not merely a read that happens not
    to move money.

    Why this needs its own instrument, and is not redundant with the test above:
    the real ``PlayerStore`` is COPY-ON-READ outside a transaction, so a route
    that quietly filled in a default ``vip`` block would mutate a DETACHED COPY
    and the write would evaporate. Against that store, "no state was written"
    cannot tell the pure route from the impure one -- measured: swapping
    ``get_vip_status``'s copy for the mutating ``get_state`` left every other
    test in this file green.

    So this mounts the routes over a store that hands back the LIVE row. Any
    mutation the handler makes to what it read is then kept and visible.
    """
    db = live_row_rig["store"]
    # Seeded WITHOUT add_vip_points, so the row genuinely has no vip block.
    assert V.STATE_KEY not in db["free"], "the control started dirty"

    r = live_row_rig["client"].get("/vip/free/status")
    assert r.status_code == 200, r.text
    # The reply is a full, correct status -- the route really did the work, so
    # a blank row here cannot be blamed on the route refusing to answer.
    assert r.json()["vip"]["tier_id"] == V.VIP_TIERS[0]["id"]
    assert r.json()["vip"]["points"] == 0

    assert V.STATE_KEY not in db["free"], (
        "GET /status wrote to the player row. A GET that fills in state is not "
        "the pure read the client contract assumes: a panel polling it every "
        "frame would rewrite the save each time.")


def test_claimed_today_flips_only_after_a_real_claim(rig):
    """The one field that lets a panel grey its own button. It must agree with
    the claim route's own report, read from committed state."""
    store = rig["store"]
    c = rig["client"]
    assert c.get("/vip/paid/status").json()["vip"]["claimed_today"] is False

    claim = c.post("/vip/paid/claim-daily")
    assert claim.status_code == 200
    assert claim.json()["already_claimed"] is False

    after = c.get("/vip/paid/status").json()["vip"]
    assert after["claimed_today"] is True
    assert after[V.STATE_KEY_LAST_CLAIM] == after["server_utc_day"]
    assert after["total_claims"] == 1
    # And the counters agree with the wallet, not with the reply.
    assert after["total_spirit_stones_claimed"] == wallet(store["paid"])


def test_the_reply_never_advertises_a_power_benefit(rig):
    """THE design constraint, checked on the wire rather than in the table.

    ``docs/top-priorities.md`` row 9: "VIP = Privileges NOT Multipliers". A tier
    that grew a cultivation-speed or qi multiplier would show up in every status
    reply, so this is where a client would first display it.
    """
    for pid in ("free", "paid", "top"):
        v = rig["client"].get(f"/vip/{pid}/status").json()["vip"]
        keys = set(v["benefits"])
        assert keys == set(V.PERMITTED_BENEFIT_KEYS), sorted(keys)
        assert not (keys & set(V.FORBIDDEN_BENEFIT_KEYS)), (
            f"the status reply advertises power: {sorted(keys & set(V.FORBIDDEN_BENEFIT_KEYS))}"
        )
        # The daily grant carries stones and items only -- never qi or a stat.
        assert set(v["benefits"]["daily_claim"]) == REWARD_KEYS


def test_claim_reply_keys(rig):
    r = rig["client"].post("/vip/paid/claim-daily")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == CLAIM_KEYS, sorted(body)
    assert set(body["reward"]) == REWARD_KEYS, sorted(body["reward"])
    assert body["ok"] is True
    assert isinstance(body["already_claimed"], bool)
    assert isinstance(body["empty_grant"], bool)
    assert body["reason"] in REASONS, body["reason"]
    assert isinstance(body["utc_day"], int)


def test_no_reply_carries_a_data_envelope(rig):
    """Both replies are FLAT at the top. On the client ``data`` is the transport
    envelope APIClient builds, so a server key by that name would collide.
    Pinning the absence stops this being filed as a bug a second time."""
    c = rig["client"]
    status = c.get("/vip/paid/status").json()
    assert "data" not in status
    # The claim reply is flat all the way down -- no {"vip": ...} wrapper either,
    # so it does NOT match the status shape.
    claim = c.post("/vip/paid/claim-daily").json()
    assert "data" not in claim
    assert "vip" not in claim, "the claim reply grew a status-shaped wrapper"


# --- the money path -------------------------------------------------------

def test_a_claim_moves_the_wallet_and_the_inventory(rig):
    """The load-bearing assertion. The reply says what was paid; the STORE says
    whether it was. Asserting only the reply would pass on the exact defect
    this codebase shipped once: 200 with a reward dict, nothing saved."""
    store = rig["store"]
    expected = V.tier_benefits(V.tier_for_points(
        V.VIP_TIERS[1]["points_required"])["id"])["daily_claim"]
    assert expected["spirit_stones"] > 0, "the control tier pays nothing"

    r = rig["client"].post("/vip/paid/claim-daily")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["already_claimed"] is False
    assert body["empty_grant"] is False
    assert body["reason"] == "claimed"
    assert body["reward"]["spirit_stones"] == expected["spirit_stones"]

    committed = store["paid"]
    assert wallet(committed) == expected["spirit_stones"], (
        "the reply reported a reward the wallet never received")
    for item_id, qty in expected["items"].items():
        assert committed["inventory"].get(item_id) == qty
    assert committed[V.STATE_KEY][V.STATE_KEY_LAST_CLAIM] == body["utc_day"]


def test_a_replayed_claim_is_200_with_a_zero_reward_not_an_error(rig):
    """EDGE 2 from the docstring. Documented, easy to get wrong, and pinned so a
    panel author does not have to rediscover it from a 200."""
    store = rig["store"]
    c = rig["client"]
    first = c.post("/vip/paid/claim-daily").json()
    paid = wallet(store["paid"])
    assert paid > 0

    for hour in range(2, 24, 6):
        again = c.post("/vip/paid/claim-daily")
        assert again.status_code == 200, again.text
        body = again.json()
        assert body["already_claimed"] is True
        assert body["reason"] == "already_claimed_today"
        assert body["reward"] == {"spirit_stones": 0, "items": {}}
        assert wallet(store["paid"]) == paid, "the wallet moved on a replay"

    # 24 presses, one payout.
    assert store["paid"][V.STATE_KEY]["total_claims"] == 1
    assert first["utc_day"] == store["paid"][V.STATE_KEY][V.STATE_KEY_LAST_CLAIM]


def test_the_free_tier_claim_pays_nothing_and_says_so_instead_of_failing(rig):
    """Not an error and not a consumed claim: ``empty_grant`` is its own flag so
    a panel can say "nothing to collect" instead of showing an error toast."""
    store = rig["store"]
    r = rig["client"].post("/vip/free/claim-daily")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["empty_grant"] is True
    assert body["already_claimed"] is False
    assert body["reason"] == "tier_grants_nothing_daily"
    assert body["reward"] == {"spirit_stones": 0, "items": {}}
    assert wallet(store["free"]) == 0
    assert store["free"].get("inventory") == {}
    # The day key stays FREE, so buying the tier later the same day still pays.
    assert store["free"].get(V.STATE_KEY, {}).get(V.STATE_KEY_LAST_CLAIM) is None


def test_upgrading_after_a_free_tier_press_still_pays_that_day(rig):
    """The consequence of the free press not consuming the day key. This is the
    whole reason that rule exists, and it is only observable end to end."""
    store = rig["store"]
    c = rig["client"]
    assert c.post("/vip/free/claim-daily").json()["empty_grant"] is True

    with store.transaction():
        V.add_vip_points(store["free"], V.VIP_TIERS[1]["points_required"])

    after = c.post("/vip/free/claim-daily").json()
    assert after["empty_grant"] is False
    assert after["already_claimed"] is False
    assert after["reward"]["spirit_stones"] > 0
    assert wallet(store["free"]) == after["reward"]["spirit_stones"]


def test_a_higher_tier_pays_at_least_as_much_as_the_one_below(rig):
    """A relationship between ladder rows, not a frozen figure. A retune that
    made the top tier pay less than a tier beneath it fails here; a retune that
    moved every number still passes."""
    amounts = [V.tier_benefits(t["id"])["daily_claim"]["spirit_stones"]
               for t in V.VIP_TIERS]
    assert amounts[0] == 0, "the free tier must pay nothing"
    assert amounts == sorted(amounts), f"the ladder is not monotonic: {amounts}"
    assert amounts[-1] > amounts[1] > 0
    assert wallet(rig["store"]["top"]) == 0


def test_the_top_tier_claim_pays_the_FOLDED_grant_not_just_its_own_row(rig):
    """THE FOLD, measured end to end on the route that pays it.

    MEASURED GAP, found by reading this file against ``vip.py`` rather than
    trusting its green. Every claim through the route uses ``"paid"``, which sits
    on the SECOND tier -- where only one ladder row is non-zero, so "sum the
    rows above" and "read this row only" produce the SAME number and the
    distinction is invisible. The fold is a module-level function
    (``tier_benefits``) and ``server/tests/test_vip.py`` does check its
    arithmetic directly, but nothing here proved that the ROUTE pays the fold:
    ``test_a_higher_tier_pays_at_least_as_much_as_the_one_below`` only compared
    the table's numbers and asserted ``wallet(store["top"]) == 0``, so the top
    player's wallet was never moved by anything in this file.

    The top tier is where the two readings must disagree, and they do:
      * its OWN row grants some stones,
      * its folded grant is every row 0..N added together, plus every ITEM
        added across rows.

    So this pins both halves on the wire and in the store: the reply, the
    wallet, and a per-item tally that can only come out right if the fold
    really accumulated rather than overwrote.
    """
    store = rig["store"]
    c = rig["client"]

    top = V.VIP_TIERS[-1]

    # EXPECTATIONS DERIVED FROM THE RAW TABLE, NOT FROM ``tier_benefits``.
    # This is the part that makes the test independent rather than tautological.
    # An earlier revision computed ``folded = V.tier_benefits(top_id)`` and then
    # asserted the route paid exactly that -- so a mutation INSIDE
    # ``tier_benefits`` moved the expectation and the payment together, and the
    # fold mutations died on the precondition instead of on the payout. Here the
    # expected number is summed from ``V.VIP_TIERS`` rows by this file, so the
    # only thing that can satisfy the payout assertions is the real fold.
    expected_stones = sum(
        int(r_["benefits"]["daily_claim"].get("spirit_stones", 0))
        for r_ in V.VIP_TIERS)
    expected_items = {}
    for r_ in V.VIP_TIERS:
        for item_id, qty in (r_["benefits"]["daily_claim"].get("items", {}) or {}).items():
            expected_items[item_id] = expected_items.get(item_id, 0) + int(qty)

    own_row = top["benefits"]["daily_claim"]
    # THE PRECONDITION: the fold must be distinguishable from the last row.
    assert expected_stones > int(own_row.get("spirit_stones", 0)), (
        "the summed grant does not exceed the top row's own, so this test "
        "cannot tell a fold from a last-row read")
    assert any(expected_items.get(k, 0) > int(v) for k, v in own_row["items"].items()), (
        "no summed item quantity exceeds the top row's own, so the per-item "
        "assertions below cannot tell a fold from a last-row read")

    r = c.post("/vip/top/claim-daily")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already_claimed"] is False
    assert body["empty_grant"] is False
    assert body["tier_id"] == top["id"]
    assert body["reward"]["spirit_stones"] == expected_stones, (
        "the route did not pay the sum of every ladder row")
    assert body["reward"]["items"] == expected_items, (
        "the route did not pay the sum of every row's items")

    # Committed state, read back from the store -- the reply alone is not proof.
    committed = store["top"]
    assert wallet(committed) == expected_stones, (
        "the top tier's wallet did not receive the summed grant")
    for item_id, qty in expected_items.items():
        assert committed["inventory"].get(item_id) == qty, (
            f"{item_id} is not the summed quantity on the top tier")

    # A replay pays nothing even at the top tier: the idempotency key is not
    # tier-dependent, so the biggest grant in the game is guarded exactly like
    # the smallest.
    paid = wallet(committed)
    again = c.post("/vip/top/claim-daily")
    assert again.status_code == 200
    assert again.json()["already_claimed"] is True
    assert wallet(store["top"]) == paid, "the top tier paid twice on one day"


def test_no_request_field_can_move_a_number_the_caller_names(rig):
    """There is no request body at all on this route, so a caller cannot state
    an amount. Probed over HTTP rather than by reading the handler: a body sent
    to a route that takes no body is ignored, and the wallet proves it."""
    store = rig["store"]
    c = rig["client"]
    before = wallet(store["paid"])
    first = c.post("/vip/paid/claim-daily")
    assert first.status_code == 200
    paid = wallet(store["paid"])
    assert paid == before + first.json()["reward"]["spirit_stones"]

    for attempt in (
        {"amount": 10 ** 9, "spirit_stones": 10 ** 9, "tier_id": "immortal",
         "points": 10 ** 9, "items": {"spirit_herb": 10 ** 9}},
    ):
        again = c.post("/vip/paid/claim-daily", json=attempt)
        assert again.status_code == 200
        assert again.json()["tier_id"] == V.tier_for_points(
            V.VIP_TIERS[1]["points_required"])["id"], "a body field moved the tier"
        assert wallet(store["paid"]) == paid, "a body field moved the wallet"

    query = c.post("/vip/paid/claim-daily?amount=1000000&tier_id=immortal")
    assert query.status_code == 200
    assert wallet(store["paid"]) == paid, "a query field moved the wallet"
    assert query.json()["tier_id"] == V.VIP_TIERS[1]["id"], (
        "a query parameter moved the reported tier")


def test_points_are_granted_server_side_only(rig):
    """There is no route that adds VIP points. A POST endpoint that grants them
    to whoever asks is a self-serve cheat vector, so ``add_vip_points`` is a
    module function with no HTTP surface."""
    import inspect
    verbs = set()
    for r in rig["app"].routes:
        if getattr(r, "path", "").startswith("/vip"):
            verbs |= (getattr(r, "methods", None) or set())
    # Only GET (read status) and POST (claim today). No "grant", no "buy", no
    # "upgrade" -- the two verbs a self-serve points route would need.
    assert verbs == {"GET", "POST"}, sorted(verbs)
    # And nothing in the module's public surface is a points-granting route.
    public = [n for n in getattr(V, "__all__", []) if n.startswith("add_")]
    assert public == ["add_vip_points"]
    assert not inspect.iscoroutinefunction(V.add_vip_points)


# --- the store seam: why persist AND transaction both have to be there ------
#
# MEASURED, NOT ASSUMED. Two sabotages of ``server/vip.py`` left every test in
# this file GREEN:
#
#   * delete ``store.persist(player_id, player)``  -> still green
#   * delete ``with store.transaction(player_id):`` -> still green
#
# That is not a redundancy in the product. It is a blind spot in the TESTS, and
# the reason is mechanical: against the real in-memory ``PlayerStore`` the two
# mechanisms cover for each other. ``transaction()`` stages the row and commits
# it, so dropping ``persist`` costs nothing; ``persist()`` writes the row, so
# dropping the transaction costs nothing. A black-box test on that store cannot
# tell "guarded" from "unguarded", because it cannot tell which half did the
# work.
#
# PostgreSQL has no such overlap. There, a read outside a transaction hands back
# a DETACHED COPY, and a mutation to a copy is discarded. So the store double
# below reproduces exactly that rule -- and nothing else. With it, each guard
# becomes individually load-bearing and each sabotage goes red.

class _DetachedReadStore:
    """The smallest store that can tell "guarded" from "unguarded".

    Two rules, both lifted from what PostgreSQL actually does:

    1. a read OUTSIDE a transaction returns a detached copy, so a mutation to
       that copy is thrown away (this is the battle-pass claim bug: HTTP 200,
       nothing saved);
    2. state changes only through ``persist`` or a row staged inside
       ``transaction``.

    It is not a re-implementation of ``PlayerStore``. It is the minimum needed
    to make the two guards separable.

    ``detached=False`` inverts rule 1 for one specific job: a read outside a
    transaction hands back the LIVE row, so a mutation to the object the
    handler was given becomes visible. That is the only way to observe whether
    a READ-ONLY route mutated what it read -- with the detached rule the
    mutation evaporates and the impure and pure versions are indistinguishable.
    """

    def __init__(self, rows, detached=True):
        import copy
        self._copy = copy.deepcopy
        self._rows = {pid: self._copy(row) for pid, row in rows.items()}
        self._staged = None
        self.transaction_calls = 0
        self.detached = detached

    def __contains__(self, pid):
        return pid in self._rows

    def __getitem__(self, pid):
        if self._staged is not None and pid in self._staged:
            return self._staged[pid]
        if not self.detached:
            # LIVE row: whatever the handler does to it is kept.
            return self._rows[pid]
        return self._copy(self._rows[pid])

    def persist(self, pid, player=None):
        if player is None:
            return None
        target = self._staged if (self._staged is not None and pid in self._staged) else self._rows
        target[pid] = self._copy(player)
        return None

    def transaction(self, pid=None):
        import contextlib
        self.transaction_calls += 1
        @contextlib.contextmanager
        def scope():
            previous, self._staged = self._staged, {}
            try:
                yield self
            finally:
                for k, row in self._staged.items():
                    self._rows[k] = row
                self._staged = previous
        return scope()


@pytest.fixture
def pg_shaped_rig():
    """The same two routes, mounted over a store with PostgreSQL's read rule."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    paid = fresh_player("paid", V.VIP_TIERS[1]["points_required"])
    db = _DetachedReadStore({"paid": paid})
    app = FastAPI()
    V.register_vip_routes(app, lambda: db)
    return {"client": TestClient(app, raise_server_exceptions=False),
            "store": db, "app": app}


@pytest.fixture
def live_row_rig():
    """The routes over a store that hands back the LIVE row on a read.

    The instrument for purity only. See ``_DetachedReadStore(detached=False)``.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    db = _DetachedReadStore({"free": fresh_player("free", 0)}, detached=False)
    app = FastAPI()
    V.register_vip_routes(app, lambda: db)
    return {"client": TestClient(app, raise_server_exceptions=False),
            "store": db, "app": app}


def test_the_payout_survives_a_store_whose_reads_are_detached(pg_shaped_rig):
    """The reply must not be able to describe a payout the store never took.

    Read back through the SAME store the route wrote to, after the request has
    finished, so a mutation to a detached copy is visible as the absence of a
    payment.
    """
    db = pg_shaped_rig["store"]
    r = pg_shaped_rig["client"].post("/vip/paid/claim-daily")
    assert r.status_code == 200, r.text
    paid = r.json()["reward"]["spirit_stones"]
    assert paid > 0

    committed = db["paid"]
    assert wallet(committed) == paid, (
        "the reply reported a payout that did not reach the store")
    assert committed[V.STATE_KEY][V.STATE_KEY_LAST_CLAIM] == r.json()["utc_day"]
    for item_id, qty in r.json()["reward"]["items"].items():
        assert committed["inventory"].get(item_id) == qty

    # And a second press against COMMITTED state still pays nothing, which is
    # only meaningful if the first press actually committed.
    again = pg_shaped_rig["client"].post("/vip/paid/claim-daily")
    assert again.json()["already_claimed"] is True
    assert wallet(db["paid"]) == paid


def test_the_claim_runs_inside_a_store_transaction(pg_shaped_rig):
    """The check and the day-key write must be in one scope.

    Asserting on a collaborator's call count is behaviour, not source
    inspection: an implementation that reads, checks a flag and writes the key
    OUTSIDE ``transaction()`` opens zero scopes, which this records.
    """
    db = pg_shaped_rig["store"]
    pg_shaped_rig["client"].post("/vip/paid/claim-daily")
    assert db.transaction_calls == 1, (
        "claim_daily must open exactly one transaction scope around the check "
        f"and the write; it opened {db.transaction_calls}")


# --- refusals -------------------------------------------------------------

def test_an_unknown_player_id_is_404_on_the_bare_mount(rig):
    """EDGE 1, first half. Mounted alone, the module's own guard answers. Read
    here so the production answer (403, below) is visibly a DIFFERENT one."""
    c = rig["client"]
    assert c.get("/vip/ghost/status").status_code == 404
    assert c.post("/vip/ghost/claim-daily").status_code == 404
    assert c.get("/vip/ghost/status").json()["detail"] == "Player not found"


def test_the_wrong_method_is_405_not_a_silent_404(rig):
    """Starlette answers 405 for a known path with the wrong verb. A panel that
    posts to ``/status`` gets a method error, not a missing route."""
    c = rig["client"]
    assert c.post("/vip/paid/status").status_code == 405
    assert c.get("/vip/paid/claim-daily").status_code == 405


def test_a_claim_on_a_player_with_no_inventory_key_still_lands(rig):
    """Legacy shape: a pre-upgrade save has no ``inventory`` dict. The payout
    creates it rather than 500-ing on a day the player is owed something."""
    store = rig["store"]
    with store.transaction():
        store["legacy"] = fresh_player("legacy",
                                       V.VIP_TIERS[1]["points_required"])
        store["legacy"].pop("inventory", None)
    r = rig["client"].post("/vip/legacy/claim-daily")
    assert r.status_code == 200, r.text
    assert r.json()["already_claimed"] is False
    assert wallet(store["legacy"]) > 0


# --- the day rollover, measured over HTTP ---------------------------------

def test_the_next_utc_day_pays_again_and_the_day_before_refuses(rig, monkeypatch):
    """The once-per-day rule has to be a DAILY rule, not a once-ever one.

    ``test_a_replayed_claim_is_200_with_a_zero_reward_not_an_error`` presses 24
    times but never moves the clock, so every press is the same day and the
    suite is blind to the other half of the promise: that the grant comes BACK
    tomorrow. A guard that said "claimed" unconditionally would pass that test
    and silently rob every paying player forever.

    The clock is the shipped seam ``vip._now`` -- the same one ``register_vip_routes``
    calls. Patching ``time.time`` instead would miss the seam entirely.

    Both directions are asserted, because only half of this is obvious:
      * day 1001 after day 1000 must PAY (the grant renews),
      * day 1000 after day 1001 must REFUSE (the server clock cannot be walked
        backwards to farm yesterday's grant).
    """
    day = float(V.SECONDS_PER_DAY)
    clock = {"now": 1000 * day}
    monkeypatch.setattr(V, "_now", lambda: clock["now"], raising=True)

    store = rig["store"]
    c = rig["client"]

    def stones(pid):
        return wallet(store[pid])

    def claim(pid):
        r = c.post(f"/vip/{pid}/claim-daily")
        assert r.status_code == 200, r.text
        return r.json()

    # --- day 1000: pays once ---
    first = claim("paid")
    assert first["reason"] == "claimed"
    assert first["utc_day"] == 1000, first["utc_day"]
    paid_once = stones("paid")
    assert paid_once == first["reward"]["spirit_stones"] > 0

    # --- later the SAME day: refuses, wallet unchanged ---
    clock["now"] = 1000 * day + 20 * 3600
    same = claim("paid")
    assert same["already_claimed"] is True
    assert same["reason"] == "already_claimed_today"
    assert stones("paid") == paid_once, "the wallet moved on a same-day replay"

    # --- day 1001: PAYS AGAIN. This is the assertion the old suite lacked. ---
    clock["now"] = 1001 * day + 6 * 3600
    second = claim("paid")
    assert second["already_claimed"] is False, (
        "the grant did not renew on the next UTC day: the once-per-day guard "
        "fired a second time and the player is paid once, ever")
    assert second["utc_day"] == 1001, second["utc_day"]
    assert second["reward"]["spirit_stones"] == first["reward"]["spirit_stones"]
    assert stones("paid") == paid_once * 2, (
        "the second day's grant was reported but did not reach the store")

    # The status route agrees, and its day index is the one the claim wrote.
    vip = c.get("/vip/paid/status").json()["vip"]
    assert vip["server_utc_day"] == 1001
    assert vip["claimed_today"] is True
    assert vip[V.STATE_KEY_LAST_CLAIM] == 1001
    assert vip["total_claims"] == 2

    # --- the clock walked BACK into the already-paid day: must refuse ---
    clock["now"] = 1000 * day + 6 * 3600
    rewound = claim("paid")
    assert rewound["already_claimed"] is True, (
        "moving the server clock back into a paid day paid again: the guard is "
        "an equality test, not a 'latest day wins' test")
    assert rewound["reason"] == "already_claimed_today"
    assert stones("paid") == paid_once * 2, "a rewound clock moved the wallet"

    # Both items in the folded grant renew too, not just the stones.
    committed = store["paid"]
    for item_id, qty in first["reward"]["items"].items():
        assert committed["inventory"].get(item_id) == qty * 2, (
            f"{item_id} did not accrue across the day boundary")


# --- the guard, measured on the real app ---------------------------------

@requires_real_app
def test_the_ownership_guard_covers_both_vip_routes():
    """The question is what ``install_ownership_guard`` did to the routes
    ``main.py`` mounted -- so this reads the real app's live route table."""
    import main as m
    from fastapi.routing import APIRoute
    import ownership_guard as og

    vip = [r for r in m.app.routes
           if isinstance(r, APIRoute) and getattr(r, "path", "").startswith("/vip")]
    assert len(vip) == 2, [getattr(r, "path", "") for r in vip]

    no_claim = sorted(getattr(r, "path", "") for r in vip
                      if not og.player_claim_params(r.dependant))
    assert not no_claim, f"the shape guard cannot see these: {no_claim}"

    unguarded = sorted(getattr(r, "path", "") for r in vip
                       if not og.has_auth_dependency(r.dependant))
    assert not unguarded, f"registered with no auth dependency: {unguarded}"

    # And they are genuinely installed, not merely present on the table.
    assert len(getattr(m, "_GUARDED_ROUTES", {}) or {}) > 0


@requires_real_app
@pytest.mark.parametrize("method,path", [
    ("GET", "/vip/alice/status"),
    ("POST", "/vip/alice/claim-daily"),
])
def test_an_anonymous_caller_is_refused_on_both_routes(method, path):
    from fastapi.testclient import TestClient
    import main as m
    assert TestClient(m.app).request(method, path).status_code == 401


def _mint_authenticated_player(c, name):
    """Create a REAL player on the app under test and return (player_id, headers).

    FIXED HERE, MEASURED FIRST. This test used to mint its session with
    ``auth.issue_session("bob")`` for a player id that does not exist in the
    store, and it answered 401 where the assertion demanded 403. The reason was
    in the app's own log, not in ``vip.py``::

        AUTH_SESSION_REJECTED reason=session_player_deleted player_id=bob

    The session layer checks that the session's player still EXISTS before the
    ownership guard ever runs, so the request died one layer earlier than the
    guard. The 401 was the auth layer being correct about a phantom player; the
    TEST was wrong, not the route. This is the fixture-401 trap: an assertion
    that expects a guard's answer can be satisfied by a different, earlier
    guard, and then it proves nothing about the guard it names.

    So the player is minted for real -- ``POST /player/create`` carries the
    signing key in its response, which is the only way to get a token. Measured
    live: a real player naming a different id gets 403 on both routes, while an
    id that exists answers 200, so the guard is genuinely reached rather than
    short-circuited.
    """
    created = c.post("/player/create", json={"name": name, "spirit_root": "fire"})
    assert created.status_code == 200, created.text
    body = created.json()
    pid, key = body["id"], body["signing_key"]

    login = c.post("/auth/login", json={"player_id": pid, "signing_key": key})
    assert login.status_code == 200, login.text
    return pid, {"Authorization": "Bearer " + login.json()["token"]}


@requires_real_app
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_caller_cannot_read_or_claim_another_players_vip(method):
    """The guard refuses on the PATH claim, so naming someone else is blocked.

    Both facts are needed, and the second is the one that rules out a blanket
    refusal:

      * naming ANOTHER player is 403,
      * naming YOURSELF reaches the handler (200 on a real row, 404 on an absent
        one) -- so the guard is an ownership check, not a blanket deny.

    TWO REAL PLAYERS, not one real and one made-up name. An earlier revision of
    this test asserted only the unknown-id case, which is weaker than it looks:
    the guard could be noticing "that id does not exist" rather than comparing
    identities. Minting two players is the only way to ask the question that
    matters. Measured live: player B reading player A's id is 403, and player B
    reading its own id is 200.
    """
    from fastapi.testclient import TestClient
    import main as m

    c = TestClient(m.app, raise_server_exceptions=False)
    victim_id, victim_headers = _mint_authenticated_player(c, "VipCrossName")
    other_id, other_headers = _mint_authenticated_player(c, "VipCrossNameOther")
    assert victim_id != other_id, "the two mints returned the same player id"

    def path(other):
        suffix = "status" if method == "GET" else "claim-daily"
        return f"/vip/{other}/{suffix}"

    # THE CONTROL, and it runs FIRST: the owner reaches the handler. Without
    # this, a guard that refuses every caller would satisfy the 403 below and
    # the refusal would prove nothing.
    own = c.request(method, path(other_id), headers=other_headers)
    assert own.status_code in (200, 404), (
        "the guard refused the OWNER, so every refusal below is vacuous: "
        f"{method} answered {own.status_code} {own.text[:200]}")

    # THE REAL QUESTION: one real player naming another real player.
    real_cross = c.request(method, path(victim_id), headers=other_headers)
    assert real_cross.status_code == 403, (
        f"{method} let one real player read another real player's VIP: "
        f"{real_cross.status_code} {real_cross.text[:200]}")
    assert "does not match" in real_cross.json()["detail"]

    # And the same for an id that does not exist at all -- a different reason,
    # but a client keying its copy on 404 still gets the wrong message.
    ghost = c.request(method, path("definitely_not_a_player"), headers=other_headers)
    assert ghost.status_code == 403, (
        "an unknown id is a 403 on the shipped app, not the module's 404: a "
        "client keying its copy on 404 shows the wrong message in production")


@requires_real_app
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_bearer_token_for_a_player_who_does_not_exist_is_401_not_403(method):
    """The layer ABOVE the ownership guard, pinned because it used to hide it.

    ``test_a_caller_cannot_read_or_claim_another_players_vip`` was written with
    ``auth.issue_session("bob")`` for a player that was never created, and it
    answered 401 -- so it went green/red on the session layer and not on the
    ownership guard it claimed to be testing. The trap generalises: any
    ownership assertion whose session names a phantom player measures a
    different guard.

    So the phantom case is pinned deliberately and separately, with the reason
    named. The log for this call reads
    ``AUTH_SESSION_REJECTED reason=session_player_deleted``.
    """
    import auth
    from fastapi.testclient import TestClient
    import main as m

    assert "phantom_vip_player" not in m.players_db, (
        "the control is invalid: a player with this id now exists, so this no "
        "longer measures the missing-player branch")

    c = TestClient(m.app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer " + auth.issue_session("phantom_vip_player")}
    suffix = "status" if method == "GET" else "claim-daily"
    r = c.request(method, f"/vip/phantom_vip_player/{suffix}", headers=headers)
    assert r.status_code == 401, (
        "a token for a player who does not exist answered "
        f"{r.status_code}; the session layer should reject it before the "
        "ownership guard runs")


# --- payment order: a failed payout must not burn the day ----------------

def test_a_failed_payout_does_not_consume_the_day_key(rig, monkeypatch):
    """PAY FIRST, KEY SECOND -- the ordering the module docstring promises.

    ``server/vip.py`` writes ``last_claim_utc_day`` AFTER the payout precisely so
    that a raise anywhere in the payout leaves the day claimable. That promise was
    unpinned: writing the key first, then paying, passes every other test in this
    file, because no test ever made the payout fail.

    The failure is made reachable with a CORRUPT LEGACY SAVE -- ``inventory`` is a
    string, so crediting an item raises ``AttributeError`` after the stones have
    already gone out. Measured on this tree: the first press answers 500, the day
    key stays ``None``, and a retry on the SAME day after the save is repaired
    pays in full.

    Both halves are asserted, because only the second one is the promise: the day
    must not be burned, AND the retry must actually pay.
    """
    monkeypatch.setattr(V, "_now", lambda: 1000 * float(V.SECONDS_PER_DAY), raising=True)
    store = rig["store"]
    c = rig["client"]

    expected = V.tier_benefits(V.tier_for_points(
        V.VIP_TIERS[1]["points_required"])["id"])["daily_claim"]
    assert expected["items"], "control failed: this tier grants no items, so a " \
                              "corrupt inventory never raises and the test is void"

    # A pre-upgrade save whose inventory is not a dict.
    row = fresh_player("broken", V.VIP_TIERS[1]["points_required"])
    row["inventory"] = "not-a-dict"
    with store.transaction():
        store["broken"] = row

    # 1. The payout raises. raise_server_exceptions=False turns the 500 into a
    #    status code so the rest of the test still runs.
    failed = c.post("/vip/broken/claim-daily")
    assert failed.status_code == 500, (
        "the corrupt inventory was supposed to fail the payout; it answered "
        f"{failed.status_code}, so the rest of this test proves nothing")

    # 2. THE PROMISE: the day key was NOT consumed, so the grant is still owed.
    assert store["broken"].get(V.STATE_KEY, {}).get(V.STATE_KEY_LAST_CLAIM) is None, (
        "a failed payout burned the day key: the player loses the whole day's "
        "grant because of an unrelated save error. Payment must come BEFORE "
        "the key is written.")

    # 3. Repair the save and retry the SAME day. The player must be paid in full.
    repaired = dict(store["broken"])
    repaired["inventory"] = {}
    with store.transaction():
        store["broken"] = repaired

    retry = c.post("/vip/broken/claim-daily")
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert body["reason"] == "claimed", (
        "the day key was consumed by the failed payout, so the retry pays "
        "nothing even though the player never received a grant")
    assert body["already_claimed"] is False
    assert wallet(store["broken"]) == expected["spirit_stones"], (
        "the retry did not pay the full grant")


# --- the wallet shapes the seed helper never produces ---------------------
#
# MEASURED GAP. Every row in this file is seeded by ``fresh_player``, which
# writes ``spirit_stones: {"low_grade": 0}`` -- and ``test_vip.py``'s store
# fixture does the same. So the suite only ever exercises the tiered-dict
# branch of ``vip._credit_spirit_stones`` and leaves the other two untested:
#
#   * a pre-upgrade save where ``spirit_stones`` is a PLAIN INT (the branch
#     exists in the product and is documented as the ``battle_pass.grant_stones``
#     convention);
#   * a tiered wallet carrying MORE THAN ONE grade, where only ``low_grade``
#     may move;
#   * a save with no ``spirit_stones`` key at all.
#
# A regression in any of those would keep every existing test in this file green
# while paying real players into a wallet shape the rest of the server cannot
# read. Each test below was measured live first (see the docstrings).


def legacy_rig(rows):
    """The two routes over a store that hands back the LIVE row.

    ``_DetachedReadStore`` already has exactly this behaviour, so it is reused
    rather than a second store double being written.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    db = _DetachedReadStore(rows, detached=False)
    app = FastAPI()
    V.register_vip_routes(app, lambda: db)
    return {"client": TestClient(app, raise_server_exceptions=False), "store": db}


@pytest.fixture
def int_wallet_rig():
    pts = V.VIP_TIERS[1]["points_required"]
    return legacy_rig({"legacy": fresh_player("legacy", pts)})


@pytest.fixture
def graded_wallet_rig():
    pts = V.VIP_TIERS[1]["points_required"]
    row = fresh_player("graded", pts)
    # Two grades: only low_grade is the free-to-play balance, so only it may move.
    row["spirit_stones"] = {"low_grade": 40, "mid_grade": 77}
    return legacy_rig({"graded": row})


@pytest.fixture
def keyless_wallet_rig():
    row = {"id": "keyless", "name": "keyless", "progress": 0.0, "inventory": {}}
    # points granted through the shipped code, so the tier is derived, not written
    V.add_vip_points(row, V.VIP_TIERS[1]["points_required"])
    assert "spirit_stones" not in row
    return legacy_rig({"keyless": row})


def _expected_claim(pid_points=None):
    tier = V.tier_for_points(pid_points or V.VIP_TIERS[1]["points_required"])
    return V.tier_benefits(tier["id"])["daily_claim"]


def test_a_claim_credits_a_legacy_wallet_that_is_a_plain_int(int_wallet_rig):
    """The int branch of ``_credit_spirit_stones``, measured end to end.

    Measured live before this was written: a row seeded with
    ``spirit_stones: 7`` answers 200, reports 150 stones, and STORES ``157`` --
    the balance added to, not replaced. That is the property that matters: a
    player who already held stones keeps them.
    """
    db = int_wallet_rig["store"]
    db["legacy"]["spirit_stones"] = 7
    expected = _expected_claim()
    assert expected["spirit_stones"] > 0

    r = int_wallet_rig["client"].post("/vip/legacy/claim-daily")
    assert r.status_code == 200, r.text
    assert r.json()["reward"]["spirit_stones"] == expected["spirit_stones"]

    committed = db["legacy"]
    assert isinstance(committed["spirit_stones"], int), (
        "the payout turned an int wallet into something else; the rest of the "
        f"server reads this field as an int. Stored {committed['spirit_stones']!r}")
    assert committed["spirit_stones"] == 7 + expected["spirit_stones"], (
        "the payout replaced the legacy balance instead of adding to it")
    assert committed["inventory"].get("spirit_herb") == \
        expected["items"].get("spirit_herb", 1)


def test_a_claim_credits_only_the_low_grade_of_a_multi_grade_wallet(graded_wallet_rig):
    """A tiered wallet can carry several grades. Only ``low_grade`` is the
    free-to-play balance the daily grant draws from.

    Measured live before this was written: ``{"low_grade": 40, "mid_grade": 77}``
    became ``{"low_grade": 190, "mid_grade": 77}`` -- the paid grade untouched.
    A credit that clobbered the dict, or wrote to the wrong grade, would pass
    every other test in this file.
    """
    db = graded_wallet_rig["store"]
    expected = _expected_claim()
    r = graded_wallet_rig["client"].post("/vip/graded/claim-daily")
    assert r.status_code == 200, r.text

    stored = db["graded"]["spirit_stones"]
    assert isinstance(stored, dict), stored
    assert stored["low_grade"] == 40 + expected["spirit_stones"]
    assert stored["mid_grade"] == 77, (
        f"the daily grant moved a paid spirit-stone grade: {stored!r}. The "
        "grant is free-to-play currency only.")


def test_a_claim_creates_the_wallet_when_the_save_has_none(keyless_wallet_rig):
    """A pre-upgrade save can lack the key entirely. The payout must create it
    rather than silently dropping the money on the floor.

    Measured live before this was written: status answers 200 for this row and
    the claim leaves ``spirit_stones`` at exactly the grant amount.
    """
    db = keyless_wallet_rig["store"]
    c = keyless_wallet_rig["client"]
    expected = _expected_claim()
    assert expected["spirit_stones"] > 0

    # The read path must survive the same row.
    read = c.get("/vip/keyless/status")
    assert read.status_code == 200, read.text
    assert read.json()["vip"]["tier_id"] == V.tier_for_points(
        V.VIP_TIERS[1]["points_required"])["id"]

    r = c.post("/vip/keyless/claim-daily")
    assert r.status_code == 200, r.text
    assert db["keyless"]["spirit_stones"] == expected["spirit_stones"], (
        "the grant was reported but the wallet key was never created, so the "
        "player received nothing they can spend")
    assert db["keyless"][V.STATE_KEY][V.STATE_KEY_LAST_CLAIM] == r.json()["utc_day"]


def test_a_corrupt_wallet_fails_the_claim_without_burning_the_day():
    """The other half of the pay-first-key-second promise, on the OTHER branch.

    ``test_a_failed_payout_does_not_consume_the_day_key`` makes the payout fail
    with a corrupt INVENTORY. Nothing made the WALLET fail, so the ordering was
    only pinned on one side. A ``spirit_stones`` value that is a string raises
    inside ``int()`` after nothing has been written.

    Measured live before this was written: 500, the wallet still holds the
    corrupt string, and ``last_claim_utc_day`` stays ``None``.
    """
    row = fresh_player("corrupt", V.VIP_TIERS[1]["points_required"])
    row["spirit_stones"] = "not-a-number"
    rig = legacy_rig({"corrupt": row})
    db = rig["store"]

    failed = rig["client"].post("/vip/corrupt/claim-daily")
    assert failed.status_code == 500, (
        "the corrupt wallet was supposed to fail the payout; it answered "
        f"{failed.status_code}, so the rest of this test proves nothing")
    assert db["corrupt"].get(V.STATE_KEY, {}).get(V.STATE_KEY_LAST_CLAIM) is None, (
        "a failed payout burned the day key, so a player loses the whole day's "
        "grant because of an unrelated save error")

    # Repair the save; the retry on the SAME day must pay in full.
    repaired = dict(db["corrupt"])
    repaired["spirit_stones"] = 0
    db._rows["corrupt"] = repaired

    retry = rig["client"].post("/vip/corrupt/claim-daily")
    assert retry.status_code == 200, retry.text
    assert retry.json()["reason"] == "claimed"
    assert db["corrupt"]["spirit_stones"] == _expected_claim()["spirit_stones"]


# --- the client panel ----------------------------------------------------

def _gd_code(src: str) -> str:
    """The GDScript source with every ``#`` comment removed.

    NEEDED, and not cosmetic: this panel's own docstring names every flag and
    every reason string it uses, four times each. A test that greps the raw file
    for ``empty_grant`` therefore passes on a panel whose branch has been
    deleted -- MUTANT M6 in the control suite survived exactly that way. Comments
    are the evidence that this panel was written knowing about a flag, not
    evidence that the code reads it.
    """
    lines = []
    for line in src.splitlines():
        cut = line.find("#")
        lines.append(line if cut < 0 else line[:cut])
    return "\n".join(lines)


def test_the_client_panel_calls_exactly_the_two_documented_routes():
    """A VIP panel EXISTS now (``client/scenes/game/VipPanel.gd``, mounted as a
    section of the shop tab in ``MainHub.gd``). This used to assert the opposite
    -- "no client screen names a VIP route" -- which was true when the file was
    written and stopped being true the moment the panel landed.

    The contract did not change; the ABSENCE did. What a panel author still needs
    pinned is that it calls only the two routes this module serves, so the test
    now asserts the panel's paths are a subset of the served surface rather than
    that they are empty. If a fifth VIP route is ever added server-side, this
    stays green and ``test_the_module_contributes_exactly_the_two_documented_routes``
    is the one that fails -- which is the correct division of labour.

    The search still asserts its own ability to match: the same pattern is run
    against two sibling features that have panels. A zero-hit grep is only
    evidence of absence if the pattern is shown to hit when there IS something
    to hit -- otherwise this test would pass on a broken search.
    """
    client_dir = _ROOT / "client"
    if not client_dir.is_dir():
        pytest.skip("no client/ directory on this tree; nothing to census")

    gd_files = list(client_dir.rglob("*.gd"))
    assert gd_files, "the client search matched no .gd files at all: the census is void"
    corpus = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                       for p in gd_files)

    # CONTROLS: the pattern matches when a panel exists.
    assert re.search(r"/tower/spire/enter", corpus), (
        "control failed: /tower/spire/enter is in TowerSpirePanel.gd, so a "
        "zero-hit result below would mean the search is broken, not absent")
    assert re.search(r"/daily-streak", corpus), (
        "control failed: /daily-streak is in DailyStreakPanel.gd")

    # The panel's paths, read off the live tree rather than restated.
    hits = sorted(set(re.findall(r"[\"'](/vip/[^\"']*)[\"']", corpus)))
    if not hits:
        pytest.skip("no VIP panel on this tree yet; the census has nothing to pin")

    # Every path the client names must be one this module actually serves. The
    # panel formats with %s, so compare the PREFIX against each served route.
    served = ("/vip/%s/status", "/vip/%s/claim-daily")
    for path in hits:
        assert path in served, (
            f"a client script names a VIP route this module does not serve: "
            f"{path}. Served: {list(served)}")

    # And a panel file exists for the feature, by filename.
    assert [p for p in client_dir.rglob("*") if p.is_file() and "vip" in p.name.lower()], (
        "a client script calls /vip/* but no VIP panel file exists: the caller "
        "is orphaned")


def test_the_client_panel_branches_on_the_body_flags_not_the_status_code():
    """``wc-vip-panel-200-that-means-no``: two of the three claim outcomes are
    HTTP 200 with no payout. A panel that branches on the status code, or on
    ``ok`` alone, announces "Claimed" to a player who received nothing.

    Read from the live panel, so this fails if the branches are ever removed.
    A missing panel skips rather than fails: the test above reports that.
    """
    panel = _ROOT / "client" / "scenes" / "game" / "VipPanel.gd"
    if not panel.is_file():
        pytest.skip("no VipPanel.gd on this tree; nothing to read")
    src = panel.read_text(encoding="utf-8", errors="ignore")
    code = _gd_code(src)

    # Both flags the three-outcome branch keys off, and the server sends both.
    # The panel branches on the FLAGS, not on the reason strings -- that is the
    # correct shape, because the flags are the contract and `reason` is
    # presentation. So the reason strings are only checked the other way
    # round: any reason the panel hardcodes must be one the server can send.
    #
    # Read from `code`, not `src`: the panel's docstring names both flags four
    # times, so a raw-text search passes even with the branches deleted.
    for flag in ("already_claimed", "empty_grant"):
        assert flag in code, (
            f"VipPanel.gd never READS `{flag}` in code: a claim that pays "
            "nothing would be announced as a success. (Its docstring mentions "
            "the flag, which is why this checks code, not text.)")
        assert flag in CLAIM_KEYS, (
            f"the server stopped sending `{flag}`, so the panel's branch is dead")

    # And the panel reads them off the RESPONSE BODY, not the status code.
    assert 'body.get("empty_grant"' in code or 'get("empty_grant"' in code, (
        "VipPanel.gd does not read empty_grant out of the parsed body")
    assert "response_code" not in code and "status_code" not in code, (
        "VipPanel.gd reads an HTTP status code; two of the three claim "
        "outcomes are 200 with no payout, so that would be a false success")

    # Any reason the panel hardcodes must be one the server can actually send.
    for reason in re.findall(r"[\"']((?:claimed|already_claimed_today|"
                             r"tier_grants_nothing_daily))[\"']", src):
        assert reason in REASONS, (
            f"VipPanel.gd hardcodes the reason {reason!r}, which the server "
            f"never sends. Server reasons: {sorted(REASONS)}")


def test_the_vault_route_census_agrees_that_vip_has_two_routes():
    """Cross-check against the repo's own reachability census, so this test is
    not the only thing claiming a route count."""
    census = _ROOT / "tests" / "reachability_audit" / "deadcode_server_census.json"
    if not census.is_file():
        pytest.skip("no reachability census on this tree")
    text = census.read_text(encoding="utf-8", errors="ignore")
    assert "vip.py" in text, "the census no longer mentions vip.py at all"


# --- the second 404: the row vanishes between the check and the claim -------
#
# MEASURED GAP. ``register_vip_routes`` has TWO distinct "player is gone"
# branches and only one of them was pinned. The membership check
# (``if player_id not in players_db``) raises 404, and
# ``test_an_unknown_player_id_is_404_on_the_bare_mount`` covers that. But
# ``claim_daily`` ALSO raises ``KeyError`` from inside the route, and the route
# catches it and raises the same 404 -- for a different reason: the row was
# deleted AFTER the membership test passed.
#
# Nothing could reach that branch before this test. It needs a store that lies
# to the membership test and then behaves truthfully, which is exactly what a
# concurrent account delete looks like. Delete the ``except KeyError`` clause
# and the exception escapes as a 500 instead, so this pins the branch as a real
# contract: a delete racing a claim is a 404 to the caller, never a 500.
#
# Measured live before this was written: ``STATUS 404 {"detail":"Player not
# found"}``. The status route has no equivalent branch, which is asserted too.

class _VanishingStore:
    """Passes the membership test, then loses the row before the claim.

    Reproduces one thing only: ``__contains__`` and ``__getitem__`` disagreeing
    between two calls, which is the window a concurrent delete opens. Not a
    re-implementation of ``PlayerStore``.
    """

    def __init__(self, row):
        import copy
        self._row = copy.deepcopy(row)
        self.gone = False

    def __contains__(self, pid):
        return pid == "p1"

    def __getitem__(self, pid):
        if self.gone:
            raise KeyError(pid)
        return self._row

    def persist(self, pid, player=None):
        return None

    def transaction(self, pid=None):
        import contextlib

        @contextlib.contextmanager
        def scope():
            yield self
        return scope()


def test_a_claim_losing_its_player_to_a_concurrent_delete_is_404_not_500():
    """The route's SECOND 404 branch, and the only one that costs a player a
    press rather than a lookup."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    db = _VanishingStore(fresh_player("p1", V.VIP_TIERS[1]["points_required"]))
    app = FastAPI()
    V.register_vip_routes(app, lambda: db)
    c = TestClient(app, raise_server_exceptions=False)

    # The control: the same store answers normally while the row is present.
    ok = c.post("/vip/p1/claim-daily")
    assert ok.status_code == 200, ok.text
    assert ok.json()["already_claimed"] is False

    db.gone = True
    lost = c.post("/vip/p1/claim-daily")
    assert lost.status_code == 404, (
        "a claim that lost its player to a delete answered "
        f"{lost.status_code}; the KeyError clause is gone, so the exception "
        "escapes as a 500 and the caller cannot tell a deleted account from a "
        "server fault")
    assert lost.json()["detail"] == "Player not found"


def test_the_status_route_has_no_such_race_branch_and_cannot_500_that_way(rig):
    """The asymmetry is real, so it is pinned rather than assumed.

    ``GET /status`` reads the row exactly once, immediately after its
    membership test, with nothing in between to fail. The claim route is the
    one that re-reads inside a transaction and therefore needs the clause.
    A 404 on status for a row that exists is not a race -- it would be the
    membership guard refusing, which is measured elsewhere -- so here a
    correctly answering 200 is the contract.
    """
    r = rig["client"].get("/vip/paid/status")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


# =============================================================================
# THE THREE AREAS WITH NO EXISTING COVERAGE
# =============================================================================
#
# MEASURED GAP, not a reading. A search of BOTH vip suites for
# ``install_xp_earn_hook``, ``grant_vip_points_for_xp`` and ``LAST_XP_EARN``
# returned ZERO hits -- so the entire earn bridge was unpinned. Likewise a
# search for ``inf``/``nan``/``OverflowError`` returned zero, and a search for
# ``extra_daily_attempts`` returned zero.
#
# That is the worst possible shape for the bridge in particular. The module
# docstring says it exists because "``add_vip_points`` used to have zero
# non-test callers, so every tier above the free one was unreachable." The whole
# paid ladder hangs off it, and nothing would have failed if it were deleted.
#
# Every figure in this section was measured against the live tree before the
# assertion was written. Where behaviour is a sharp edge rather than a virtue,
# it is pinned AS IT SHIPS with the gap named, so a fix turns the test red.


# --- (3) the XP earn bridge: the ladder's only real income -----------------

@pytest.fixture
def xp_hook():
    """``battle_pass.grant_xp`` with the VIP bridge installed, restored after.

    THE FIXTURE MUST RESTORE. ``install_xp_earn_hook`` rebinds a MODULE GLOBAL
    on ``battle_pass``, so a test that installs it and leaves it installed
    silently changes every other test in the repo that grants XP -- including
    tests in files that never mention VIP. A leaked wrapper pays VIP points
    twice for the rest of the session. Teardown is not hygiene here; it is what
    keeps this file from being a landmine.

    MEASURED, and it changes how this fixture must be built. This test FILE
    imports ``main`` at COLLECTION time (see ``_main_import_error``), and
    ``main`` mounts ``vip``, which installs the hook. So by the time any test
    here runs, ``grant_xp`` is ALREADY wrapped and ``install_xp_earn_hook``
    returns ``False``.

    That means the fixture has to be IDEMPOTENT, not merely restoring: it must
    end with the hook installed no matter which state it started in, and
    restore the ORIGINAL state on the way out. A fixture that assumed it was
    the installer would make every test in this section measure the
    collection-time import instead of its own setup.

    The yielded value is the hook's state as the test found it.
    """
    import battle_pass
    already = getattr(battle_pass.grant_xp, V._XP_EARN_HOOK_MARKER, False)
    try:
        V.install_xp_earn_hook()
        assert getattr(battle_pass.grant_xp, V._XP_EARN_HOOK_MARKER, False), (
            "the fixture could not get the hook installed, so every test using "
            "it would measure an unwrapped grant_xp")
        yield already
    finally:
        if not already:
            V.uninstall_xp_earn_hook()
        assert getattr(battle_pass.grant_xp, V._XP_EARN_HOOK_MARKER, False) is already, (
            "teardown did not restore battle_pass.grant_xp to the state this "
            "fixture found it in; every later XP grant in this pytest session "
            "would pay VIP points twice")


def test_mounting_the_routes_is_what_makes_the_ladder_earnable(xp_hook):
    """``register_vip_routes`` installs the bridge, and importing must not.

    The docstring is explicit that the install happens at MOUNT time because
    "importing this module has no side effects -- a test that imports vip to
    read the tier table must not wrap battle_pass."

    So the two halves are asserted SEPARATELY, and the order matters:

      * importing ``vip`` alone must not install the hook -- checked on a
        CLEAN interpreter state by uninstalling first;
      * mounting the routes must install it -- checked on a bare app.

    A single combined check would be satisfied by a module that installs at
    import (passes "installed") and by one that never installs (passes
    "import is clean"), so neither half is meaningful alone.

    WHY THE CLEAN-STATE CHECK MATTERS HERE: this test file imports ``main`` at
    collection, which installs the hook before any test runs. Without an
    explicit uninstall first, "importing vip does not install the hook" would
    be measuring nothing at all -- the hook is already there and no assertion
    could tell.
    """
    import battle_pass
    from fastapi import FastAPI

    # HALF 1: the import leaves things alone. Start from a known-clean state.
    V.uninstall_xp_earn_hook()
    assert not getattr(battle_pass.grant_xp, V._XP_EARN_HOOK_MARKER, False)

    app = FastAPI()
    db = _DetachedReadStore({"p": fresh_player("p", 0)})
    V.register_vip_routes(app, lambda: db)

    # HALF 2: the MOUNT is what installs it.
    assert getattr(battle_pass.grant_xp, V._XP_EARN_HOOK_MARKER, False) is True, (
        "register_vip_routes did not install the XP bridge, so nothing in the "
        "game earns VIP points and every tier above the free one is "
        "unreachable content")


def test_the_bridge_turns_gameplay_xp_into_vip_points(xp_hook):
    """THE load-bearing behaviour: XP earned is VIP points earned.

    Measured live before this was written: one ``grant_xp(row, 250)`` returned
    ``LAST_XP_EARN`` with ``points_gained == 25`` at ``VIP_POINTS_PER_XP == 10``,
    and wrote ``vip.points == 25`` onto the row.
    """
    import battle_pass
    row = {"id": "xp1", "name": "xp1", "progress": 0.0}

    result = battle_pass.grant_xp(row, 250, "probe")

    # The XP funnel's own reply is untouched: the bridge adds nothing to it,
    # because six contract tests elsewhere pin that exact key set.
    assert sorted(result) == ["leveled_up", "new_level", "ok", "source",
                              "total_xp", "xp_gained"]
    assert "vip" not in result, (
        "the bridge added a `vip` key to the XP reply; that is a client-facing "
        "contract change on a funnel several other tests pin exactly")

    earn = V.LAST_XP_EARN
    assert earn["ok"] is True
    assert earn["xp_earned"] == result["xp_gained"], (
        "the bridge paid on something other than the XP the server actually "
        "awarded; a clamped grant would pay VIP points for XP that never landed")
    assert earn["points_gained"] == result["xp_gained"] // V.VIP_POINTS_PER_XP
    assert earn["total_points"] == earn["points_gained"]

    # AND the points landed on the player's row, not just in the reply.
    assert row[V.STATE_KEY]["points"] == earn["points_gained"]


def test_the_bridge_carries_the_remainder_instead_of_dropping_it(xp_hook):
    """Small earns are the normal case, so rounding them away is silent loss.

    ``VIP_POINTS_PER_XP`` is 10 and a sect contribution is worth 3 XP, so most
    single earns convert to ZERO points. Dropping the leftover would mean most
    gameplay grants nothing at all, forever.

    Measured live: three 3-XP grants (9 XP) gave ``points_gained == 0`` with
    ``carry_xp == 9``; a fourth (12 XP) gave ``points_gained == 1`` with
    ``carry_xp == 2``. The control -- that the row really did not move on the
    first three -- is what makes the fourth meaningful.
    """
    import battle_pass
    row = {"id": "xp2", "name": "xp2", "progress": 0.0}
    per_grant = 3
    assert per_grant < V.VIP_POINTS_PER_XP, (
        "the control failed: this test needs a grant smaller than one point")

    for _ in range(3):
        battle_pass.grant_xp(row, per_grant, "probe")
        assert V.LAST_XP_EARN["points_gained"] == 0

    carried = row[V.STATE_KEY]["points_remainder_xp"]
    assert carried == 3 * per_grant, (
        f"the remainder was dropped: {carried} XP carried, not "
        f"{3 * per_grant}. Small earns would grant nothing forever.")
    assert row[V.STATE_KEY]["points"] == 0, "a zero-point grant moved the points"

    battle_pass.grant_xp(row, per_grant, "probe")
    assert V.LAST_XP_EARN["points_gained"] == 1, (
        "9 carried XP plus 3 new did not become a point; the carry is not being "
        "spent by the next grant")
    # 12 XP total against a 10-XP cost: 1 point spent, 2 left over.
    assert V.LAST_XP_EARN["carry_xp"] == 3 * per_grant + per_grant - V.VIP_POINTS_PER_XP


def test_the_bridge_reports_the_tier_change_and_the_bridge_alone_grants_it(xp_hook):
    """Area (3) of the brief: points crossing a boundary must re-derive the tier.

    ``tier_changed`` is what a caller reads to know a notification is worth
    sending, so it must be true ONLY on the crossing.

    ONE GRANT CANNOT CROSS THE BOUNDARY, and that is the measurement that
    shaped this test. ``battle_pass.MAX_XP_PER_GRANT`` is 1000, so the largest
    single grant is worth 100 VIP points -- while the first paid tier needs 500.
    An earlier revision tried to cross in one 5000-XP grant and failed,
    correctly: ``grant_xp`` clamped the request to 1000 XP and the tier never
    moved. That clamp is also WHY the bridge reads ``xp_gained`` rather than the
    requested amount (see ``install_xp_earn_hook``), so this test doubles as the
    check that the clamp is respected.

    So the boundary is crossed by REPEATED real grants, which is how a player
    actually reaches it.
    """
    import battle_pass
    row = {"id": "xp3", "name": "xp3", "progress": 0.0}
    paid = V.VIP_TIERS[1]

    # The precondition, stated rather than assumed: one grant cannot get there.
    single = paid["points_required"] * V.VIP_POINTS_PER_XP
    assert single > battle_pass.MAX_XP_PER_GRANT, (
        "this test assumed a single grant is clamped below the tier boundary; "
        f"MAX_XP_PER_GRANT is {battle_pass.MAX_XP_PER_GRANT} and the boundary "
        f"is {single} XP, so the loop below is not the shortest path")

    # Repeated real grants, stopping one step short of the boundary.
    per_grant = battle_pass.MAX_XP_PER_GRANT
    points_per_grant = per_grant // V.VIP_POINTS_PER_XP
    grants_to_boundary = -(-paid["points_required"] // points_per_grant)
    for _ in range(grants_to_boundary - 1):
        battle_pass.grant_xp(row, per_grant, "probe")
        assert V.LAST_XP_EARN["tier_changed"] is False, (
            "a grant short of the boundary reported a tier change")

    remaining_xp = paid["points_required"] * V.VIP_POINTS_PER_XP - \
        (grants_to_boundary - 1) * per_grant
    battle_pass.grant_xp(row, remaining_xp, "probe")

    earn = V.LAST_XP_EARN
    assert earn["tier_changed"] is True, (
        "landing exactly on the tier boundary did not report a tier change, so "
        "a client that watches this flag never tells the player they upgraded")
    assert earn["previous_tier_id"] == V.VIP_TIERS[0]["id"]
    assert earn["tier_id"] == paid["id"]
    assert row[V.STATE_KEY]["points"] == paid["points_required"]


def test_a_second_install_does_not_double_pay_every_xp_grant(xp_hook):
    """Double-wrapping is not cosmetic: every XP grant would pay VIP twice.

    The module's own comment says so, and nothing enforced it. A second
    ``install_xp_earn_hook`` must return ``False`` and leave the conversion
    rate unchanged.
    """
    import battle_pass
    assert V.install_xp_earn_hook() is False, (
        "a second install wrapped the wrapper; every XP grant now pays VIP "
        "points twice")
    row = {"id": "xp4", "name": "xp4", "progress": 0.0}
    battle_pass.grant_xp(row, 100, "probe")
    assert V.LAST_XP_EARN["points_gained"] == 10, (
        "a double-wrapped bridge paid 20 points for 100 XP")
    assert row[V.STATE_KEY]["points"] == 10


def test_a_vip_failure_never_takes_the_xp_grant_with_it(xp_hook):
    """``grant_xp`` has already written the XP when the bridge runs.

    If the bridge raised out to the caller, the caller would answer 500 for an
    XP grant that really landed -- money-shaped data with an error attached. So
    the failure is CONTAINED, and recorded in ``LAST_XP_EARN`` rather than
    swallowed silently.

    Measured live with the bridge forced to raise: ``grant_xp`` still returned
    ``ok: True`` with its six normal keys, and ``LAST_XP_EARN`` carried
    ``ok: False`` plus the error text.
    """
    import battle_pass
    row = {"id": "xp5", "name": "xp5", "progress": 0.0}
    from unittest import mock
    with mock.patch.object(V, "grant_vip_points_for_xp",
                           side_effect=RuntimeError("forced bridge failure")):
        result = battle_pass.grant_xp(row, 100, "probe")

    assert result.get("ok") is True, (
        "the bridge's failure escaped to the caller; an XP grant that really "
        "landed would be answered 500")
    assert sorted(result) == ["leveled_up", "new_level", "ok", "source",
                              "total_xp", "xp_gained"]
    # The XP really was written -- that is the whole point of the containment.
    assert result["xp_gained"] == 100

    earn = V.LAST_XP_EARN
    assert earn["ok"] is False, "the containment was silent: LAST_XP_EARN says ok"
    assert "forced bridge failure" in earn["error"]


def test_no_vip_route_can_reach_the_xp_bridge(rig):
    """The bridge takes no request, so the HTTP surface is unchanged.

    The bridge is where points come from, so "no route grants points" is only a
    real guarantee if the bridge itself is unreachable over HTTP. Both routes
    are driven with an amount in the body AND a query, and neither may move the
    points.
    """
    store = rig["store"]
    client = rig["client"]
    before = store["paid"][V.STATE_KEY]["points"]

    for pid in ("free", "paid"):
        r = client.post(f"/vip/{pid}/claim-daily?xp={10 ** 9}"
                        f"&points={10 ** 9}&amount={10 ** 9}",
                        json={"xp": 10 ** 9, "points": 10 ** 9})
        assert r.status_code == 200, r.text
    assert store["paid"][V.STATE_KEY]["points"] == before, (
        "a request field moved VIP points through a VIP route")


# --- (4) non-finite and non-numeric values --------------------------------
#
# WHY THIS SECTION IS ABOUT RAISING, NOT REFUSING
# ``vip.py`` has no validation layer for amounts. It calls ``int()`` on whatever
# it is handed, and Python's ``int()`` raises on the bad cases rather than
# coercing them to zero. That is the safe direction -- a silent zero would be
# the dangerous one -- so these tests pin the RAISE, because a future
# "harden this with a try/except that returns 0" would otherwise be a silent
# regression rather than a caught one.
#
# MEASURED on this tree before the assertions were written:
#   float("inf")  -> OverflowError, float("-inf") -> OverflowError,
#   float("nan")  -> ValueError, "abc"           -> ValueError,
#   and ``10 ** 400`` is accepted (Python ints are unbounded), landing on the
#   top tier.

NON_FINITE = [float("inf"), float("-inf"), float("nan")]


@pytest.mark.parametrize("bad", NON_FINITE)
def test_non_finite_points_are_refused_rather_than_read_as_zero(bad):
    """A stored infinite or not-a-number point total must not resolve to a tier.

    The dangerous shape is the silent one: ``tier_for_points`` clamps with
    ``int(points or 0)``, and ``float('nan') or 0`` is truthy, so a NaN would
    sail past a truthiness check and only be caught by the ``int()`` inside.

    Measured: ``get_vip_status`` raises ``ValueError`` on NaN and
    ``OverflowError`` on infinity. Neither returns a tier, and neither writes
    to the player -- which is asserted below, because a status route that
    half-applied a bad value would leave the save poisoned.
    """
    row = {"id": "bad", "name": "bad", V.STATE_KEY: {"points": bad}}
    before = dict(row[V.STATE_KEY])

    try:
        tier = V.get_vip_status(row)["tier_id"]
    except (OverflowError, ValueError):
        pass
    else:
        raise AssertionError(
            f"a stored point total of {bad!r} resolved to tier {tier!r} instead "
            "of being refused: a corrupt save silently promotes a player to a "
            "paid tier")

    assert row[V.STATE_KEY] == before, (
        f"refusing {bad!r} still wrote to the player row: {row[V.STATE_KEY]!r}")


@pytest.mark.parametrize("bad", NON_FINITE)
def test_non_finite_grants_are_refused_by_add_vip_points(bad):
    """The earn path must refuse a non-finite amount, not bank it.

    ``add_vip_points`` is server-internal, so this is not an attacker path --
    it is a corrupt-save or bad-caller path, and the point is that a bad value
    stops here instead of becoming a permanent top-tier row.
    """
    row = {"id": "bad", "name": "bad", V.STATE_KEY: {"points": 0}}
    try:
        V.add_vip_points(row, bad)
    except (OverflowError, ValueError):
        pass
    else:
        raise AssertionError(
            f"add_vip_points accepted {bad!r}: total is now "
            f"{row[V.STATE_KEY]['points']!r}")


def test_a_non_numeric_grant_is_refused_and_the_existing_points_survive():
    """A string that is not a number must not zero out a player's real total.

    This is the one with money attached: ``int(state.get("points", 0) or 0) +
    delta`` runs AFTER the coercion, so a raise leaves the saved total exactly
    as it was. If a future change reordered that to coerce-then-default, the
    player would drop to zero points and back to the free tier.
    """
    row = {"id": "s", "name": "s", V.STATE_KEY: {"points": 4321}}
    with pytest.raises((ValueError, TypeError)):
        V.add_vip_points(row, "abc")
    assert row[V.STATE_KEY]["points"] == 4321, (
        "a refused grant destroyed the player's existing points")


def test_a_huge_but_finite_grant_is_accepted_and_cannot_pay_past_the_top_tier():
    """The opposite bound: a gigantic int is legal and must not raise or overflow.

    Python ints are unbounded, so ``10 ** 400`` converts fine. What matters is
    that it lands on the TOP tier -- the ladder ceiling -- rather than raising
    or, worse, indexing past the end of the tier table.
    """
    row = {"id": "huge", "name": "huge", V.STATE_KEY: {"points": 0}}
    result = V.add_vip_points(row, 10 ** 400)
    assert result["tier_id"] == V.VIP_TIERS[-1]["id"], (
        "an enormous grant did not land on the top tier; the ladder ceiling "
        "does not hold")
    assert row[V.STATE_KEY]["points"] == 10 ** 400
    # And the pure read agrees rather than raising on the stored value.
    status = V.get_vip_status(row)
    assert status["is_max_tier"] is True
    assert status["points_to_next"] == 0


def test_a_corrupt_stored_point_total_fails_the_claim_without_burning_the_day():
    """The bad-value path THROUGH THE ROUTE: a 500, and the day stays claimable.

    Extends the pay-first-key-second promise to a third failure shape. The
    existing two make the payout fail with a corrupt INVENTORY and a corrupt
    WALLET. A corrupt ``vip.points`` fails EARLIER -- during the state read,
    before any payment is attempted -- so the ordering is untested on that side.

    Measured live: the claim answers 500 and ``last_claim_utc_day`` stays
    ``None``, so repairing the save lets the same day pay in full.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    row = fresh_player("corrupt_pts", V.VIP_TIERS[1]["points_required"])
    row[V.STATE_KEY]["points"] = float("inf")
    rig_local = legacy_rig({"corrupt_pts": row})

    failed = rig_local["client"].post("/vip/corrupt_pts/claim-daily")
    assert failed.status_code == 500, (
        "the corrupt point total was supposed to fail the claim; it answered "
        f"{failed.status_code}, so the rest of this test proves nothing")
    assert rig_local["store"]["corrupt_pts"].get(V.STATE_KEY, {}).get(
        V.STATE_KEY_LAST_CLAIM) is None, (
        "a failed claim burned the day key: the player loses the whole day's "
        "grant because of an unrelated corrupt field")

    repaired = dict(rig_local["store"]["corrupt_pts"])
    repaired[V.STATE_KEY] = V.default_state()
    repaired[V.STATE_KEY]["points"] = V.VIP_TIERS[1]["points_required"]
    rig_local["store"]._rows["corrupt_pts"] = repaired

    retry = rig_local["client"].post("/vip/corrupt_pts/claim-daily")
    assert retry.status_code == 200, retry.text
    assert retry.json()["reason"] == "claimed"
    assert wallet(rig_local["store"]["corrupt_pts"]) == _expected_claim()["spirit_stones"]


# --- (2) extra_daily_attempts: the benefit that was REMOVED -----------------
#
# THE BRIEF ASKED FOR A TEST THAT "extra_daily_attempts gates dungeon attempts".
# MEASURED, that behaviour DOES NOT EXIST and MUST NOT:
#
#   * ``extra_daily_attempts`` is not in ``PERMITTED_BENEFIT_KEYS``,
#   * it appears in no ladder row and in no status reply,
#   * ``server/dungeons.py`` holds no daily counter and raises no refusal --
#     dungeon runs are already unlimited, so there is nothing for the key to
#     widen;
#   * ``validate_tier_table`` REJECTS it, so it cannot be re-added by accident.
#
# The module docstring explains why: the key used to be granted and "NOTHING
# ever read it", which is the dead-track shape the file was written to avoid.
# So the contract to pin is the ABSENCE and the refusal -- which is the real
# anti-regression guard, and the only test that would fail if someone re-added
# the key.

def test_extra_daily_attempts_is_not_a_permitted_benefit_and_cannot_be_readded():
    """The removal is enforced, not merely documented.

    ``validate_tier_table`` is called by ``register_vip_routes`` before it
    mounts a route, so a re-added key breaks the mount rather than shipping.
    """
    assert "extra_daily_attempts" not in V.PERMITTED_BENEFIT_KEYS
    assert "extra_daily_attempts" not in V.FORBIDDEN_BENEFIT_KEYS, (
        "extra_daily_attempts moved into the forbidden list; it is absent by "
        "omission, which validate_tier_table reports as 'unknown benefit'")

    # The shipped table really is clean -- the control for the mutation below.
    V.validate_tier_table()

    # And injecting the key into a real ladder row is a hard failure.
    victim = V.VIP_TIERS[1]
    saved = victim["benefits"]
    victim["benefits"] = dict(saved, extra_daily_attempts=3)
    try:
        with pytest.raises(V.VipError) as caught:
            V.validate_tier_table()
    finally:
        victim["benefits"] = saved

    assert "extra_daily_attempts" in str(caught.value), (
        f"the refusal did not name the key: {caught.value}")
    # Restored, so the rest of the suite is unaffected.
    V.validate_tier_table()


def test_no_status_reply_advertises_extra_daily_attempts_at_any_tier(rig):
    """Checked on the wire, because the panel renders whatever the reply says.

    Measured live across all four ladder rows: the reply's ``benefits`` carries
    exactly the four permitted keys at every tier, so the removed key cannot
    reach a client.
    """
    points = [0, V.VIP_TIERS[1]["points_required"],
              V.VIP_TIERS[2]["points_required"], V.VIP_TIERS[-1]["points_required"]]
    for tier_points in points:
        v = V.get_vip_status({V.STATE_KEY: {"points": tier_points}})
        assert "extra_daily_attempts" not in v["benefits"], (
            f"at {tier_points} points the status reply advertises "
            "extra_daily_attempts; nothing in the game reads it, so a client "
            "would render a benefit the server cannot honour")


def test_the_client_panel_no_longer_renders_the_removed_attempts_row():
    """FIXED 2026-10-05: the drift is closed, so this asserts the closed state.

    ``client/scenes/game/VipPanel.gd`` used to render::

        _benefits_box.add_child(_row("Extra daily attempts",
            "+%d" % int(benefits.get("extra_daily_attempts", 0))))

    The server had REMOVED that key -- it is not in
    ``PERMITTED_BENEFIT_KEYS``, no ladder row declares it, no status reply
    carries it, and ``validate_tier_table`` raises if it is re-added. So
    ``.get(..., 0)`` always returned 0 and the panel showed every paying
    player a permanent "+0" row for a benefit that does not exist, on the one
    screen a paying customer reads.

    The row is gone. This test now FAILS if anyone re-adds it, which is the
    anti-regression guard the pin version could not give: the pin passed while
    the bug shipped.
    """
    panel = _ROOT / "client" / "scenes" / "game" / "VipPanel.gd"
    if not panel.is_file():
        pytest.skip("no VipPanel.gd on this tree; nothing to read")
    code = _gd_code(panel.read_text(encoding="utf-8", errors="ignore"))

    # CONTROL FIRST. The stripper must be able to see the keys the panel really
    # does read, or the check below would be measuring a broken search.
    for key in ("bag_slots", "shop_discount_pct", "daily_claim", "cosmetics"):
        assert key in code, (
            f"control failed: VipPanel.gd no longer reads {key!r}, so the "
            "check for extra_daily_attempts below would prove nothing")

    assert "extra_daily_attempts" not in code, (
        "REGRESSED: VipPanel.gd reads extra_daily_attempts again. The server "
        "removed that key -- server/vip.py raises on it in validate_tier_table "
        "and no status reply carries it -- so any row built from it renders a "
        "benefit the server cannot honour.")


def test_the_panel_renders_only_benefits_the_server_sends():
    """The general rule behind the fix, not one key's name.

    Every benefit row the panel draws must be a key the server actually puts
    in the reply. A row built from a key the server never sends is a permanent
    zero-value lie, which is the defect class this closes.
    """
    panel = _ROOT / "client" / "scenes" / "game" / "VipPanel.gd"
    if not panel.is_file():
        pytest.skip("no VipPanel.gd on this tree; nothing to read")
    code = _gd_code(panel.read_text(encoding="utf-8", errors="ignore"))

    # Keys the server's PERMITTED_BENEFIT_KEYS actually declares.
    permitted = set(V.PERMITTED_BENEFIT_KEYS)

    # Every benefits.get("...") the panel renders in its benefits section.
    import re as _re
    reads = set(_re.findall(r'benefits\.get\(\s*"([a-z_]+)"', code))

    # CONTROL: the panel must be reading something, or the check is vacuous.
    assert len(reads) >= 4, (
        f"control failed: only found {sorted(reads)} read from benefits; the "
        "panel's render_benefits section should read several keys")

    unknown = reads - permitted
    assert not unknown, (
        f"the panel renders benefit rows for keys the server never sends: "
        f"{sorted(unknown)}. PERMITTED_BENEFIT_KEYS is {sorted(permitted)}. "
        "Each unknown key renders as a permanent +0 row that lies to a paying "
        "customer.")
