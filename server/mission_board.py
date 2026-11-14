"""The MISSION BOARD: a server-timed checklist that points at the other systems.

What this is, and what it is not
--------------------------------
Three similar things exist in this codebase and they are NOT the same feature:

``missions.py``            A per-mission event ledger. Each mission tracks one
                          CATEGORY of server-recorded event, progress is derived
                          from a running count minus a baseline, and claiming
                          pays spirit stone. It is a counter with a payout.
``goal_graph.py``          A read-only dependency view: "you want skill X, you
                          need material Y, here is the route". It never spends,
                          grants or mutates anything.
``mission_board.py``      THIS FILE. A board the player reads as a checklist:
                          a MIX of timescales (a thing you can do in a minute,
                          a thing for the day, a thing for the week), each row
                          pointing at another system (hunting, the tower, PvP,
                          beasts, cultivation), each claimable once per cycle,
                          the whole thing turning over on a server-timed cycle
                          with a visible countdown.

So the board does not duplicate the ledger: it READS progress that the other
systems already wrote to the player record (and the mission event ledger the
hunt system writes). It never lets a caller dial its own progress.

Timescales
----------
``quick``   A 4-hour server bucket. Small enough that a player who opens the
            game twice a day always meets at least one new quick cycle, which
            is the retention promise the board exists to make.
``daily``   The UTC calendar day.
``weekly``  The ISO week (Monday 00:00 UTC).

Every row belongs to exactly one of those, and its cycle key is part of its
claim identity, so "one claim per objective per cycle" is literally true.

Progress: derived, never asserted
---------------------------------
Each row names a SOURCE -- a named reader over state the SERVER wrote. Two row
kinds:

``threshold``  Done when the source value reaches the target. Needs no
               baseline: "reach a new tower floor" is a fact about now.
``delta``      Done when the source has advanced by the target since the start
               of the cycle. The baseline is stamped on the first read of a
               cycle, so a reset cannot be undone by progress earned before it
               -- the same trick ``missions.py`` uses, and for the same reason.

No request body can move either number. ``add_progress`` on the old module
accepted an ``amount`` and threw it away for exactly this reason.

Persistence
-----------
Board state lives on the player record under ``mission_board`` (see
``STATE_KEY``), so it is persisted by the existing player store with no schema
migration. The writer is always the transaction path: the caller opens
``store.transaction(player_id)``, re-reads the row INSIDE it, mutates the
staged row, and lets the transaction's compare-then-upsert save it.

That last point is load-bearing. ``player_store.py`` hands out DEEP COPIES on
read outside a transaction, so mutating the dict you read back and walking away
discards the write with no error. Writing to a value you read back is
discarded; the row must be staged and saved through the store.

State is also BOUNDED. Each cycle adds a bucket, and only the current cycle's
bucket can ever be read again, so ``board_view`` prunes finished ones (see
``KEPT_PAST_CYCLES``). Without that the blob a returning player carries grows
for as long as they play.

Rewards
-------
Spirit stone amounts are NOT invented here. Each row names an existing mission
id in ``missions.MISSION_DEFINITIONS`` and reuses that mission's
``reward_spirit_stone``, so the board's economy is the economy the game already
ships. The progression material is a real catalog item from
``shared/items/items.json`` -- the same inner-skill materials the goal panel
points at -- at ONE unit per claim. One unit is deliberately not a faucet: a
board row pays less than a single shop purchase of the same item.

Fateum is not a reward anywhere on this board. See ``missions.py`` for the
load-time guard that keeps it out of the ledger too.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from missions import MISSION_DEFINITIONS, mission_def
from multi_currency import CURRENCY_LIST


def _iso_year_week(now: float) -> Tuple[int, int]:
    """(iso_year, iso_week) for a UTC epoch timestamp.

    Explicitly UTC. ``datetime(*gmtime(now)[:6])`` would build a NAIVE datetime
    and then read it in the SERVER's local zone, so a machine running west of
    UTC would label Sunday night as the previous ISO week and turn the weekly
    board over a day early.
    """
    iso = datetime.fromtimestamp(now, tz=timezone.utc).isocalendar()
    return iso[0], iso[1]

#: Player-record key holding the board state. Persisted by player_store as
#: part of extra_state, so no migration is needed.
STATE_KEY = "mission_board"

#: How many past cycle buckets to keep on a player record.
#:
#: A claim's identity is its cycle key, so a bucket is consulted only while its
#: cycle is the current one -- the moment the cycle turns over the bucket is
#: unreachable and nothing in this module ever reads it again. Kept forever it
#: is unbounded growth in the player's ``extra_state`` blob, re-written on every
#: save: measured at 785 buckets / 2774 objective rows / ~129 KB after one
#: simulated year of one board open per day.
#:
#: Two are kept rather than one so a player who was away across a rollover does
#: not find their previous cycle's claim marker already deleted. At most a few
#: hundred bytes.
KEPT_PAST_CYCLES = 2

#: Cycle length in seconds, per timescale.
QUICK_CYCLE_SECONDS = 4 * 60 * 60
DAILY_CYCLE_SECONDS = 24 * 60 * 60
WEEKLY_CYCLE_SECONDS = 7 * 24 * 60 * 60

#: The only currency this board may pay. Fateum is deliberately absent; the
#: load-time guard in missions.py is the second half of that rule.
REWARD_CURRENCY = "spirit_stone"

#: Progression materials, all real catalog items. The keys are the board's
#: tiers; the values are item ids in shared/items/items.json.
TIER_MATERIAL = {
    "quick": "insight_scroll",
    "daily": "body_essence",
    "weekly": "spirit_ink",
}

#: One unit per claim. See the module docstring: this is a nudge toward the
#: inner-skill material the goal panel asks for, not a faucet.
MATERIAL_UNITS = 1


# ----------------------------------------------------------------------
# Sources: named readers over state the SERVER wrote
# ----------------------------------------------------------------------
def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def source_hunt_wins(player: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    """Monsters hunted, from the mission event ledger the hunt system writes."""
    store = ctx.get("mission_store")
    if store is None:
        return 0
    try:
        return _int(store.get_event_count(ctx["player_id"], "hunt"))
    except Exception:
        return 0


def source_combat_wins(player: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    """Fights won, from the same ledger's combat category."""
    store = ctx.get("mission_store")
    if store is None:
        return 0
    try:
        return _int(store.get_event_count(ctx["player_id"], "combat"))
    except Exception:
        return 0


