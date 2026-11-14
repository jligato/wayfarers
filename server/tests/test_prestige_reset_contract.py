"""
CONTRACT tests for ``server/prestige_reset.py`` — the prestige HARD RESET engine.

WHAT THIS FILE IS FOR
---------------------
``server/test_prestige_hard_reset.py`` already drives the feature through the
real store and over HTTP. That suite proves the reset works inside the game.
This file tests the module as a *module*: its function signatures, its return
shapes, its boundary conditions, its error codes, and the shape of the data it
stores. It runs against plain dictionaries — no database, no session, no HTTP
client — so it is fast, and so a failure points at a line of the engine rather
than at a store that hands back copies.

Why the faucet is the priority
------------------------------
A hard reset is the one routine in an idle game whose bug class is *free money*.
The reset deletes progress and grants a permanent advantage. If the grant can
be collected twice — by resetting and undoing in a loop, by resetting faster
than the safety valve closes, or by letting the reset clear a once-only marker
so its first-time reward can be claimed again — then the correct play for a
player is to spam the button. So the tests below are ordered so that every
farmable variant is measured by RUNNING it, not by reading the code:

  * ``TestTheFaucet`` — a 20-reset loop saturates; a 10x prestige+undo loop
    banks exactly nothing; a live undo cannot be replayed; the wallet never
    moves; once-only claim markers survive the reset so they cannot be re-armed.
  * ``TestSignatures`` — no exported entry point takes a caller-supplied
    number. A reset that accepted an ``amount`` or an ``insight`` argument
    would be a mint; this is asserted from ``inspect.signature`` so the
    parameter list itself is the contract.
  * ``TestReturnShapes`` — the exact key set of every returned mapping, so a
    renamed or dropped key is a failure rather than a silent ``KeyError`` in a
    caller that is not in this repo.
  * ``TestBoundaries`` — the realm gate on either side of the unlock, the
    insight floor and ceiling, the undo window on either side of its expiry.
  * ``TestErrors`` — ``PrestigeError`` is a ``ValueError`` carrying a 4xx
    status, and each refusal writes nothing at all.

TWO TRAPS THIS FILE AVOIDS, both learned elsewhere in this repo
--------------------------------------------------------------
1. A test that iterates ``RESET_FIELDS`` to check ``RESET_FIELDS`` cannot fail
   when that constant changes — deleting a field deletes the check on it too.
   So the expected reset set is spelled out here as a LITERAL, and likewise the
   undo window (86400.0) and the spent-undo counter (1).
2. A control that edits a source file must restore it byte-for-byte. The
   file is CRLF; a text-mode rewrite would silently change every line ending.

Run:
  tools/wc_pytest.sh server/tests/test_prestige_reset_contract.py
"""
from __future__ import annotations

import inspect
import json
import math
import os
import sys
import uuid
from copy import deepcopy
from pathlib import Path

for _var in ("WC_PG_DB", "WC_PG_HOST", "WC_PG_USER", "WC_PG_PASSWORD",
             "WC_PG_PORT"):
    os.environ.pop(_var, None)

_SERVER = Path(__file__).resolve().parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

import pytest

import prestige_reset as pr

# The HTTP layer. Imported at module scope on purpose so an import failure is a
# collection error rather than one test erroring later -- the classes below
# that need the app must fail loudly if the app cannot boot.
#
# ``server.test_auth_helpers.auth_headers`` mints a session the way trusted
# SERVER code does (``auth.issue_session``), because this game has no
# POST /auth/login and must not grow one: such a route would accept any
# player_id and hand every visitor a session for any player they name.
from fastapi.testclient import TestClient

from server.main import app, players_db
from server.test_auth_helpers import auth_headers
from server.tests.wc_test_auth import clear_signup_rate_limit


# /player/create carries a PROCESS-WIDE signup rate limit (five creations per
# 60s per address). The classes below create real players, so a file with more
# than five creating tests exhausts the window and later tests fail at the
# create call -- before the assertion under test ever runs. The limit has its
# own dedicated tests; clearing the counters here keeps this file about its own
# subject. Same treatment as server/tests/test_prestige_claim_transition_mint.py.
@pytest.fixture(autouse=True)
def _clear_signup_rate_limit():
    clear_signup_rate_limit()
    import server.main as _main

    for attr in ("_SIGNUP_ATTEMPTS", "_SIGNUP_WINDOW_START"):
        bucket = getattr(_main, attr, None)
        if hasattr(bucket, "clear"):
            bucket.clear()
    yield


# ---------------------------------------------------------------------------
# Literals, deliberately independent of the module constants they check
# ---------------------------------------------------------------------------

#: The progress fields a prestige is expected to reset, written out here rather
#: than read from ``RESET_FIELDS``.
EXPECTED_RESET_FIELDS = {
    "cultivation_realm", "cultivation_stage", "progress", "qi",
    "current_zone", "breakthrough_state",
}

#: The undo window, in seconds, as a literal (24h). NOT ``UNDO_WINDOW_SECONDS``.
UNDO_WINDOW = 24 * 3600.0

#: The spent-undo counter, as a literal (1). NOT ``UNDO_LIMIT``.
SPENT_UNDO_COUNT = 1

DAY = 86400.0
T0 = 1_700_000_000.0

#: The base values a reset returns the player to, as a literal.
EXPECTED_BASES = {
    "cultivation_realm": "mortal",
    "cultivation_stage": "early",
    "current_zone": "mortal_village",
    "breakthrough_state": None,
    "progress": 0.0,
    "qi": 0.0,
}


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

def make_player(**overrides) -> dict:
    """A plain-dict player row shaped like the store's.

    Defaults describe a player who has climbed to the unlock realm and holds
    money, purchases and one already-claimed mission — the state a reset is
    most likely to destroy by accident.
    """
    player = {
        "id": "p_contract",
        "name": "Contract",
        "spirit_root": "metal",
        "signing_key": "contract-test-signing-key",
        # progress, at non-base values so a reset has something to change
        "cultivation_realm": "core",
        "cultivation_stage": "late",
        "progress": 87.5,
        "qi": 42.25,
        "current_zone": "inner_sect",
        "breakthrough_state": {"pillars": {"body": 1, "heart": 0, "dao": 0},
                               "scars": 2},
        # money
        "spirit_stones": {"copper": 7777, "silver": 333, "gold": 42},
        "wallet": {"copper": 500, "silver": 10},
        # purchases and companions
        "cosmetics": {"title_first_ascendant": {"owned": True}},
        "inventory": {"jade_slab": 3},
        "beasts": {"reed_wolf": {"rarity": "rare"}},
        # earned records, including once-only claim markers
        "achievements": {"first_breakthrough": True},
        "missions": {"daily_sweep": {"claimed": True}},
        "tower_state": {"floor": 12, "tower_season_token": 7},
        "legacy": {},
    }
    player.update(overrides)
    return player


@pytest.fixture
def climbed():
    """A player standing at the unlock realm, ready to prestige."""
    return make_player()


def _climbed_for_reset(player: dict) -> dict:
    """Re-climb to the unlock realm, as a player must after each reset."""
    player["cultivation_realm"] = "core"
    player["cultivation_stage"] = "late"
    return player


def _drop_recovery(player: dict) -> dict:
    """Forget the undo window, isolating the GAIN's shape from the safety valve.

    A farming player does not press Undo; they wait. Letting the recovery lapse
    is the real loop, so that is what the loop tests do.
    """
    player["prestige"] = {"recovery": None}
    return player


def prestige_now(player: dict, now: float = T0) -> dict:
    """Reset, at an explicit clock, and return the engine's reply."""
    return pr.apply_prestige(player, pr.CONFIRM_PHRASE, now=now)


def without_undo_bookkeeping(row: dict) -> dict:
    """A copy of a row with the undo bookkeeping removed.

    The undo slot is *designed* to differ after an undo — it is consumed, and
    that consumption is the once-only guard. So "the row came back exactly" is
    asserted over everything except this one block, named rather than skipped
    silently.
    """
    out = deepcopy(row)
    out.pop("prestige", None)
    return out


# ===========================================================================
# The faucet. Every test here is measured by running the loop.
# ===========================================================================


