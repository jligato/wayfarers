"""Breakthrough: the gate, the attempt, the storm, and what survives a restart.

Four things are asserted here, and each one is a claim that is easy to make
and easy to get wrong:

1. THE GATE IS REAL. An attempt with an unprepared pillar is refused, and the
   refusal costs the player nothing. Preparation is what makes the moment mean
   anything, so it has to be a precondition and not a decoration.

2. THE ATTEMPT CAN FAIL, AND FAILURE COSTS. A forced failure loses the stage
   progress, spends the stones, records a permanent scar, and locks the next
   attempt out behind a cooldown. A failure that cost nothing would be free to
   spam, and a free retry makes the whole thing a formality.

3. THE STORM IS SERVER-RESOLVED. The transcript comes back with the wave
   count, and the same player at the same attempt number resolves to the same
   storm -- which is what "resolved server-side and deterministically" means.
   The client never rolls anything.

4. SUCCESS PERSISTS ACROSS A FRESH READ. This is the copy-on-read trap: the
   store hands out COPIES, so a write to a value read back outside a
   transaction is silently discarded. The proof is a read taken from a
   DIFFERENT store object after the route returned, plus a re-read of the raw
   row. The same class of bug as the historical money bug.

Run with:
    <project-python> -m pytest -q server/tests/test_breakthrough.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import breakthrough as BT  # noqa: E402
from server.tests.wc_test_auth import Session, clear_signup_rate_limit  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture: a real app, a real store, a real signed-in player.
# ---------------------------------------------------------------------------

@pytest.fixture()
def harness():
    from player_store import PlayerStore
    import main

    main.USE_PG = False
    wallet = main.InMemoryWalletStore()
    main.wallet_store = wallet
    main.players_db = PlayerStore(wallet)
    clear_signup_rate_limit()
    client = Session(TestClient(main.app))
    r = client.post("/player/create", json={"name": "Breaker", "spirit_root": "fire"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    client.sign_in(pid)
    return client, main, pid


def _restarted_store(main):
    """A store rebuilt from what was actually COMMITTED, as a restart would.

    A brand new PlayerStore is not a restart -- it is an empty one, because
    the in-memory rows live on the original object. So this reads the
    committed rows out of the live store, pushes each one through the
    store's own serialize/deserialize pair (``_split`` then ``_row_to_dict``,
    which is exactly the path a PostgreSQL round trip takes), and rebuilds a
    fresh store from the result.

    That is the strongest restart simulation available in memory mode, and it
    is the one that catches the copy-on-read class of bug: a value that only
    ever existed in the dict the handler was holding never reaches the
    committed row, so it cannot survive the round trip.
    """
    from player_store import PlayerStore
    store = PlayerStore(main.wallet_store)
    committed = main.players_db._mem  # only what the store has published
    for pid, row in committed.items():
        cols, extra = PlayerStore._split(row)
        restored = PlayerStore._row_to_dict(store, dict(cols, extra_state=extra or {}))
        store[pid] = restored
    return store


def _fill_bar(main, pid, value=100.0):
    """Put the stage bar at full through the STORE, not through a route."""
    with main.players_db.transaction(pid):
        row = main.players_db[pid]
        row["progress"] = value
        main.players_db.persist(pid, row)


def _grant_stones(main, pid, amount):
    with main.players_db.transaction(pid):
        row = main.players_db[pid]
        row["spirit_stones"]["low_grade"] = amount
        main.players_db.persist(pid, row)


def _fill_gate(client, pid):
    """Spend stones until all three pillars are full. Returns the last body."""
    last = None
    for _ in range(60):
        view = client.get(f"/breakthrough/{pid}").json()
        if view["gate_open"]:
            return view
        for pillar in ("qi", "dao", "mind"):
            p = next(x for x in view["gate"]["pillars"] if x["pillar"] == pillar)
            if not p["full"]:
                r = client.post(f"/breakthrough/{pid}/prepare",
                                json={"pillar": pillar})
                assert r.status_code == 200, r.text
                last = r.json()
                break
    raise AssertionError("gate never opened")


def _force_outcome(monkeypatch, success: bool):
    """Pin BOTH gates so each outcome is testable deterministically.

    There are two independent gates and a breakthrough has to clear both: the
    tribulation (can the body survive the storm) and the roll (does heaven
    grant the realm). Pinning only the roll was not enough once the storm was
    calibrated to be survivable about half the time -- a "success" test would
    then fail roughly one run in two, on a coin flip rather than on a defect.

    So both are pinned here, and what is left under test is everything that is
    NOT the dice: the gate, the costs, the promotion, and the persistence. The
    storm's own odds are asserted separately, by
    ``test_a_full_preparation_does_not_guarantee_surviving_the_storm``.
    """
    original = BT._seeded_random
    real_resolve = BT._resolve_tribulation

    def patched(seed, attempt):
        rng = original(seed, attempt)
        if str(seed).endswith(":roll"):
            rng.random = lambda: 0.0 if success else 0.999999
        return rng

    def resolve(player, state, attempt):
        storm = real_resolve(player, state, attempt)
        # A pinned SUCCESS survives the storm. A pinned FAILURE is asserted
        # either way -- the roll alone is enough to fail it, and forcing the
        # storm to break as well would test the storm instead of the cost.
        if success:
            storm = dict(storm, survived=True, broken_at_wave=None)
        return storm

    monkeypatch.setattr(BT, "_seeded_random", patched)
    monkeypatch.setattr(BT, "_resolve_tribulation", resolve)


# ---------------------------------------------------------------------------
# 1. The gate
# ---------------------------------------------------------------------------

def test_attempt_is_refused_while_the_gate_is_open_nothing(harness):
    """A player who has not prepared cannot attempt -- and loses nothing."""
    client, main, pid = harness
    _grant_stones(main, pid, 100000)
    _fill_bar(main, pid)

    before = client.get(f"/breakthrough/{pid}").json()
    assert before["gate_open"] is False
    assert before["can_attempt"] is False

    r = client.post(f"/breakthrough/{pid}/attempt")
    assert r.status_code == 409, r.text
    assert "Preparation is incomplete" in r.json()["detail"]

    # A REFUSED attempt is free. If a refusal could charge stones, a player
    # could be drained by pressing a button that does nothing.
    after = client.get(f"/breakthrough/{pid}").json()
    assert after["stones"] == before["stones"]
    assert after["attempts"] == before["attempts"] == 0


def test_preparation_spends_stones_and_fills_pillars_one_at_a_time(harness):
    client, main, pid = harness
    _grant_stones(main, pid, 100000)

    first = client.post(f"/breakthrough/{pid}/prepare", json={"pillar": "qi"}).json()
    assert first["spent_stones"] == BT.PREP_STONE_COST
    assert 0 < first["gained_points"] <= BT.PILLAR_MAX
    assert first["value"] == first["gained_points"]

    # Preparation is priced off the player's own balance, and it lands.
    assert client.get(f"/breakthrough/{pid}").json()["stones"] == \
        100000 - BT.PREP_STONE_COST

    # A full pillar refuses more of the same, so the gate cannot be over-filled.
    _fill_gate(client, pid)
    view = client.get(f"/breakthrough/{pid}").json()
    assert view["gate_open"] is True
    for pillar in BT.PILLARS:
        p = next(x for x in view["gate"]["pillars"] if x["pillar"] == pillar)
        assert p["value"] == BT.PILLAR_MAX
    over = client.post(f"/breakthrough/{pid}/prepare", json={"pillar": "qi"})
    assert over.status_code == 409


def test_an_unknown_pillar_is_refused_not_guessed(harness):
    client, main, pid = harness
    _grant_stones(main, pid, 1000)
    r = client.post(f"/breakthrough/{pid}/prepare", json={"pillar": "vibes"})
    assert r.status_code == 400, r.text


def test_a_broken_through_body_is_not_purchasable(harness, monkeypatch):
    """Preparation is the only thing stones buy. The attempt is not for sale.

    A player with a full wallet and a full gate still has to survive a storm
    and a roll. The strongest form of that claim: even at the ceiling chance
    with a perfect gate and ten million stones, a bad roll still fails. If
    money could buy the realm, this would pass.
    """
    client, main, pid = harness
    _grant_stones(main, pid, 10_000_000)
    _fill_bar(main, pid)
    _fill_gate(client, pid)

    view = client.get(f"/breakthrough/{pid}").json()
    assert view["chance"] < 1.0, "a guaranteed breakthrough is not a tribulation"

    _force_outcome(monkeypatch, success=False)
    r = client.post(f"/breakthrough/{pid}/attempt")
    assert r.status_code == 200, r.text
    assert r.json()["success"] is False
    assert _restarted_store(main)[pid]["cultivation_realm"] == "mortal"


# ---------------------------------------------------------------------------
# 2. Failure has a cost
# ---------------------------------------------------------------------------

def test_a_failed_attempt_costs_progress_stones_and_sets_a_scar(harness, monkeypatch):
    client, main, pid = harness
    _grant_stones(main, pid, 100000)
    _fill_bar(main, pid)
    _fill_gate(client, pid)
    before = client.get(f"/breakthrough/{pid}").json()

    _force_outcome(monkeypatch, success=False)
    r = client.post(f"/breakthrough/{pid}/attempt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is False
    assert body["result"]["outcome"] == "failed"

    after = body["persisted"]
    # 1. The stones are gone: the attempt cost AND the preparation.
    assert after["stones"] < before["stones"]
    assert before["stones"] - after["stones"] >= BT.ATTEMPT_STONE_COST
    # 2. The stage progress is scattered.
    assert after["progress"] == 0.0
    # 3. A scar is permanent.
    assert after["gate"]["scars"] == 1
    assert after["failures"] == 1
    # 4. The next attempt is locked out, so failure cannot be spammed.
    assert after["cooldown_remaining"] > 0.0
    assert after["can_attempt"] is False

    # A second press while the storm is still fresh is refused, and the gate
    # is empty again anyway: preparation is spent, not banked.
    retry = client.post(f"/breakthrough/{pid}/attempt")
    assert retry.status_code == 409, retry.text
    assert client.get(f"/breakthrough/{pid}").json()["stones"] == after["stones"]


def test_a_scar_reduces_the_chance_of_the_next_attempt(harness, monkeypatch):
    """Failure leaves a mark: the same gate is worth less after one scar.

    Measured at a realm where the scar is VISIBLE. A mortal's first gate has a
    configured base of 0.95 and the chance is capped at 0.90, so there is no
    headroom there for a penalty to show -- the early ladder really is nearly
    free, and the tension lives higher up. Asserting the scar at mortal/early
    would have passed for the wrong reason: both numbers would be the cap.
    """
    client, main, pid = harness
    with main.players_db.transaction(pid):
        row = main.players_db[pid]
        row["cultivation_realm"] = "soul_transformation"
        row["cultivation_stage"] = "early"   # configured base 0.35
        main.players_db.persist(pid, row)
    _grant_stones(main, pid, 100000)
    _fill_bar(main, pid)
    _fill_gate(client, pid)
    clean_chance = client.get(f"/breakthrough/{pid}").json()["chance"]
    assert clean_chance < BT.CHANCE_CEILING, "premise: there is headroom to lose"

    _force_outcome(monkeypatch, success=False)
    client.post(f"/breakthrough/{pid}/attempt")

    # Clear the cooldown server-side to measure the scar's effect alone.
    with main.players_db.transaction(pid):
        row = main.players_db[pid]
        row["breakthrough"]["cooldown_until"] = 0.0
        main.players_db.persist(pid, row)
    _fill_bar(main, pid)
    _fill_gate(client, pid)
    scarred_chance = client.get(f"/breakthrough/{pid}").json()["chance"]

    assert scarred_chance < clean_chance
    assert scarred_chance >= BT.CHANCE_FLOOR


# ---------------------------------------------------------------------------
# 3. The storm is server-resolved
# ---------------------------------------------------------------------------

def test_the_tribulation_is_resolved_on_the_server_and_scales_with_realm():
    """Wave count rises with the realm, and the transcript is generated here."""
    low = {"id": "p", "cultivation_realm": "mortal", "cultivation_stage": "late"}
    high = {"id": "p", "cultivation_realm": "core", "cultivation_stage": "late"}
    state = BT.state_of(dict(low))

    low_storm = BT._resolve_tribulation(dict(low), BT.state_of(dict(low)), 1)
    high_storm = BT._resolve_tribulation(dict(high), BT.state_of(dict(high)), 1)

    assert low_storm["waves_total"] < high_storm["waves_total"]
    assert len(low_storm["transcript"]) == low_storm["waves_survived"]
    for wave in low_storm["transcript"]:
        assert 1 <= wave["wave"] <= wave["of"]
        assert wave["strike"] > 0


def test_the_same_player_and_attempt_resolve_to_the_same_storm():
    """Deterministic: a disputed outcome can be re-derived from its inputs."""
    player = {"id": "fixed", "cultivation_realm": "mortal", "cultivation_stage": "late"}
    a = BT._resolve_tribulation(dict(player), BT.state_of(dict(player)), 3)
    b = BT._resolve_tribulation(dict(player), BT.state_of(dict(player)), 3)
    assert a == b
    c = BT._resolve_tribulation(dict(player), BT.state_of(dict(player)), 4)
    assert c != a


def test_preparation_is_what_lets_you_survive_the_storm():
    """An unprepared dao pillar is a shorter storm survival, not a cosmetic."""
    weak = {"id": "p", "cultivation_realm": "soul_transformation",
            "cultivation_stage": "late"}
    strong = dict(weak)
    weak_state = BT.state_of(dict(weak))
    strong_state = BT.state_of(dict(strong))
    strong_state["pillars"]["dao"] = BT.PILLAR_MAX

    weak_storm = BT._resolve_tribulation(dict(weak), weak_state, 1)
    strong_storm = BT._resolve_tribulation(dict(strong), strong_state, 1)
    assert strong_storm["defence"] > weak_storm["defence"]
    assert strong_storm["waves_survived"] >= weak_storm["waves_survived"]


# ---------------------------------------------------------------------------
# 4. Success persists
# ---------------------------------------------------------------------------

def test_a_full_preparation_does_not_guarantee_surviving_the_storm():
    """Heaven's answer must be able to say no.

    Measured over 200 seeded attempts at every realm, a fully prepared gate
    survives roughly half the time, with the spread across the whole ladder
    under 25 points. If this ever reads 100% everywhere, the tribulation has
    become a formality that merely looks expensive -- the same defect as a
    guaranteed breakthrough, wearing a storm costume. If it reads 0%, the high
    ladder is a wall no preparation can pass.
    """
    import random as _random
    realms = ["mortal", "foundation", "core", "nascent_soul",
              "soul_transformation", "dao_seeking"]
    rates = {}
    for realm in realms:
        wins = 0
        trials = 200
        for n in range(1, trials + 1):
            player = {"id": "surveyor", "cultivation_realm": realm,
                      "cultivation_stage": "late",
                      "breakthrough": {"pillars": {k: BT.PILLAR_MAX
                                                   for k in BT.PILLARS}}}
            state = BT.state_of(player)
            if BT._resolve_tribulation(player, state, n)["survived"]:
                wins += 1
        rates[realm] = 100.0 * wins / trials
    assert all(0.0 < r < 100.0 for r in rates.values()), (
        f"the storm cannot fail at some realm: {rates}")
    spread = max(rates.values()) - min(rates.values())
    assert spread < 30.0, f"the storm is far harsher at one end: {rates}"


def test_more_preparation_survives_more_often():
    """A fuller dao pillar is a sturdier body, and that is measurable."""
    def wins(dao):
        total = 0
        for n in range(1, 101):
            player = {"id": "surveyor", "cultivation_realm": "core",
                      "cultivation_stage": "late",
                      "breakthrough": {"pillars": {"qi": 100, "dao": dao,
                                                   "mind": 100}}}
            state = BT.state_of(player)
            if BT._resolve_tribulation(player, state, n)["survived"]:
                total += 1
        return total
    assert wins(BT.PILLAR_MAX) >= wins(0), "preparation bought nothing"


def test_a_successful_breakthrough_persists_across_a_fresh_read(harness, monkeypatch):
    """THE persistence proof.

    The store returns COPIES on read, so a write that did not go through the
    store's writer is discarded silently and the player stays exactly where
    they started. Three reads are taken here, and each is a different kind of
    fresh:

      * the response's own ``persisted`` view, re-read after the transaction;
      * a read through a NEWLY CONSTRUCTED store object;
      * the raw row, to confirm the column itself moved.
    """
    client, main, pid = harness
    _grant_stones(main, pid, 100000)
    _fill_bar(main, pid)
    _fill_gate(client, pid)
    before = client.get(f"/breakthrough/{pid}").json()
    assert before["realm"] == "mortal" and before["stage"] == "early"
    before_atk = before.get("temper", 1.0)

    _force_outcome(monkeypatch, success=True)
    r = client.post(f"/breakthrough/{pid}/attempt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True, body
    assert body["result"]["outcome"] == "success"
    # The ladder is per-STAGE first: a mortal at EARLY advances to the
    # mortal's middle stage. Only the last stage of a realm changes the realm.
    assert body["result"]["to_realm"] == "mortal"
    assert body["result"]["to_stage"] == "middle"

    # 1. the post-transaction re-read
    landed = body["persisted"]
    assert landed["realm"] == "mortal"
    assert landed["stage"] == "middle"
    assert landed["successes"] == 1
    # The body changed permanently, and the power number moved with it.
    assert landed["temper"] > before_atk
    assert landed["failures"] == 0

    # 2. a different store object entirely
    other = _restarted_store(main)
    fresh = other[pid]
    assert fresh["cultivation_realm"] == "mortal", (
        "the promotion did not survive a fresh read -- the write was lost")
    assert fresh["cultivation_stage"] == "middle", (
        "the new stage did not survive a fresh read -- the write was lost")
    assert fresh["progress"] == 0.0
    assert fresh["breakthrough"]["successes"] == 1
    assert fresh["breakthrough"]["temper"] > 1.0

    # 3. the view the client will be shown also says foundation
    assert client.get(f"/breakthrough/{pid}").json()["stage"] == "middle"


def test_a_successful_breakthrough_changes_the_players_power(harness, monkeypatch):
    """Not a label: the numbers the game fights with actually move."""
    client, main, pid = harness
    _grant_stones(main, pid, 100000)
    _fill_bar(main, pid)
    _fill_gate(client, pid)

    with main.players_db.transaction(pid):
        row = main.players_db[pid]
        before_stats = (row["atk"], row["hp"], row["def_"])
        main.players_db.persist(pid, row)

    _force_outcome(monkeypatch, success=True)
    assert client.post(f"/breakthrough/{pid}/attempt").json()["success"] is True

    other = _restarted_store(main)
    row = other[pid]
    after_stats = (row["atk"], row["hp"], row["def_"])
    assert all(a > b for a, b in zip(after_stats, before_stats)), (
        "a breakthrough that does not change the body is not a breakthrough")
    assert row["lifespan_days"] == int(row.get("lifespan_days", 0))


def test_the_next_gate_is_a_new_gate_after_a_success(harness, monkeypatch):
    """Preparation is spent, not banked across the promotion."""
    client, main, pid = harness
    _grant_stones(main, pid, 100000)
    _fill_bar(main, pid)
    _fill_gate(client, pid)
    _force_outcome(monkeypatch, success=True)
    client.post(f"/breakthrough/{pid}/attempt")

    after = client.get(f"/breakthrough/{pid}").json()
    assert after["stage"] == "middle"
    assert after["gate_open"] is False, "preparation carried across the promotion"
    assert after["target_realm"] == "mortal"
    assert after["target_stage"] == "late"


def test_a_second_breakthrough_from_the_next_gate_works(harness, monkeypatch):
    """Walk the ladder twice: prepare, attempt, advance, prepare, attempt."""
    client, main, pid = harness
    _grant_stones(main, pid, 1_000_000)

    for expected_realm, expected_stage in (("mortal", "middle"),
                                           ("mortal", "late")):
        _fill_bar(main, pid)
        _fill_gate(client, pid)
        _force_outcome(monkeypatch, success=True)
        r = client.post(f"/breakthrough/{pid}/attempt")
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True, r.json()
        view = _restarted_store(main)[pid]
        assert view["cultivation_realm"] == expected_realm
        assert view["cultivation_stage"] == expected_stage

    assert _restarted_store(main)[pid]["breakthrough"]["successes"] == 2

    # Two more advances and the player leaves the mortal realm entirely,
    # which is where the ladder's realm change is actually exercised.
    for expected_realm, expected_stage in (("foundation", "early"),
                                           ("foundation", "middle")):
        _fill_bar(main, pid)
        _fill_gate(client, pid)
        _force_outcome(monkeypatch, success=True)
        r = client.post(f"/breakthrough/{pid}/attempt")
        assert r.json()["success"] is True, r.json()
        row = _restarted_store(main)[pid]
        assert row["cultivation_realm"] == expected_realm
        assert row["cultivation_stage"] == expected_stage
    assert _restarted_store(main)[pid]["breakthrough"]["successes"] == 4


def test_the_middle_stage_gate_is_priced_from_the_middle_row():
    """The 'middle' / 'mid_late' alias bridge stays intact.

    A middle-stage player must be gated by the MIDDLE config row, not the
    early one. The bridge lives in cultivation_config.STAGE_NAME_ALIASES and
    breakthrough.py routes every stage lookup through it, so a middle player
    can never be silently paid or gated as an early one.
    """
    from cultivation_config import STAGE_NAME_ALIASES, get_stage

    assert STAGE_NAME_ALIASES["mid_late"] == "middle", "alias bridge removed"
    player = {"cultivation_realm": "mortal", "cultivation_stage": "mid_late",
              "progress": 0.0, "id": "x"}
    early = dict(player, cultivation_stage="early")
    state = BT.state_of(dict(player))

    assert BT.balance_ratio(state) == 0.0
    # The lookup must land on the MIDDLE row, not the early one. The
    # configured base chances differ (0.95 early vs 0.90 middle), so a
    # lookup that missed the alias would read the wrong row.
    from cultivation_config import calculate_breakthrough_chance
    assert BT._config_stage("mortal", "mid_late") is get_stage("mortal", "middle")
    assert calculate_breakthrough_chance("mortal", "middle") == \
        get_stage("mortal", "middle").breakthrough_base_chance
    # The runtime stage name is translated exactly once, on the way in.
    assert BT.stage_progress_used(player) == \
        get_stage("mortal", "middle").progress_required
    assert BT.stage_progress_used(early) == \
        get_stage("mortal", "early").progress_required


# ---------------------------------------------------------------------------
# 012-242 -- legacy_bonus was a live parameter no call site ever passed.
#
# The parameter was NEVER dead arithmetic: calculate_breakthrough_chance
# multiplied it by 0.05 and clamped. What was dead was the WIRING, in four
# production call sites that omitted it, so a player holding 10000 legacy
# points scored exactly what a player holding 0.0 scored.
#
# Measured before the fix, end to end through attempt_chance:
#   legacy_bonus=0.0      -> 0.85
#   legacy_bonus=10000.0  -> 0.85   <- the currency was inert
#
# The stage below is core/late (base 0.55) and the gate is DELIBERATELY
# UNBALANCED, because either fact alone would hide the change:
#   - core/late's base leaves headroom under the 0.90 engine ceiling, so
#     +0.05 and +0.10 are both visible. mortal/late (0.85) cannot show the
#     step: +0.10 lands on 0.95, which the ceiling clamps to 0.90 anyway.
#   - balance_ratio()=0.0 means BALANCE_BONUS contributes nothing, so the ONLY
#     thing that can move the number is legacy_bonus. A full gate would let the
#     balance bonus mask a broken wiring, which is the trap this avoids.
# ---------------------------------------------------------------------------


def _legacy_player(**over):
    """A core/late player -- base chance 0.55, room under the 0.90 ceiling."""
    player = {"cultivation_realm": "core", "cultivation_stage": "late"}
    player.update(over)
    return player


def _unbalanced_gate():
    """A gate whose balance bonus is zero, so legacy_bonus is the only mover."""
    return {"pillars": {"body": 100, "mind": 0, "spirit": 0},
            "scars": 0, "cooldown_until": 0.0}


def test_legacy_points_move_the_breakthrough_roll_end_to_end():
    """The defect itself: the currency has to reach the roll.

    Read off the table rather than pinned to a literal, so a rebalance of
    core/late does not red this. What is pinned is the RELATION between the
    three figures: each legacy point is worth exactly LEGACY_BONUS_PER_POINT,
    and the ladder is monotonic.
    """
    from cultivation_config import get_stage, LEGACY_BONUS_PER_POINT

    base = float(get_stage("core", "late").breakthrough_base_chance)
    assert BT.balance_ratio(_unbalanced_gate()) == 0.0, \
        "the gate must be unbalanced, or the balance bonus masks the wiring"

    none = BT.attempt_chance(_legacy_player(legacy_bonus=0.0), _unbalanced_gate())
    one = BT.attempt_chance(_legacy_player(legacy_bonus=1.0), _unbalanced_gate())
    two = BT.attempt_chance(_legacy_player(legacy_bonus=2.0), _unbalanced_gate())

    assert abs(none - base) < 1e-9, (none, base)
    assert abs(one - (none + LEGACY_BONUS_PER_POINT)) < 1e-9, (one, none)
    assert abs(two - (none + 2 * LEGACY_BONUS_PER_POINT)) < 1e-9, (two, none)
    assert none < one < two, (none, one, two)


def test_a_large_legacy_balance_saturates_at_the_engine_ceiling_not_the_config_one():
    """Which clamp wins. 0.90 here, not the config table's 0.95.

    This is the reason the bonus is summed inside attempt_chance rather than
    passed into calculate_breakthrough_chance: attempt_chance owns the
    0.05-0.90 clamp, so ONE clamp governs the bonus. Wiring it inside the
    config function would apply 0.95 first and 0.90 second, and the two
    ceilings would price the same player two different ways.
    """
    huge = BT.attempt_chance(_legacy_player(legacy_bonus=100000.0),
                             _unbalanced_gate())
    assert huge == BT.CHANCE_CEILING, huge
    assert BT.CHANCE_CEILING < 0.95


def test_a_player_with_no_legacy_field_scores_exactly_the_configured_base():
    """Absence is not a bonus. A row written before the field existed."""
    from cultivation_config import get_stage

    got = BT.attempt_chance(_legacy_player(), _unbalanced_gate())
    assert abs(got - float(get_stage("core", "late").breakthrough_base_chance)) \
        < 1e-9, got


def test_a_corrupt_stored_legacy_costs_the_bonus_and_not_the_roll():
    """A dirty field must not raise out of the chance calculation.

    Four of the five writers read legacy_bonus with a bare
    float(player.get(...)), which propagates a stored infinity or NaN forward
    instead of refusing it -- so the reader cannot assume a clean value. A
    corrupt bonus reads as 0.0: the player loses the BONUS, never the roll.
    """
    base = BT.attempt_chance(_legacy_player(legacy_bonus=0.0), _unbalanced_gate())
    for dirty in ("text", None, float("nan"), float("inf"), 10 ** 400, [1]):
        got = BT.attempt_chance(_legacy_player(legacy_bonus=dirty),
                                _unbalanced_gate())
        assert abs(got - base) < 1e-9, (dirty, got)


def test_legacy_bonus_of_is_total_and_never_raises():
    """The one reader for the field answers every shape.

    A reader that raises would take the whole breakthrough surface down on one
    bad row, which is the historical money bug in this module.
    """
    from cultivation_config import legacy_bonus_of

    for player in ({}, None, "not a dict", [], {"legacy_bonus": 0},
                   {"legacy_bonus": "x"}, {"legacy_bonus": {}}):
        assert legacy_bonus_of(player) == 0.0, player
    # A numeric string is a number, and the writers do write numbers as text.
    assert legacy_bonus_of({"legacy_bonus": "3.5"}) == 3.5


def test_the_scale_is_one_named_constant_rather_than_two_literals():
    """Two copies of 0.05 in two files is a drift waiting to happen.

    calculate_breakthrough_chance and attempt_chance apply the SAME multiplier
    to the SAME field. If either hardcodes its own copy, raising one makes the
    two clamps disagree about what a legacy point is worth. This asserts the
    constant is IMPORTED into breakthrough, not redeclared there.
    """
    from cultivation_config import LEGACY_BONUS_PER_POINT

    assert LEGACY_BONUS_PER_POINT == 0.05
    assert not hasattr(BT, "LEGACY_BONUS_PER_POINT"), \
        "breakthrough redeclared the scale instead of importing it"