#: The player-row key the LIVE tower writes. ``tower_run.settle_run`` stamps it
#: and it is the only tower state a real settle touches: the older ladder
#: routes in ``tower.py`` write ``tower_state`` instead, and nothing on the
#: paying path ever fills that one in. Reading only ``tower_state`` is why
#: ``quick_tower_floor`` could never leave 0/1.
TOWER_RUN_KEY = "tower_run"
#: The older ladder's key, kept so a player who only ever used ``/tower/clear-floor``
#: is not reported as having done nothing.
TOWER_STATE_KEY = "tower_state"


def _tower_run_total_floors(run: Dict[str, Any]) -> int:
    """Lifetime tower floors cleared, from the live run block.

    ``weekly_best`` maps an ISO week to that week's best run depth and is only
    ever raised, never rewritten or trimmed, so summing it is MONOTONE. The
    ``history`` list is the obvious alternative and is the wrong one: it keeps
    only the last 20 runs (``MAX_RUN_HISTORY``), so the total DROPS mid-cycle as
    old runs fall off the end, and a delta row's progress would fall back
    under the player mid-week.

    Undercounts rather than overcounts when a week contains several runs: it
    counts each week's best depth, not every floor of every run. A mission
    board must never report progress the player did not earn.
    """
    weekly = run.get("weekly_best")
    if not isinstance(weekly, dict):
        return 0
    total = 0
    for depth in weekly.values():
        total += _int(depth)
    return total