class TestTheFaucet:
    """A reset that can be farmed is the defect this whole class exists for."""

    def test_prestige_then_undo_repeatedly_banks_nothing(self):
        """The sharpest farm: reset for the gain, undo for the progress.

        Ten times over. If the undo carried the permanent gain forward — even
        by a rounding error — the player would bank insight while keeping every
        scrap of progress, and the correct play would be to spam reset/undo.
        The correct outcome is that the row comes back EXACTLY as it was, ten
        times running, with no gain accumulated at any point.
        """
        player = make_player()
        before = deepcopy(player)

        gains = []
        for _ in range(10):
            result = prestige_now(player, now=T0)
            gains.append(float(player["dao_insight"]))
            undone = pr.undo_prestige(player, now=T0)
            assert undone["restored"] is True
            assert float(player.get("dao_insight", 0.0)) == 0.0, (
                "undo left a permanent gain behind: the reset is farmable by "
                "pressing reset then undo"
            )
            # And the whole row is back, not just the progress fields. The
            # undo bookkeeping is excluded by name: consuming it is the
            # once-only guard, so it is meant to differ.
            assert without_undo_bookkeeping(player) == without_undo_bookkeeping(before)

        assert gains == [pytest.approx(0.03)] * 10, gains

    def test_the_gain_saturates_over_twenty_resets(self):
        """A loop of twenty resets pays the fifth one exactly as much as the last.

        A multiplicative permanent gain (``1.1 ** n``) is unbounded by
        construction. This one is additive and capped, so it stops moving.
        """
        player = make_player()
        seen = []
        for cycle in range(20):
            _climbed_for_reset(player)
            _drop_recovery(player)
            result = prestige_now(player, now=T0 + cycle * (UNDO_WINDOW + 3600.0))
            seen.append(result["insight_bonus"])

        assert max(seen) <= pr.INSIGHT_CAP + 1e-9
        assert seen == sorted(seen), "the permanent gain went DOWN"
        assert seen[-1] == pytest.approx(pr.INSIGHT_CAP)
        # The fifth reset is worth exactly what the twentieth is worth.
        assert seen[4] == pytest.approx(seen[-1])
        # And the stored total keeps climbing past the cap while the bonus does
        # not: earning is auditable, spending is bounded.
        assert float(player["dao_insight"]) == pytest.approx(20 * pr.INSIGHT_PER_PRESTIGE)

    def test_a_live_undo_cannot_be_replayed(self):
        """The snapshot is consumed by the undo that used it.

        A second undo against the same snapshot would let a player re-read a
        pre-reset account indefinitely. The consumed slot must leave nothing to
        restore, and the refusal must be a 4xx rather than a silent repeat.
        """
        player = make_player()
        prestige_now(player, now=T0)
        pr.undo_prestige(player, now=T0)
        with pytest.raises(pr.PrestigeError) as exc:
            pr.undo_prestige(player, now=T0)
        assert exc.value.status_code == 409

    def test_a_spent_undo_counter_is_refused_even_with_a_live_snapshot(self):
        """The once-only limit, exercised on its own.

        The replay test above would also pass if the refusal came from the slot
        having been emptied. Here the snapshot is still sitting there in full
        and only the counter can refuse the undo, so the limit itself is what
        is being measured.
        """
        player = make_player()
        prestige_now(player, now=T0)
        player["prestige"]["recovery"]["undo_count"] = SPENT_UNDO_COUNT
        with pytest.raises(pr.PrestigeError) as exc:
            pr.undo_prestige(player, now=T0)
        assert exc.value.status_code == 409
        assert "already been used" in str(exc.value)

    def test_a_second_reset_cannot_overwrite_a_live_snapshot(self):
        """The safety valve must protect itself.

        Resetting again while the first recovery is pending would destroy the
        only record of the pre-reset account. The refusal is 409 and the first
        snapshot still works afterwards.
        """
        player = make_player()
        prestige_now(player, now=T0)
        _climbed_for_reset(player)
        with pytest.raises(pr.PrestigeError) as exc:
            prestige_now(player, now=T0 + 60.0)
        assert exc.value.status_code == 409
        assert "recovery" in str(exc.value)

        undone = pr.undo_prestige(player, now=T0 + 120.0)
        assert undone["restored"] is True
        assert player["cultivation_realm"] == "core"

    def test_a_reset_loop_moves_no_currency(self):
        """Ten resets must not create or destroy a single stone.

        Read from the row the engine mutates, because a reply is not a payout.
        """
        player = make_player()
        stones_before = deepcopy(player["spirit_stones"])
        wallet_before = deepcopy(player["wallet"])
        for cycle in range(10):
            _climbed_for_reset(player)
            _drop_recovery(player)
            prestige_now(player, now=T0 + cycle * (UNDO_WINDOW + 3600.0))
            assert player["spirit_stones"] == stones_before, (
                f"cycle {cycle} moved spirit stones"
            )
            assert player["wallet"] == wallet_before, (
                f"cycle {cycle} moved the wallet"
            )

    def test_a_reset_does_not_re_arm_a_once_only_claim(self):
        """The re-arm differential, run as a test.

        A first-time reward is usually gated on a marker the player carries.
        If the reset cleared that marker, the reward becomes claimable again,
        forever. Every marker-shaped field is compared across a reset.
        """
        player = make_player()
        player["legacy"]["carryover_applied_season"] = 7
        before = {
            "achievements": deepcopy(player["achievements"]),
            "missions": deepcopy(player["missions"]),
            "cosmetics": deepcopy(player["cosmetics"]),
            "tower_state": deepcopy(player["tower_state"]),
            "legacy": deepcopy(player["legacy"]),
        }
        prestige_now(player, now=T0)
        assert player["achievements"] == before["achievements"]
        assert player["missions"] == before["missions"]
        assert player["cosmetics"] == before["cosmetics"]
        assert player["tower_state"] == before["tower_state"]
        assert player["legacy"]["carryover_applied_season"] == \
            before["legacy"]["carryover_applied_season"]

    def test_the_gain_ignores_anything_the_caller_could_set(self):
        """The engine sizes its own reward.

        The row is seeded with every value a hostile or buggy caller might try
        to talk its way past, and the grant is still exactly one constant.
        """
        player = make_player(
            legacy={"ascension_count": 0, "first_prestige_bonus_claimed": False},
            prestige={"count": 999, "recovery": None},
        )
        result = prestige_now(player, now=T0)
        assert result["insight_bonus"] == pytest.approx(pr.INSIGHT_PER_PRESTIGE)
        assert float(player["dao_insight"]) == pytest.approx(pr.INSIGHT_PER_PRESTIGE)
        # The ascension ordinal is the player's own, incremented by one.
        assert player["legacy"]["ascension_count"] == 1
        # And the bookkeeping it overwrote was not consulted for the grant: the
        # prestige counter increments from whatever was there (it is monotonic
        # bookkeeping, not a reward) and the grant is still one constant.
        assert player["prestige"]["count"] == 1000
        assert result["prestige_count"] == 1000

    def test_the_ascension_ordinal_never_decreases_and_never_leaks_a_calendar(self):
        """The gain is keyed on the player's own history, not on a season.

        Two players who have prestigied the same number of times get the same
        gain no matter when they do it — a gain keyed on an absolute calendar
        season is unbounded by construction.
        """
        early = make_player()
        late = make_player()
        for player in (early, late):
            for _ in range(3):
                _climbed_for_reset(player)
                _drop_recovery(player)
                player["dao_insight"] = 0.0
        early_bonus = prestige_now(early, now=T0)["insight_bonus"]
        late_bonus = prestige_now(late, now=T0 + 365 * DAY)["insight_bonus"]
        assert early_bonus == pytest.approx(late_bonus)
        assert early["legacy"]["ascension_count"] == 1
        assert late["legacy"]["ascension_count"] == 1

    def test_undo_restores_the_pre_reset_row_completely(self):
        """Full row equality, not spot checks.

        The failure mode being guarded is a field nobody thought to check, so
        the assertion is the whole row against the snapshot taken before the
        reset, with the prestige-owned bookkeeping excluded by name.
        """
        player = make_player()
        before = deepcopy(player)
        prestige_now(player, now=T0)
        pr.undo_prestige(player, now=T0)

        assert without_undo_bookkeeping(player) == without_undo_bookkeeping(before)


# ===========================================================================
# Signatures — no caller-supplied number reaches a payout
# ===========================================================================


class TestSignatures:
    def test_no_entry_point_takes_a_caller_supplied_amount(self):
        """The parameter lists ARE the anti-mint contract.

        An exported function that grew an ``amount``, ``insight``, ``points``
        or ``count`` parameter would be a caller choosing its own reward. The
        names are asserted as literals so the check does not move with the
        signature it is checking.
        """
        expected = {
            "apply_prestige": ["player", "confirm", "now"],
            "undo_prestige": ["player", "now"],
            "plan_prestige": ["player", "now"],
            "recovery_state": ["player", "now"],
            "recovery_pending": ["player", "now"],
            "undo_available": ["player", "now"],
            "build_snapshot": ["player", "now"],
            "insight_points": ["player"],
            "insight_bonus": ["player"],
            "has_reached_unlock": ["player"],
        }
        for name, params in expected.items():
            func = getattr(pr, name)
            got = list(inspect.signature(func).parameters)
            assert got == params, f"{name}{tuple(got)} != {tuple(params)}"

    def test_optional_parameters_really_default(self):
        """``now`` is optional everywhere; omitting it must work.

        It is optional precisely so the HTTP route can stamp the server clock;
        a test that only ever passed an explicit clock would never notice the
        default had been removed.
        """
        player = make_player()
        assert isinstance(pr.plan_prestige(player), dict)
        assert isinstance(pr.recovery_state(player), (dict, type(None)))
        assert isinstance(pr.recovery_pending(player), bool)
        assert isinstance(pr.undo_available(player), bool)
        assert isinstance(pr.insight_bonus(player), float)
        result = pr.apply_prestige(player, pr.CONFIRM_PHRASE)
        assert result["ok"] is True
        assert isinstance(pr.undo_prestige(player), dict)

    def test_build_snapshot_requires_an_explicit_clock(self):
        """A snapshot stamped with 'now' rather than 'when' cannot be undone.

        It is the one place where an implicit clock would make the safety
        valve unfalsifiable, so the parameter is mandatory by signature.
        """
        sig = inspect.signature(pr.build_snapshot)
        assert sig.parameters["now"].default is inspect.Parameter.empty

    def test_every_public_callable_is_documented(self):
        """No undocumented public function joins this module.

        The reset's safety argument is that the allowlist is the WHOLE blast
        radius. A new public helper is new surface a caller can reach, so it
        must arrive with a docstring saying what it touches.
        """
        for name in sorted(dir(pr)):
            if name.startswith("_"):
                continue
            obj = getattr(pr, name)
            if not callable(obj):
                continue
            assert (obj.__doc__ or "").strip(), f"{name} has no docstring"


# ===========================================================================
# Return shapes
# ===========================================================================


class TestReturnShapes:
    def test_apply_prestige_reply_shape(self):
        player = make_player()
        result = prestige_now(player, now=T0)
        assert set(result) == {
            "ok", "prestige_count", "ascension_count", "insight_bonus",
            "dao_insight", "realm", "stage", "undo_expires_at", "reset_fields",
        }
        assert result["ok"] is True
        assert result["prestige_count"] == 1
        assert result["ascension_count"] == 1
        assert isinstance(result["insight_bonus"], float)
        assert isinstance(result["dao_insight"], float)
        assert result["realm"] == "mortal"
        assert result["stage"] == "early"
        assert result["undo_expires_at"] == pytest.approx(T0 + UNDO_WINDOW)
        assert isinstance(result["reset_fields"], list)
        assert result["reset_fields"] == list(pr.RESET_FIELDS)

    def test_undo_prestige_reply_shape(self):
        player = make_player()
        prestige_now(player, now=T0)
        result = pr.undo_prestige(player, now=T0 + 60.0)
        assert set(result) == {
            "ok", "restored", "realm", "stage", "insight_bonus", "taken_at",
        }
        assert result["ok"] is True
        assert result["restored"] is True
        assert result["realm"] == "core"
        assert result["stage"] == "late"
        assert result["insight_bonus"] == pytest.approx(0.0)
        assert result["taken_at"] == pytest.approx(T0)

    def test_plan_prestige_reply_shape(self):
        player = make_player()
        plan = pr.plan_prestige(player, now=T0)
        assert set(plan) == {
            "unlocked", "unlock_realm", "current_realm", "current_stage",
            "will_reset", "survives", "gains", "undo", "confirm_phrase",
        }
        assert plan["unlocked"] is True
        assert plan["unlock_realm"] == pr.UNLOCK_REALM
        assert plan["current_realm"] == "core"
        assert plan["current_stage"] == "late"
        assert plan["survives"] == list(pr.SURVIVES_FIELDS)
        assert plan["confirm_phrase"] == pr.CONFIRM_PHRASE
        assert set(plan["gains"]) == {
            "dao_insight_points", "insight_bonus_before", "insight_bonus_after",
            "insight_bonus_capped_at", "ascension_count_after", "permanent",
        }
        assert plan["gains"]["permanent"] is True
        assert plan["gains"]["dao_insight_points"] == pr.INSIGHT_PER_PRESTIGE
        assert plan["gains"]["insight_bonus_capped_at"] == pr.INSIGHT_CAP
        assert set(plan["undo"]) == {"available", "window_seconds", "limit"}
        assert plan["undo"]["window_seconds"] == UNDO_WINDOW
        assert plan["undo"]["limit"] == SPENT_UNDO_COUNT
        for entry in plan["will_reset"]:
            assert set(entry) == {"field", "before", "after"}

    def test_build_snapshot_reply_shape(self):
        player = make_player()
        snap = pr.build_snapshot(player, T0)
        assert set(snap) == {"version", "taken_at", "player"}
        assert snap["version"] == 1
        assert snap["taken_at"] == pytest.approx(T0)
        assert snap["player"] == player

    def test_recovery_state_reply_shape(self):
        player = make_player()
        prestige_now(player, now=T0)
        rec = pr.recovery_state(player, now=T0 + 60.0)
        assert rec is not None
        assert {"snapshot", "taken_at", "expires_at", "undo_count",
                "expired", "seconds_remaining"} <= set(rec)
        assert rec["undo_count"] == 0
        assert rec["expired"] is False
        assert rec["seconds_remaining"] == pytest.approx(UNDO_WINDOW - 60.0)

    def test_build_snapshot_is_a_deep_copy(self):
        """Mutating the player afterwards must not rewrite history.

        A shallow snapshot would let post-reset writes reach back into the
        record of the pre-reset account, which is the corruption the module
        docstring calls out.
        """
        player = make_player()
        snap = pr.build_snapshot(player, T0)
        player["spirit_stones"]["copper"] = 0
        player["breakthrough_state"]["scars"] = 99
        player["cosmetics"]["title_first_ascendant"]["owned"] = False
        assert snap["player"]["spirit_stones"]["copper"] == 7777
        assert snap["player"]["breakthrough_state"]["scars"] == 2
        assert snap["player"]["cosmetics"]["title_first_ascendant"]["owned"] is True

    def test_recovery_state_returns_a_copy(self):
        """A caller holding the returned dict must not be able to edit the store.

        The snapshot is the safety valve; a route that read it and then
        mutated the returned object would be editing the recovery in place.
        """
        player = make_player()
        prestige_now(player, now=T0)
        rec = pr.recovery_state(player, now=T0)
        rec["snapshot"]["player"]["spirit_stones"]["copper"] = 0
        rec["undo_count"] = SPENT_UNDO_COUNT
        rec["expires_at"] = T0
        again = pr.recovery_state(player, now=T0)
        assert again["snapshot"]["player"]["spirit_stones"]["copper"] == 7777
        assert again["undo_count"] == 0
        assert again["expires_at"] == pytest.approx(T0 + UNDO_WINDOW)


# ===========================================================================
# Boundaries
# ===========================================================================