def source_tower_clears(player: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    """Tower floors cleared, lifetime.

    The live run block is the source of truth. The older ladder's
    ``total_clears`` is a DIFFERENT measure of the same career, so it is used
    only when it is the larger of the two: whichever system the player actually
    used, the board reflects their floors and never fewer.
    """
    run = player.get(TOWER_RUN_KEY)
    live = _tower_run_total_floors(run) if isinstance(run, dict) else 0
    state = player.get(TOWER_STATE_KEY)
    legacy = _int(state.get("total_clears")) if isinstance(state, dict) else 0
    return max(live, legacy)


def source_tower_floor(player: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    """Highest tower floor reached, lifetime.

    ``tower_run`` records this as ``best_ever``: the deepest a single run has
    ever gone. That is the same thing the older ladder calls ``highest_floor``,
    so both are read and the larger wins.
    """
    run = player.get(TOWER_RUN_KEY)
    live = _int(run.get("best_ever")) if isinstance(run, dict) else 0
    state = player.get(TOWER_STATE_KEY)
    legacy = _int(state.get("highest_floor")) if isinstance(state, dict) else 0
    return max(live, legacy)


def source_qi_progress(player: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    """Qi banked toward the next breakthrough (the 100-point gate)."""
    return _int(player.get("progress"))


def source_pvp_wins(player: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    """Duels won, written by the PvP route."""
    return _int(player.get("pvp_wins"))


def source_beasts_owned(player: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    """Spirit beasts tamed."""
    beasts = player.get("beasts")
    return len(beasts) if isinstance(beasts, (list, dict)) else 0


def source_realm_ordinal(player: Dict[str, Any], ctx: Dict[str, Any]) -> int:
    """Realm position on the cultivation ladder. 0 is the starting realm.

    Normalised through the cultivation config's own realm list rather than an
    ordered copy written here, so a realm added to that table is counted
    without touching this file.
    """
    from cultivation_config import REALMS

    raw = player.get("cultivation_realm")
    text = str(raw or "").strip().lower()
    if not text:
        return 0
    slug = "_".join(text.split())
    for index, realm in enumerate(REALMS):
        if text == realm.name or slug == realm.name:
            return index
    return 0


#: Every source the board may name. A row referring to anything else is a bug
#: caught at import time by ``_validate_definitions`` below.
SOURCES: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], int]] = {
    "hunt_wins": source_hunt_wins,
    "combat_wins": source_combat_wins,
    "tower_clears": source_tower_clears,
    "tower_floor": source_tower_floor,
    "qi_progress": source_qi_progress,
    "pvp_wins": source_pvp_wins,
    "beasts_owned": source_beasts_owned,
    "realm_ordinal": source_realm_ordinal,
}


# ----------------------------------------------------------------------
# The board
# ----------------------------------------------------------------------
#: ``spirit_stone_from`` names an EXISTING mission in MISSION_DEFINITIONS; the
#: board reuses that mission's reward_spirit_stone rather than declaring a
#: number of its own. Changing the game's mission economy moves the board with
#: it, and the board cannot become a second, disagreeing economy.
BOARD_OBJECTIVES: List[Dict[str, Any]] = [
    # --- quick: doable in a minute, new every 4 hours -----------------
    {"objective_id": "quick_hunt_one",
     "cycle": "quick", "kind": "delta", "source": "hunt_wins", "target": 1,
     "unit": "monster", "points_at": "hunt",
     "description": "Hunt one monster in the wild",
     "spirit_stone_from": "daily_claim_event"},
    {"objective_id": "quick_qi_ready",
     "cycle": "quick", "kind": "threshold", "source": "qi_progress", "target": 100,
     "unit": "qi", "points_at": "cultivation",
     "description": "Bank enough qi to attempt a breakthrough",
     "spirit_stone_from": "daily_claim_event"},
    {"objective_id": "quick_tower_floor",
     "cycle": "quick", "kind": "threshold", "source": "tower_floor", "target": 1,
     "unit": "floor", "points_at": "tower",
     "description": "Reach tower floor one",
     "spirit_stone_from": "daily_claim_event"},

    # --- daily: the day's work ----------------------------------------
    {"objective_id": "daily_hunt_five",
     "cycle": "daily", "kind": "delta", "source": "hunt_wins", "target": 5,
     "unit": "monsters", "points_at": "hunt",
     "description": "Hunt 5 monsters in the wild today",
     "spirit_stone_from": "hunt_daily_1001"},
    {"objective_id": "daily_combat_three",
     "cycle": "daily", "kind": "delta", "source": "combat_wins", "target": 3,
     "unit": "fights", "points_at": "combat",
     "description": "Win 3 fights today",
     "spirit_stone_from": "daily_cultivate_1"},
    {"objective_id": "daily_tower_two",
     "cycle": "daily", "kind": "delta", "source": "tower_clears", "target": 2,
     "unit": "floors", "points_at": "tower",
     "description": "Clear 2 tower floors today",
     "spirit_stone_from": "hunt_daily_1002"},
    {"objective_id": "daily_realm_step",
     "cycle": "daily", "kind": "threshold", "source": "realm_ordinal", "target": 1,
     "unit": "realm", "points_at": "cultivation",
     "description": "Reach the second realm",
     "spirit_stone_from": "daily_cultivate_1"},

    # --- weekly: the week's work --------------------------------------
    {"objective_id": "weekly_hunt_twenty",
     "cycle": "weekly", "kind": "delta", "source": "hunt_wins", "target": 20,
     "unit": "monsters", "points_at": "hunt",
     "description": "Hunt 20 monsters in the wild this week",
     "spirit_stone_from": "hunt_weekly_2001"},
    {"objective_id": "weekly_tower_ten",
     "cycle": "weekly", "kind": "delta", "source": "tower_clears", "target": 10,
     "unit": "floors", "points_at": "tower",
     "description": "Clear 10 tower floors this week",
     "spirit_stone_from": "weekly_defeat_5"},
    {"objective_id": "weekly_pvp_three",
     "cycle": "weekly", "kind": "delta", "source": "pvp_wins", "target": 3,
     "unit": "duels", "points_at": "pvp",
     "description": "Win 3 duels this week",
     "spirit_stone_from": "weekly_defeat_5"},
    {"objective_id": "weekly_beast_one",
     "cycle": "weekly", "kind": "threshold", "source": "beasts_owned", "target": 1,
     "unit": "beast", "points_at": "beasts",
     "description": "Tame a spirit beast",
     "spirit_stone_from": "hunt_weekly_2002"},
]


class MissionBoardError(ValueError):
    """A board definition or claim that cannot be honoured."""


def _validate_definitions() -> None:
    """Refuse a bad board at import time, not at claim time.

    A definition naming an unknown source, an unknown timescale, a negative
    target, a currency the board may not pay, or a mission id that does not
    exist all raise HERE. The alternative -- discovering it when a player
    presses Claim -- is a 500 on somebody's dashboard.
    """
    seen = set()
    for row in BOARD_OBJECTIVES:
        oid = row.get("objective_id")
        if not oid:
            raise MissionBoardError("board row without an objective_id")
        if oid in seen:
            raise MissionBoardError(f"duplicate board objective_id {oid!r}")
        seen.add(oid)
        if row.get("cycle") not in ("quick", "daily", "weekly"):
            raise MissionBoardError(f"{oid}: unknown cycle {row.get('cycle')!r}")
        if row.get("kind") not in ("delta", "threshold"):
            raise MissionBoardError(f"{oid}: unknown kind {row.get('kind')!r}")
        if row.get("source") not in SOURCES:
            raise MissionBoardError(f"{oid}: unknown source {row.get('source')!r}")
        target = row.get("target")
        if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
            raise MissionBoardError(f"{oid}: target must be a positive integer")
        if row.get("spirit_stone_from") and mission_def(row["spirit_stone_from"]) is None:
            raise MissionBoardError(
                f"{oid}: spirit_stone_from {row['spirit_stone_from']!r} is not a real mission")
        # The board may not pay a currency it has no table entry for, and may
        # never pay fateum. Matched on the REWARD KEY PREFIX, not on the exact
        # string "fateum": the definitions spell it `reward_fateum`, so a test
        # for `"fateum" in row` passes clean while a fateum reward sits right
        # there in the row. Any key mentioning fateum is refused.
        if REWARD_CURRENCY not in CURRENCY_LIST:
            raise MissionBoardError(f"{REWARD_CURRENCY} is not a real currency")
        fateum_keys = [k for k in row if "fateum" in str(k).lower()]
        if fateum_keys:
            raise MissionBoardError(
                f"{oid}: the board may not declare a fateum reward (found {fateum_keys})")
        material = TIER_MATERIAL.get(row["cycle"])
        if material is None:
            raise MissionBoardError(f"{oid}: no material for cycle {row['cycle']!r}")


_validate_definitions()


def objective_def(objective_id: str) -> Optional[Dict[str, Any]]:
    """The definition for `objective_id`, or None. One lookup, every caller."""
    return next((r for r in BOARD_OBJECTIVES if r["objective_id"] == objective_id), None)


# ----------------------------------------------------------------------
# Cycles: server-timed, with a countdown the client can show
# ----------------------------------------------------------------------
def cycle_key(cycle: str, now: Optional[float] = None) -> str:
    """The identity of the cycle `now` falls in. Part of the claim identity."""
    if now is None:
        now = time.time()
    if cycle == "quick":
        return "q%d" % int(now // QUICK_CYCLE_SECONDS)
    if cycle == "daily":
        return "d%d" % int(now // DAILY_CYCLE_SECONDS)
    if cycle == "weekly":
        # True ISO year+week, so the board turns over on Monday 00:00 UTC like
        # a player expects. An epoch/604800 division would drift against the
        # calendar across a year boundary; tm_yday//7 is a rough approximation
        # that is wrong for the first days of January. isocalendar() is exact.
        iso_year, iso_week = _iso_year_week(now)
        return "w%d-%02d" % (iso_year, iso_week)
    raise MissionBoardError(f"unknown cycle {cycle!r}")


def seconds_to_refresh(cycle: str, now: Optional[float] = None) -> int:
    """Whole seconds until the board turns over for this timescale."""
    if now is None:
        now = time.time()
    if cycle == "quick":
        period = QUICK_CYCLE_SECONDS
    elif cycle == "daily":
        period = DAILY_CYCLE_SECONDS
    elif cycle == "weekly":
        period = WEEKLY_CYCLE_SECONDS
    else:
        raise MissionBoardError(f"unknown cycle {cycle!r}")
    if cycle == "weekly":
        # Monday 00:00 UTC, counted from the calendar.
        #
        # `time.gmtime().tm_wday` already numbers MONDAY as 0 (the C convention),
        # so it is used directly. The earlier `((tm_wday + 6) % 7)` "convert to
        # ISO" shuffle treated Monday as day 6 and pushed every weekly countdown
        # six days out: a player opening the board on Monday was told to come
        # back in six days for a board that would turn over in seven.
        moment = datetime.fromtimestamp(now, tz=timezone.utc)
        seconds_into_week = moment.weekday() * 86400 + moment.hour * 3600 \
            + moment.minute * 60 + moment.second
        return int(max(1, WEEKLY_CYCLE_SECONDS - seconds_into_week))
    elapsed = now % period
    return int(max(1, period - elapsed))


# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------
def default_state() -> Dict[str, Any]:
    """A board state for a player who has never opened the board."""
    return {"cycles": {}}


def get_state(player: Dict[str, Any]) -> Dict[str, Any]:
    """The player's board state, created if absent. Tolerant of a partial save."""
    raw = player.get(STATE_KEY)
    if not isinstance(raw, dict):
        raw = {}
        player[STATE_KEY] = raw
    if not isinstance(raw.get("cycles"), dict):
        raw["cycles"] = {}
    return raw


def _cycle_bucket(state: Dict[str, Any], key: str) -> Dict[str, Any]:
    cycles = state["cycles"]
    bucket = cycles.get(key)
    if not isinstance(bucket, dict):
        bucket = {}
        cycles[key] = bucket
    return bucket


def _prune_stale_cycles(state: Dict[str, Any], now: float) -> None:
    """Drop cycle buckets that can never be read again. Called from board_view.

    The board's own state is the only thing that grows here, and nothing ever
    reads it back: an objective's claim identity is (objective_id, cycle_key),
    and only the CURRENT cycle_key can ever be produced by ``cycle_key``, so a
    bucket for a finished cycle is unreachable by construction.

    Left in place they accumulate one bucket per cycle forever -- measured at
    785 buckets / 2774 objective rows / ~129 KB after one simulated year of one
    board open per day, all of it rewritten on every player save.

    Ordering is by INSERTION order, not by parsing the key back into a
    timestamp: the keys are opaque strings ("q123", "d456", "w2026-40") and a
    lexicographic sort of mixed prefixes would order them by prefix, not by age.
    Insertion order is the order the cycles were first seen, which is the age.
    The CURRENT buckets are pinned regardless of position, so a player who
    returns mid-cycle can never lose the bucket they are being served.
    """
    cycles = state["cycles"]
    live = {cycle_key(row["cycle"], now) for row in BOARD_OBJECTIVES}
    stale = [key for key in cycles if key not in live]
    if len(stale) <= KEPT_PAST_CYCLES:
        return
    for key in stale[: len(stale) - KEPT_PAST_CYCLES]:
        del cycles[key]


def _reward_for(row: Dict[str, Any]) -> Dict[str, Any]:
    """The reward for a row, assembled from EXISTING tables only."""
    mission = mission_def(row.get("spirit_stone_from") or "")
    stone = _int(mission.get("reward_spirit_stone")) if mission else 0
    return {
        REWARD_CURRENCY: stone,
        "material": {"item_id": TIER_MATERIAL[row["cycle"]], "quantity": MATERIAL_UNITS},
    }


def board_view(player: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None,
               now: Optional[float] = None) -> Dict[str, Any]:
    """The whole board for one player, with progress, claim state, countdown.

    MUTATES `player` only to stamp the baseline of a cycle seen for the first
    time. That stamp is the only write, and the caller must be holding a
    transaction for it to reach disk -- see the module docstring.
    """
    if now is None:
        now = time.time()
    ctx = dict(ctx or {})
    ctx.setdefault("player_id", player.get("id"))
    state = get_state(player)
    # A finished cycle's bucket is unreachable, so it is dead weight in a blob
    # the player store rewrites on every save. Pruned before the rows are read
    # so a long-lived record does not grow without bound.
    _prune_stale_cycles(state, now)

    entries: List[Dict[str, Any]] = []
    # A player who has NEVER opened the board has no cycle history, so their
    # first view cannot distinguish "did nothing this cycle" from "did the work
    # before the board existed". Stamping the current value as the baseline
    # there would report zero for a returning player who has been playing for a
    # week, which is precisely the player the board exists to reward: they open
    # the game, see a board where nothing is done, and leave.
    #
    # So the FIRST EVER view baselines every delta row at zero. Progress
    # earned before the board existed counts. Every later cycle stamps on
    # first sight, which is what stops a reset being refilled by old events.
    first_ever_view = not state["cycles"]
    for row in BOARD_OBJECTIVES:
        source = SOURCES[row["source"]]
        key = cycle_key(row["cycle"], now)
        bucket = _cycle_bucket(state, key)
        try:
            value = _int(source(player, ctx))
        except Exception:
            # A source that cannot read its state reports zero rather than
            # taking the whole board down with it.
            value = 0

        # This objective's record for this cycle, created on first sight.
        #
        # The membership test is on the objective's OWN entry, never on the
        # cycle bucket. Testing the bucket re-stamps the baseline on every
        # read, and then progress is always value - value = 0: a board that
        # renders a row which can never complete.
        record = bucket.get(row["objective_id"])
        if not isinstance(record, dict):
            # A threshold row carries no baseline; only a delta row does.
            if row["kind"] == "threshold":
                record = {"claimed": False}
            else:
                record = {"baseline": 0 if first_ever_view else value, "claimed": False}
            bucket[row["objective_id"]] = record

        if row["kind"] == "threshold":
            progress = min(row["target"], value)
        else:
            progress = max(0, min(row["target"], value - _int(record.get("baseline"))))
        claimed = bool(record.get("claimed"))
        entries.append({
            "objective_id": row["objective_id"],
            "cycle": row["cycle"],
            "cycle_key": key,
            "kind": row["kind"],
            "description": row["description"],
            "points_at": row["points_at"],
            "unit": row["unit"],
            "target": row["target"],
            "progress": progress,
            "completed": progress >= row["target"],
            "claimed": claimed,
            "reward": _reward_for(row),
            "seconds_to_refresh": seconds_to_refresh(row["cycle"], now),
        })

    return {
        "ok": True,
        "objectives": entries,
        "server_time": now,
        "cycles": {
            "quick": {"key": cycle_key("quick", now),
                      "seconds_to_refresh": seconds_to_refresh("quick", now)},
            "daily": {"key": cycle_key("daily", now),
                      "seconds_to_refresh": seconds_to_refresh("daily", now)},
            "weekly": {"key": cycle_key("weekly", now),
                       "seconds_to_refresh": seconds_to_refresh("weekly", now)},
        },
    }


def claimable(player: Dict[str, Any], objective_id: str,
              ctx: Optional[Dict[str, Any]] = None,
              now: Optional[float] = None) -> int:
    """0 when the row may be claimed, or a refusal reason code.

    `ctx` is REQUIRED in practice and is forwarded to ``board_view``: a source
    that reads the mission event ledger needs the store, and calling this with
    no context reports that source as zero -- so a completed hunt objective
    reads "not complete" and the claim is refused for a player who did the
    work. The default exists only so the pure-logic tests can pass a player
    whose sources do not need a store.

    Pure with respect to money: it mutates nothing beyond the baseline stamp
    ``board_view`` already performs, and pays nothing.
    """
    view = board_view(player, ctx=ctx, now=now)
    for entry in view["objectives"]:
        if entry["objective_id"] == objective_id:
            if entry["claimed"]:
                return "already_claimed"
            if not entry["completed"]:
                return "not_complete"
            return 0
    return "unknown_objective"


def claim_key(player_id: str, objective_id: str, key: str) -> str:
    """The WALLET idempotency key for one claim.

    Keyed by cycle, which is what makes "once per objective per cycle" true at
    the wallet: the same objective in the next cycle is a different key and
    pays again, while any number of retries inside one cycle collapse onto the
    same key and pay once.
    """
    return f"mission_board:{player_id}:{objective_id}:{key}"


def record_claim(player: Dict[str, Any], objective_id: str, now: Optional[float] = None) -> str:
    """Mark the row claimed in THIS cycle. Returns the cycle key.

    The caller must have already refused a second claim; this is the durable
    marker, staged on the player row inside a transaction.
    """
    if now is None:
        now = time.time()
    row = objective_def(objective_id)
    if row is None:
        raise MissionBoardError(f"unknown objective {objective_id!r}")
    key = cycle_key(row["cycle"], now)
    state = get_state(player)
    bucket = _cycle_bucket(state, key)
    entry = bucket.get(objective_id)
    if not isinstance(entry, dict):
        entry = {"baseline": 0, "claimed": False}
        bucket[objective_id] = entry
    entry["claimed"] = True
    return key


def reward_for(row: Dict[str, Any]) -> Dict[str, Any]:
    """Public alias of the internal reward builder, for the claim route.

    Same function, public name: a route that has to reach into a private helper
    is a route that cannot be checked by the same tests as the rest.
    """
    return _reward_for(row)


def outcome_for(objective_id: str, key: str, reward: Dict[str, Any],
                already: bool = False) -> Dict[str, Any]:
    """The client-facing shape of a claim. One place, so both arms agree."""
    return {
        "ok": not already,
        "objective_id": objective_id,
        "cycle_key": key,
        "already_claimed": already,
        "reward": {} if already else reward,
    }