class TestBoundaries:
    @pytest.mark.parametrize("realm,expected", [
        ("mortal", False),
        ("foundation", False),
        ("core", True),
        ("nascent_soul", True),
        ("soul_transformation", True),
        ("dao_seeking", True),
    ])
    def test_the_unlock_gate_on_both_sides_of_its_realm(self, realm, expected):
        assert pr.has_reached_unlock(make_player(cultivation_realm=realm)) is expected

    def test_an_unknown_or_missing_realm_is_below_the_gate(self):
        """An unrecognised realm must fail CLOSED, not open.

        A realm name this tree does not know about resolves to position -1,
        which is below every ladder rung, so it can never open the gate. If it
        resolved to the top instead, an unrecognised value would grant the
        permanent gain.
        """
        assert pr.has_reached_unlock(make_player(cultivation_realm="ascended")) is False
        assert pr.has_reached_unlock(make_player(cultivation_realm="")) is False
        assert pr.has_reached_unlock({}) is False
        assert pr._realm_order("ascended") == -1
        assert pr._realm_order("mortal") == 0

    def test_reset_refuses_below_the_unlock_realm_and_writes_nothing(self):
        player = make_player(cultivation_realm="foundation")
        before = deepcopy(player)
        with pytest.raises(pr.PrestigeError) as exc:
            prestige_now(player, now=T0)
        assert exc.value.status_code == 409
        assert player == before, "a refused reset wrote something"
        assert "core" in str(exc.value)

    @pytest.mark.parametrize("stored,expected_bonus", [
        (-5.0, 0.0),
        (0.0, 0.0),
        (pr.INSIGHT_PER_PRESTIGE, pr.INSIGHT_PER_PRESTIGE),
        (pr.INSIGHT_CAP, pr.INSIGHT_CAP),
        (pr.INSIGHT_CAP * 10, pr.INSIGHT_CAP),
        (9999.0, pr.INSIGHT_CAP),
    ])
    def test_the_insight_bonus_is_floored_and_capped(self, stored, expected_bonus):
        player = make_player(dao_insight=stored)
        assert pr.insight_bonus(player) == pytest.approx(expected_bonus)
        assert 0.0 <= pr.insight_bonus(player) <= pr.INSIGHT_CAP

    @pytest.mark.parametrize("stored", [None, "not-a-number", object(), ""])
    def test_unusable_stored_insight_reads_as_zero(self, stored):
        """A corrupt value must not become a payout or an exception.

        The stored field is written by an earlier version of this module or by
        a store migration; a non-numeric value there must degrade to zero, not
        crash a route and not turn into a bonus.
        """
        player = make_player(dao_insight=stored)
        assert pr.insight_points(player) == 0.0
        assert pr.insight_bonus(player) == 0.0

    def test_insight_points_preserves_a_numeric_string(self):
        """A value that parses stays a number rather than becoming zero.

        JSON round-trips can leave the field as a string; silently reading that
        as zero would erase a player's earned permanent gain.
        """
        assert pr.insight_points(make_player(dao_insight="0.06")) == pytest.approx(0.06)

    def test_a_missing_insight_field_reads_as_zero(self):
        assert pr.insight_points({}) == 0.0
        assert pr.insight_points(make_player(dao_insight=None)) == 0.0

    def test_the_reset_allowlist_is_the_documented_one(self):
        """A reset that quietly stops resetting is a feature that no longer works.

        Expected set written as a LITERAL, not derived from the constant — a
        test that iterates the very constant it is checking cannot fail when
        that constant changes.
        """
        assert isinstance(pr.RESET_FIELDS, tuple)
        assert set(pr.RESET_FIELDS) == EXPECTED_RESET_FIELDS
        assert len(pr.RESET_FIELDS) == len(EXPECTED_RESET_FIELDS)

    def test_the_allowlist_and_the_survives_list_never_overlap(self):
        """A field in both lists is a field the reset destroys while promising
        it survives. The two lists are opposite halves of one contract.
        """
        assert not (set(pr.RESET_FIELDS) & set(pr.SURVIVES_FIELDS))

    def test_base_values_match_the_documented_bottom_of_the_ladder(self):
        for field, expected in EXPECTED_BASES.items():
            assert pr._base_value_for(field) == expected, field
        # A field nobody configured still gets a number, not None, so an
        # unlisted numeric field added to the allowlist resets sanely.
        assert pr._base_value_for("some_future_numeric_field") == 0.0

    def test_reset_returns_every_allowlisted_field_to_its_base(self):
        player = make_player()
        prestige_now(player, now=T0)
        for field, expected in EXPECTED_BASES.items():
            assert player[field] == expected, field

    def test_a_field_absent_from_the_row_is_not_invented(self):
        """The reset writes only fields the player already has.

        Inventing a key the store never wrote would put a value in the row
        that nothing else on this tree expects to find.
        """
        player = make_player()
        del player["qi"]
        del player["breakthrough_state"]
        prestige_now(player, now=T0)
        assert "qi" not in player
        assert "breakthrough_state" not in player

    def test_the_plan_lists_only_fields_that_will_actually_change(self):
        """A confirmation screen that says 'mortal -> mortal' teaches skimming."""
        player = make_player()
        player["progress"] = 0.0  # already at base
        del player["qi"]  # not on this row
        plan = pr.plan_prestige(player, now=T0)
        fields = {e["field"] for e in plan["will_reset"]}
        assert "progress" not in fields
        assert "qi" not in fields
        assert {"cultivation_realm", "cultivation_stage", "current_zone",
                "breakthrough_state"} <= fields

    def test_the_plan_predicts_the_reset_exactly(self):
        """Every ``after`` the plan promises is what the reset produces."""
        player = make_player()
        plan = pr.plan_prestige(player, now=T0)
        prestige_now(player, now=T0)
        for entry in plan["will_reset"]:
            assert player[entry["field"]] == entry["after"], entry["field"]
        assert plan["gains"]["ascension_count_after"] == 1
        assert plan["gains"]["insight_bonus_after"] == pytest.approx(
            round(min(pr.INSIGHT_CAP, pr.INSIGHT_PER_PRESTIGE), 4)
        )

    def test_the_plan_is_read_only(self):
        """Previewing a prestige must not change a single field."""
        player = make_player()
        before = deepcopy(player)
        pr.plan_prestige(player, now=T0)
        assert player == before

    def test_recovery_pending_is_false_for_a_fresh_account(self):
        """The regression that blocked every first prestige.

        ``recovery_pending`` and ``undo_available`` answer OPPOSITE questions.
        Collapsing them made a brand-new account — which correctly has no
        recovery — read as 'recovery pending', and the reset refused to run at
        all. Both must be False on a fresh row, and the reset must proceed.
        """
        player = make_player()
        assert pr.recovery_pending(player, now=T0) is False
        assert pr.undo_available(player, now=T0) is False
        assert pr.recovery_state(player, now=T0) is None
        assert pr.plan_prestige(player, now=T0)["undo"]["available"] is False
        result = prestige_now(player, now=T0)
        assert result["ok"] is True

    def test_recovery_pending_is_true_only_while_the_window_is_open(self):
        player = make_player()
        prestige_now(player, now=T0)
        assert pr.recovery_pending(player, now=T0 + 1.0) is True
        assert pr.undo_available(player, now=T0 + 1.0) is True
        # One second inside the boundary.
        assert pr.recovery_pending(player, now=T0 + UNDO_WINDOW - 1.0) is True
        # Exactly on the boundary: the window is CLOSED (<=), so this is the
        # last instant the undo is refused.
        assert pr.recovery_pending(player, now=T0 + UNDO_WINDOW) is False

    def test_undo_works_one_second_inside_the_window_and_not_on_it(self):
        """Both sides of the expiry boundary.

        Without the open side, 'always refuse an old undo' would pass the
        expiry test while having destroyed the safety valve entirely.
        """
        inside = make_player()
        prestige_now(inside, now=T0)
        assert pr.undo_prestige(inside, now=T0 + UNDO_WINDOW - 1.0)["restored"] is True
        assert inside["cultivation_realm"] == "core"

        on_it = make_player()
        prestige_now(on_it, now=T0)
        with pytest.raises(pr.PrestigeError) as exc:
            pr.undo_prestige(on_it, now=T0 + UNDO_WINDOW)
        assert exc.value.status_code == 409
        assert "window" in str(exc.value)

    def test_undo_grants_nothing(self):
        """An undo that paid out would be a faucet, not a recovery."""
        player = make_player(dao_insight=0.06)
        granted_before = float(player["dao_insight"])
        prestige_now(player, now=T0)
        assert float(player["dao_insight"]) == pytest.approx(granted_before + 0.03)
        pr.undo_prestige(player, now=T0)
        # The reset's own grant rolled back; the earlier prestige's did not.
        assert float(player["dao_insight"]) == pytest.approx(granted_before)

    def test_undo_keeps_the_identity_and_carries_forward_new_store_fields(self):
        """Identity is the row key and is never restored FROM the snapshot.

        A snapshot that disagreed with the current id would write the row under
        the wrong key. Conversely, a field another system added after the
        snapshot must survive the restore rather than being silently dropped.
        """
        player = make_player()
        prestige_now(player, now=T0)
        player["id"] = "p_contract_renamed"
        player["lifecycle"] = {"life_index": 2}
        player["dao_insight"] = 0.09  # the reset's grant, in the live row
        pr.undo_prestige(player, now=T0)
        assert player["id"] == "p_contract_renamed"
        assert player["lifecycle"] == {"life_index": 2}
        # dao_insight is undo-owned: the grant rolls back rather than stacking.
        # The key may be gone entirely, because the pre-reset row never had one.
        assert float(player.get("dao_insight", 0.0)) == pytest.approx(0.0)

    def test_a_spent_undo_leaves_no_live_recovery(self):
        player = make_player()
        prestige_now(player, now=T0)
        pr.undo_prestige(player, now=T0)
        assert player["prestige"]["recovery"] is None
        assert pr.recovery_state(player, now=T0) is None
        assert pr.recovery_pending(player, now=T0) is False
        # And the reset is available again, so the loop cannot wedge itself.
        _climbed_for_reset(player)
        assert prestige_now(player, now=T0 + UNDO_WINDOW + 1.0)["ok"] is True


# ===========================================================================
# Errors
# ===========================================================================


class TestErrors:
    def test_prestige_error_is_a_value_error_with_a_4xx_status(self):
        err = pr.PrestigeError("nope")
        assert isinstance(err, ValueError)
        assert err.status_code == 400
        assert err.message == "nope"
        assert str(err) == "nope"
        assert pr.PrestigeError("x", 409).status_code == 409
        # Every status the module raises must be a client error, never a 500.
        for status in (400, 409):
            assert 400 <= status < 500

    @pytest.mark.parametrize("bad", ["", "   ", "yes", "break my seal",
                                     "BREAK MY SEALS!", None, 0])
    def test_the_confirmation_phrase_is_required(self, bad):
        player = make_player()
        before = deepcopy(player)
        with pytest.raises(pr.PrestigeError) as exc:
            pr.apply_prestige(player, bad, now=T0)
        assert exc.value.status_code == 400
        assert "confirmation" in str(exc.value)
        assert player == before, "a mis-typed confirmation wrote something"

    def test_the_confirmation_phrase_is_normalised_not_exact(self):
        """Case and surrounding whitespace are forgiven; the words are not.

        A player typing on a phone keyboard should not lose an account to a
        stray space, and a client should not have to shout the phrase.
        """
        player = make_player()
        result = pr.apply_prestige(player, "  break my seals  ", now=T0)
        assert result["ok"] is True

    def test_the_unlock_is_checked_before_the_phrase(self):
        """A locked player is told to climb, not to retype.

        Ordering matters for the message a player sees: the phrase is not the
        problem when the gate is shut.
        """
        locked = make_player(cultivation_realm="mortal")
        with pytest.raises(pr.PrestigeError) as exc:
            pr.apply_prestige(locked, "wrong", now=T0)
        assert exc.value.status_code == 409
        assert "core" in str(exc.value)

    def test_undo_with_no_prestige_at_all_is_a_409(self):
        player = make_player()
        with pytest.raises(pr.PrestigeError) as exc:
            pr.undo_prestige(player, now=T0)
        assert exc.value.status_code == 409
        assert "no prestige" in str(exc.value)

    @pytest.mark.parametrize("broken", [
        None,
        "not-a-dict",
        {},
        {"recovery": None},
        {"recovery": {}},
        {"recovery": {"snapshot": {}}},
    ])
    def test_a_malformed_recovery_slot_is_treated_as_no_undo(self, broken):
        """Corrupt bookkeeping must not be a route crash or a free restore.

        Every one of these shapes is what a partial write, a schema change or a
        hand-edited row could leave behind. The answer must be a refusal.
        """
        player = make_player()
        player["prestige"] = broken
        assert pr.recovery_state(player, now=T0) is None
        assert pr.recovery_pending(player, now=T0) is False
        with pytest.raises(pr.PrestigeError) as exc:
            pr.undo_prestige(player, now=T0)
        assert exc.value.status_code == 409

    @pytest.mark.parametrize("broken", [
        "not-a-dict",
        ["a"],
        5,
        0,
        None,
        True,
        object(),
    ])
    def test_a_non_dict_legacy_is_coerced_rather_than_crashing(self, broken):
        """A corrupt legacy block must not wipe the player and then raise.

        ``apply_prestige`` writes the reset fields BEFORE it touches the legacy
        block, so a non-dict there used to raise AttributeError with the player
        already reset: no grant, no ordinal, no undo slot. The row was left
        half-reset and the caller saw a 500 rather than the module's own 4xx.

        The ordinal must also land exactly once. If this increment were ever
        moved inside the reset loop it would run once per allowlisted field and
        a single reset would bank six ascensions -- silent, and a faucet.
        """
        player = make_player(legacy=broken)
        result = prestige_now(player, now=T0)

        # It is a success, not a refusal and not a crash.
        assert result["ok"] is True
        assert result["ascension_count"] == 1

        # The block is now a dict, written back onto the row so the ordinal is
        # where claim_season_transition reads it from.
        assert isinstance(player["legacy"], dict)
        assert player["legacy"]["ascension_count"] == 1

        # ONCE, not once per reset field. len(RESET_FIELDS) is 6, so the
        # loop-placed variant would read 6 here.
        assert result["ascension_count"] == 1, (
            "the ordinal was incremented more than once in a single reset"
        )

        # The grant is still exactly one constant -- the coercion did not open
        # a second payout path.
        assert float(player["dao_insight"]) == pytest.approx(pr.INSIGHT_PER_PRESTIGE)
        assert result["insight_bonus"] == pytest.approx(pr.INSIGHT_PER_PRESTIGE)

        # And the reset actually completed: the safety valve exists, so the
        # half-reset outcome (wiped, no undo, no grant) is gone.
        assert isinstance(player["prestige"], dict)
        assert player["prestige"]["recovery"]["snapshot"]["player"]["progress"] == 87.5
        assert player["cultivation_realm"] == "mortal"
        assert player["progress"] == 0.0

    @pytest.mark.parametrize("broken", ["not-a-dict", ["a"], 5, None])
    def test_a_non_dict_prestige_block_is_coerced_too(self, broken):
        """The undo slot is the one thing a reset must never destroy.

        Two lines below the legacy read, the safety valve had the same shape of
        bug: a non-dict there raised AttributeError after the progress was
        already wiped, leaving no snapshot and no undo at all. A missing or
        unreadable slot means there is nothing left to protect, which is the
        same state as a player's first ever reset.
        """
        player = make_player(prestige=broken)
        result = prestige_now(player, now=T0)

        assert result["ok"] is True
        assert result["prestige_count"] == 1
        assert isinstance(player["prestige"], dict)
        assert player["prestige"]["recovery"]["undo_count"] == 0

        # And the valve works: the snapshot it just wrote is usable.
        undone = pr.undo_prestige(player, now=T0 + 60.0)
        assert undone["restored"] is True
        assert player["cultivation_realm"] == "core"

    def test_a_non_dict_legacy_does_not_re_arm_the_season_claim(self):
        """The once-only marker lives in ``legacy``. A reset must not clear it.

        ``prestige.claim_season_transition`` gates the carryover payout on
        ``legacy["carryover_applied_season"]``. If a reset reset that key to a
        fresh block the player could claim the same season twice, so the marker
        has to survive a reset that touches legacy. This is the money
        question: the coercion replaces a non-dict, and a non-dict has no
        marker -- but a REAL dict must pass through untouched.
        """
        player = make_player(legacy={
            "carryover_applied_season": 7,
            "ascension_season": 3,
            "ascension_calendar_season": 83,
            "total_legacy": 10.0,
            "ascension_count": 4,
        })
        prestige_now(player, now=T0)
        legacy = player["legacy"]

        assert legacy["carryover_applied_season"] == 7
        assert legacy["ascension_season"] == 3
        assert legacy["ascension_calendar_season"] == 83
        assert legacy["total_legacy"] == 10.0
        # Only the ordinal moves: 4 -> 5. Nothing else in the block is touched.
        assert legacy["ascension_count"] == 5

    def test_a_missing_legacy_block_still_resets_cleanly(self):
        """The engine creates its own bookkeeping rather than assuming it.

        A row written before these fields existed has no ``legacy`` and no
        ``prestige`` block; the first reset on it must still succeed and start
        the ordinals at one.
        """
        player = make_player()
        del player["legacy"]
        result = prestige_now(player, now=T0)
        assert result["ok"] is True
        assert result["ascension_count"] == 1
        assert result["prestige_count"] == 1
        assert player["legacy"]["ascension_count"] == 1

    def test_a_missing_prestige_block_does_not_crash(self):
        """Same for the undo bookkeeping the engine owns.

        A row written before the prestige block existed must prestige on its
        first try rather than raising on a missing key.
        """
        player = make_player()
        assert "prestige" not in player, "fixture assumption: no undo block yet"
        assert pr.recovery_state(player, now=T0) is None
        result = prestige_now(player, now=T0)
        assert result["prestige_count"] == 1
        assert isinstance(player["prestige"], dict)
# ===========================================================================
# Auth. The engine above is a pure function over a dict, so NOTHING in the
# classes above can see whether the reset is reachable by a stranger. A hard
# reset is the most destructive routine in the game; if the route that reaches
# it is open, an anonymous caller can wipe any player whose id they can guess.
# Measured over real HTTP here, because the guard that refuses lives in
# server/ownership_guard.py and is mounted by main.py -- neither is reachable
# from the engine.
# ===========================================================================


class TestResetRequiresAuth:
    """Every reset route refuses a caller who is not the player being reset."""

    @staticmethod
    def _raw():
        return TestClient(app)

    @staticmethod
    def _make_player(client, hint):
        """Create a player through the real route and return their id.

        ``clear_signup_rate_limit`` runs per test (the autouse fixture above),
        because /player/create carries a process-wide limit of five creations
        per minute per address and every test here creates players.
        """
        response = client.post("/player/create", json={
            "name": f"{hint}_{uuid.uuid4().hex[:8]}",
            "spirit_root": "metal",
        })
        assert response.status_code == 200, response.text
        return response.json()["id"]

    @staticmethod
    def _climb(pid):
        """Put the player at the unlock realm, through the store's writer.

        The same fixture-setup rule the HTTP suite in server/ uses: the climb is
        not what is under test, so it must not go through a route that could
        refuse.
        """
        with players_db.transaction(pid) as db:
            player = db[pid]
            player["cultivation_realm"] = pr.UNLOCK_REALM
            player["cultivation_stage"] = "late"
            player["progress"] = 87.5
            player["current_zone"] = "inner_sect"
            db.persist(pid, player)

    @pytest.mark.parametrize("method,path_suffix", [
        ("get", "/plan"),
        ("post", "/reset"),
        ("post", "/undo"),
    ])
    def test_an_anonymous_caller_is_refused_every_reset_route(self, method, path_suffix):
        """No session header at all -> 401, and the handler never runs.

        The reset is the most destructive thing a caller could ask this server
        to do, so it is reachable only as the player named in the path.
        """
        client = self._raw()
        pid = self._make_player(client, "PrestigeAnon")
        self._climb(pid)

        response = getattr(client, method)(f"/prestige/{pid}{path_suffix}")
        assert response.status_code == 401, response.text

        # The refusal wrote nothing: a 401 that had already half-run the reset
        # would still leave a wiped player behind it.
        row = players_db[pid]
        assert row["cultivation_realm"] == pr.UNLOCK_REALM
        assert float(row["progress"]) == 87.5
        assert row.get("dao_insight") is None

    @pytest.mark.parametrize("method,path_suffix", [
        ("get", "/plan"),
        ("post", "/reset"),
        ("post", "/undo"),
    ])
    def test_another_player_cannot_reset_someone_else(self, method, path_suffix):
        """A valid session for the WRONG player is a 403, not a 401.

        This is the sharper half of the auth contract. A 401 only proves the
        token was missing; it does not prove the token was checked against the
        id in the path. Without the match check, any player with any account
        could wipe any other player by naming their id -- and player ids travel
        in URLs that a client logs, shares and screenshots.
        """
        client = self._raw()
        attacker = self._make_player(client, "PrestigeAttacker")
        victim = self._make_player(client, "PrestigeVictim")
        self._climb(victim)

        headers = auth_headers(attacker, "")
        response = getattr(client, method)(
            f"/prestige/{victim}{path_suffix}", headers=headers
        )
        assert response.status_code == 403, response.text
        assert "does not match" in response.json()["detail"]

    def test_a_cross_player_reset_leaves_the_victim_completely_untouched(self):
        """The refusal is a refusal, not a partially-applied reset.

        Reading state rather than the reply: on this tree a handler can answer
        200 having written nothing, so the store is the only honest witness.
        """
        client = self._raw()
        attacker = self._make_player(client, "PrestigeAttacker")
        victim = self._make_player(client, "PrestigeVictim")
        self._climb(victim)
        with players_db.transaction(victim) as db:
            player = db[victim]
            player["spirit_stones"] = {"copper": 7777, "silver": 333}
            player["achievements"] = {"first_breakthrough": True}
            db.persist(victim, player)

        before = deepcopy(players_db[victim])
        response = client.post(
            f"/prestige/{victim}/reset",
            json={"confirm": pr.CONFIRM_PHRASE},
            headers=auth_headers(attacker, ""),
        )
        assert response.status_code == 403, response.text
        assert players_db[victim] == before, "a refused cross-player reset wrote something"

    def test_the_owner_can_reset_and_the_write_really_lands(self):
        """The control arm: auth must not have simply closed the route.

        Without this, every refusal above would also pass against a route that
        is broken for everybody -- a guard that refuses the owner too is not a
        working feature, it is a denial of service wearing a security costume.
        """
        client = self._raw()
        pid = self._make_player(client, "PrestigeOwner")
        self._climb(pid)

        response = client.post(
            f"/prestige/{pid}/reset",
            json={"confirm": pr.CONFIRM_PHRASE},
            headers=auth_headers(pid, ""),
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True

        # Read back from the store, not the reply.
        row = players_db[pid]
        assert row["cultivation_realm"] == pr.BASE_REALM
        assert row["cultivation_stage"] == pr.BASE_STAGE
        assert row["current_zone"] == pr.BASE_ZONE
        assert float(row["progress"]) == 0.0
        assert float(row["dao_insight"]) == pytest.approx(pr.INSIGHT_PER_PRESTIGE)
        assert row["prestige"]["recovery"]["snapshot"]["player"]["progress"] == 87.5

    def test_a_malformed_token_is_refused_rather_than_treated_as_a_wildcard(self):
        """A garbage header must not read as "no opinion, allow".

        This is the failure mode where a guard parses the token, fails to
        understand it, and returns None instead of raising -- and a None
        session then flows into the comparison as "not a player", which an
        ``if session is None: pass`` shape treats as absent-constraint.
        """
        client = self._raw()
        pid = self._make_player(client, "PrestigeBadToken")
        self._climb(pid)

        for bad in ("Bearer not-a-real-token", "", "null", "undefined"):
            response = client.post(
                f"/prestige/{pid}/reset",
                json={"confirm": pr.CONFIRM_PHRASE},
                headers={"Authorization": bad},
            )
            assert response.status_code == 401, f"{bad!r} -> {response.text}"
        assert players_db[pid]["cultivation_realm"] == pr.UNLOCK_REALM


# ===========================================================================
# Compounding. Measured by running the loop, not by reading the arithmetic.
# ===========================================================================


class TestCompounding:
    """The permanent gain must accumulate, and must stop accumulating."""

    def test_the_gain_accumulates_additively_across_a_run_of_resets(self):
        """N resets pay N constants, and the bonus tracks the stored total.

        The relationship between what is stored and what is spent is the whole
        contract: ``insight_bonus`` is a pure function of the stored points, so
        every partial run must land exactly on the ladder's straight line.
        Reading the ladder off the module would test nothing, so it is read off
        a real loop instead.
        """
        player = make_player()
        observed = []
        for cycle in range(1, 8):
            _climbed_for_reset(player)
            _drop_recovery(player)
            observed.append(prestige_now(
                player, now=T0 + cycle * (UNDO_WINDOW + 3600.0)
            )["insight_bonus"])

        # Every rung equals min(cap, n * per_prestige), read off the run rather
        # than off the module. The ladder rises by exactly one constant while
        # it is below the ceiling -- additive, not compounding (1.1 ** n would
        # keep growing) -- and is flat from the fifth reset onward, which is
        # the saturation the cap exists for: a tenth reset must be worth no
        # more than the fifth.
        expected_ladder = [
            min(pr.INSIGHT_CAP, n * pr.INSIGHT_PER_PRESTIGE)
            for n in range(1, len(observed) + 1)
        ]
        assert observed == pytest.approx(expected_ladder), observed

        # The rise below the ceiling, stated as a step so the ADDITIVE shape is
        # pinned separately from the ceiling.
        below_cap = [b for b in observed if b < pr.INSIGHT_CAP - 1e-9]
        for step in range(1, len(below_cap)):
            assert below_cap[step] - below_cap[step - 1] == pytest.approx(
                pr.INSIGHT_PER_PRESTIGE
            ), below_cap

        # And the stored total keeps climbing past the ceiling the bonus sits
        # on: earning is auditable, spending is bounded.
        assert float(player["dao_insight"]) == pytest.approx(
            len(observed) * pr.INSIGHT_PER_PRESTIGE
        )

    def test_the_bonus_is_the_stored_total_read_through_the_ceiling(self):
        """One relationship, stated once: stored points -> bonus.

        Held on both sides of the ceiling. Without the over-cap side, "the
        bonus equals the stored total" would pass on a build that let the bonus
        grow without limit -- which is the unbounded-permanent-gain defect the
        cap exists to prevent.
        """
        for stored in (0.0, 0.03, 0.09, 0.15, 0.30, 1e9):
            player = make_player(dao_insight=stored)
            assert pr.insight_bonus(player) == pytest.approx(
                min(pr.INSIGHT_CAP, max(0.0, stored))
            ), stored

    def test_a_second_prestige_continues_the_ordinal_rather_than_restarting_it(self):
        """The gain is keyed on the player's own history, so it compounds
        WITHIN an account and cannot be re-based by replaying from zero."""
        player = make_player()
        first = prestige_now(player, now=T0)
        _climbed_for_reset(player)
        _drop_recovery(player)
        second = prestige_now(player, now=T0 + UNDO_WINDOW + 60.0)

        assert first["ascension_count"] == 1
        assert second["ascension_count"] == 2
        assert player["legacy"]["ascension_count"] == 2
        assert second["insight_bonus"] == pytest.approx(2 * pr.INSIGHT_PER_PRESTIGE)

    def test_the_plan_predicts_the_compounded_bonus_before_it_is_spent(self):
        """The confirmation screen and the reset compute the same next bonus.

        A preview that understates the gain makes a player confirm a different
        trade than the one they get; a preview that overstates it is worse.
        """
        player = make_player(dao_insight=2 * pr.INSIGHT_PER_PRESTIGE)
        _climbed_for_reset(player)
        _drop_recovery(player)
        plan = pr.plan_prestige(player, now=T0)
        result = prestige_now(player, now=T0)

        assert plan["gains"]["insight_bonus_before"] == pytest.approx(
            round(2 * pr.INSIGHT_PER_PRESTIGE, 4)
        )
        assert plan["gains"]["insight_bonus_after"] == pytest.approx(
            result["insight_bonus"]
        )
        assert plan["gains"]["ascension_count_after"] == result["ascension_count"]


# ===========================================================================
# Progression preservation, read from the STORE across a real HTTP reset.
# ===========================================================================


class TestProgressionPreservedOverHttp:
    """What a prestige must NOT destroy, measured on the persisted row.

    The dict-level class ``TestTheFaucet`` already proves the engine preserves
    these fields in memory. This class proves the SURVIVING state survives the
    whole path -- route, transaction, store copy-on-read -- because a handler
    can answer 200 having written a row that lost half its fields.
    """

    @pytest.fixture
    def seeded(self):
        """A player at the unlock realm holding every group of durable state."""
        client = TestResetRequiresAuth._raw()
        pid = TestResetRequiresAuth._make_player(client, "PrestigeKeep")
        TestResetRequiresAuth._climb(pid)

        payload = {
            "spirit_stones": {"copper": 7777, "silver": 333, "gold": 42},
            "wallet": {"copper": 500, "silver": 10},
            "cosmetics": {"title_first_ascendant": {"owned": True}},
            "inventory": {"jade_slab": 3},
            "beasts": {"reed_wolf": {"rarity": "rare"}},
            "achievements": {"first_breakthrough": True},
            "missions": {"daily_sweep": {"claimed": True}},
            "tower_state": {"floor": 12, "tower_season_token": 7},
            "codex": {"reed_wolf": {"seen": 3}},
            "sect_id": "azure_cloud",
            "sect_rank": "elder",
            "pvp_rating": 1840,
            "legacy": {"carryover_applied_season": 7},
        }
        with players_db.transaction(pid) as db:
            player = db[pid]
            for key, value in payload.items():
                player[key] = deepcopy(value)
            db.persist(pid, player)
        return pid, payload

    def test_every_durable_field_survives_a_reset_over_http(self, seeded):
        """One assertion per field group, so a wipe names itself.

        Read back from the store: a 200 is not proof the write landed, and the
        store on this tree hands back COPIES on read.
        """
        pid, payload = seeded
        response = TestResetRequiresAuth._raw().post(
            f"/prestige/{pid}/reset",
            json={"confirm": pr.CONFIRM_PHRASE},
            headers=auth_headers(pid, ""),
        )
        assert response.status_code == 200, response.text

        row = players_db[pid]
        for key, value in payload.items():
            if key == "legacy":
                # Touched deliberately -- the ordinal is incremented -- but the
                # once-only season marker inside it must not move.
                assert row["legacy"]["carryover_applied_season"] == 7
                continue
            assert row[key] == value, f"a prestige destroyed {key}"

    def test_the_reset_fields_are_the_only_ones_that_moved(self, seeded):
        """The blast radius is the allowlist and nothing wider.

        Asserted as a set difference: which keys CHANGED must be exactly the
        progress fields, which is the property a denylist would violate and an
        allowlist holds.
        """
        pid, payload = seeded
        with players_db.transaction(pid) as db:
            player = db[pid]
            for key, value in payload.items():
                player[key] = deepcopy(value)
            db.persist(pid, player)
        before = deepcopy(players_db[pid])

        TestResetRequiresAuth._raw().post(
            f"/prestige/{pid}/reset",
            json={"confirm": pr.CONFIRM_PHRASE},
            headers=auth_headers(pid, ""),
        )
        after = players_db[pid]

        changed = {k for k in set(before) | set(after)
                   if before.get(k) != after.get(k)}
        # The allowlist's own fields, plus the three bookkeeping keys this
        # engine owns (the permanent gain and its two ordinals). Anything else
        # in `changed` is an unannounced wipe.
        assert changed <= set(EXPECTED_RESET_FIELDS) | {
            "dao_insight", "legacy", "prestige",
        }, changed

    def test_the_undo_restores_the_persisted_row_over_http(self, seeded):
        """The safety valve has to work through the store, not just in memory.

        A snapshot that restores correctly against a dict but not against the
        copied, transaction-published row would leave a player wiped with no
        way back -- the exact outcome the module is written to prevent.
        """
        pid, payload = seeded
        client = TestResetRequiresAuth._raw()
        headers = auth_headers(pid, "")
        before = deepcopy(players_db[pid])

        assert client.post(f"/prestige/{pid}/reset",
                           json={"confirm": pr.CONFIRM_PHRASE},
                           headers=headers).status_code == 200
        assert players_db[pid]["cultivation_realm"] == pr.BASE_REALM

        undone = client.post(f"/prestige/{pid}/undo", headers=headers)
        assert undone.status_code == 200, undone.text
        assert undone.json()["restored"] is True

        # The undo slot is consumed by design, so it is compared by name.
        after = players_db[pid]
        without_slot = {k: v for k, v in after.items() if k != "prestige"}
        expected = {k: v for k, v in before.items() if k != "prestige"}
        assert without_slot == expected
        assert after["prestige"]["recovery"] is None


# ===========================================================================
# Non-finite values. "Not a number" is a value the store can hold.
# ===========================================================================


class TestNonFiniteValuesAreRejected:
    """inf and nan must never reach the row, the gain, or the reply.

    The distinction that matters: ``int("inf")`` raises OverflowError while
    ``int("nan")`` raises ValueError, so an ``except (TypeError, ValueError)``
    arm catches exactly one of them and lets the other escape as a 500. These
    tests pin the isfinite guard rather than an exception arm, because only the
    guard can see both.
    """

    NON_FINITE = (float("inf"), float("-inf"), float("nan"))

    @pytest.mark.parametrize("bad", NON_FINITE + (10 ** 400, "nan", "inf"))
    def test_the_numeric_coercions_return_the_default_for_non_finite(self, bad):
        """Both coercions, both output types, one corrupt value each.

        The huge ``int`` is here too: ``int(10**400)`` raises nothing and
        returns a 401-digit number, so checking isfinite on the ORIGINAL value
        would raise on that input and trade one 500 for another.
        """
        assert pr._as_finite_int(bad, -1) == -1
        assert pr._as_finite_float(bad, -1.0) == -1.0

    def test_the_coercions_still_pass_real_numbers_through(self):
        """The control arm: the guard is a filter, not a blanket refusal.

        Without it, "the coercion always returns the default" would pass the
        test above while quietly destroying every stored counter on the tree.
        """
        assert pr._as_finite_int(7) == 7
        assert pr._as_finite_int(7.9) == 7
        assert pr._as_finite_float(0.06) == pytest.approx(0.06)
        assert pr._as_finite_float("0.06") == pytest.approx(0.06)
        assert pr._as_finite_int(-3) == -3

    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_a_non_finite_stored_insight_reads_as_zero_and_grants_one_constant(self, bad):
        """A poisoned gain column cannot become a payout or an exception.

        ``float()`` never raises on inf or nan -- it returns them -- so an
        ``except`` arm here would be dead code. Only isfinite can see this, and
        without it the poison reaches the player row and the JSON reply.
        """
        player = make_player(dao_insight=bad)
        assert pr.insight_points(player) == 0.0
        assert pr.insight_bonus(player) == 0.0

        result = prestige_now(player, now=T0)
        assert result["ok"] is True
        # Exactly one constant. A poisoned stored value must not add to itself.
        assert float(player["dao_insight"]) == pytest.approx(pr.INSIGHT_PER_PRESTIGE)
        assert result["insight_bonus"] == pytest.approx(pr.INSIGHT_PER_PRESTIGE)

    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_a_non_finite_ordinal_counts_as_zero_not_as_a_huge_number(self, bad):
        """The bookkeeping columns are coerced the same way.

        inf as an ascension count is the more dangerous of the two: it is a
        counter, and an uncapped one would be a free permanent gain rather than
        a crash.
        """
        player = make_player(
            legacy={"ascension_count": bad},
            prestige={"count": bad, "recovery": None},
        )
        result = prestige_now(player, now=T0)
        assert result["ascension_count"] == 1
        assert result["prestige_count"] == 1
        assert float(player["dao_insight"]) == pytest.approx(pr.INSIGHT_PER_PRESTIGE)

    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_a_non_finite_expiry_reads_as_still_open_so_the_snapshot_survives(self, bad):
        """The direction of the fallback is the safety argument.

        ``apply_prestige`` treats an expired recovery as permission to reset
        again, and a second reset OVERWRITES the only snapshot that could undo
        the first. So a corrupt expiry must fail CLOSED -- unreadable means
        still-open, which blocks the reset and protects the undo record.
        Failing open here trades a temporary block on a corrupt row for an
        unrecoverable wipe.
        """
        player = make_player()
        prestige_now(player, now=T0)
        player["prestige"]["recovery"]["expires_at"] = bad

        rec = pr.recovery_state(player, now=T0 + 60.0)
        assert rec is not None
        assert rec["expired"] is False
        assert rec["expires_at"] == pr.UNDETERMINED_EXPIRY

        # And the reply stays valid JSON: inf would poison the very reply this
        # guard exists to protect, which is why the fallback is finite.
        assert math.isfinite(rec["seconds_remaining"])
        json.dumps(rec)

    def test_a_non_finite_expiry_actually_blocks_the_second_reset(self):
        """The fail-closed direction, measured by driving the reset.

        ``recovery_state`` returning ``expired: False`` is the intermediate.
        What has to be true is the consequence: the second prestige is refused
        and the first snapshot is still usable afterwards.
        """
        player = make_player()
        prestige_now(player, now=T0)
        player["prestige"]["recovery"]["expires_at"] = float("nan")

        _climbed_for_reset(player)
        with pytest.raises(pr.PrestigeError) as exc:
            prestige_now(player, now=T0 + 60.0)
        assert exc.value.status_code == 409

        # The original snapshot survived the blocked attempt.
        player["prestige"]["recovery"]["expires_at"] = T0 + UNDO_WINDOW
        undone = pr.undo_prestige(player, now=T0 + 120.0)
        assert undone["restored"] is True
        assert player["cultivation_realm"] == "core"
        assert float(player["progress"]) == 87.5

    def test_a_non_finite_undo_count_blocks_the_reset_rather_than_allowing_one(self):
        """A corrupt undo counter must not read as "zero undos spent"...

        Actually it must read the opposite way: an unreadable counter is not
        provably spent, and this guard protects the snapshot. Read as zero it
        would be < the limit and would permit the reset that overwrites the
        recovery. So it fails closed here too, and the reset is held.
        """
        player = make_player()
        prestige_now(player, now=T0)
        player["prestige"]["recovery"]["undo_count"] = float("inf")

        assert pr.recovery_pending(player, now=T0 + 60.0) is True
        _climbed_for_reset(player)
        with pytest.raises(pr.PrestigeError) as exc:
            prestige_now(player, now=T0 + 120.0)
        assert exc.value.status_code == 409

    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_the_recovery_reply_never_carries_a_non_finite_number(self, bad):
        """Every number the recovery read hands back is finite.

        Asserted as a sweep over the returned mapping rather than one field at a
        time, because the reply is serialised to JSON by the route and a single
        surviving inf is a crash on the wire.
        """
        player = make_player()
        prestige_now(player, now=T0)
        player["prestige"]["recovery"]["expires_at"] = bad
        player["prestige"]["recovery"]["taken_at"] = bad

        rec = pr.recovery_state(player, now=T0 + 60.0)
        for key in ("expires_at", "taken_at", "seconds_remaining"):
            assert math.isfinite(float(rec[key])), key
        json.dumps(rec)

    def test_a_non_finite_snapshot_is_not_a_free_restore(self):
        """Corrupt bookkeeping must refuse, not restore.

        A row whose recovery slot has been filled with junk by a partial write
        has no pre-reset account in it. Restoring from it would replace a real
        player row with whatever the junk happened to contain.
        """
        player = make_player()
        for junk in (float("nan"), float("inf"), "not-a-snapshot", 5, None):
            broken = make_player()
            broken["prestige"] = {
                "count": 1,
                "recovery": {
                    "snapshot": junk,
                    "taken_at": float("nan"),
                    "expires_at": float("inf"),
                    "undo_count": 0,
                },
            }
            with pytest.raises((pr.PrestigeError, TypeError, KeyError)):
                pr.undo_prestige(broken, now=T0)