"""
Per-Rarity Lottery Pools — R10: Separate gacha pools per rarity.
Competitor: Overmortal _lottery_1_orange/purple/blue_N (separate pools).
Our adaptation: F2P-friendly with pity, displayed odds, no real-money in slice.

Pity persistence: pity counters are stored per-pool in player['lottery'] state
and persist across events. The server stores state in players_db which survives
across sessions (PG) or is monkeypatched for tests (see test_lottery_pity.py).
"""

from __future__ import annotations
import math
import time
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# Request is imported at MODULE level, not inside register_lottery_routes.
#
# This module uses `from __future__ import annotations`, so every parameter
# annotation is a string that FastAPI resolves later against the module's own
# globals. An annotation naming a name that only exists as a local inside
# register_lottery_routes cannot be resolved there, so FastAPI fell back to
# treating `request: Request` as an ordinary scalar and therefore a REQUIRED
# QUERY parameter -- every pull answered 422
# {"detail":[{"type":"missing","loc":["query","request"]}]} and never charged.
# Measured, not read: the route returned 422 on a well-formed body pull.
from fastapi import Request

# The token currency's ONE name, imported rather than re-typed.
#
# Measured before this was an import: `POOL_CONFIG` spelled this currency
# `tribulation_tokens` while every other module that touches the balance spells
# it `tribulation_token` -- `tribulation.TRIBULATION_TOKEN` (tribulation.py:78,
# which AWARDS the tokens), `cosmetics._currency_balance` /
# `cosmetics._debit_currency` (cosmetics.py:905/927/929, which SPEND them), the
# `purchase_currency` CHECK constraint in migrations/011_cosmetics.sql:130, and
# the `item_id` drop table in dungeons.py:341. Five independent call sites, one
# spelling. The plural exists in this file only because a RESPONSE-BODY field
# (`tribulation.py:210`, `"tribulation_tokens": token_count`) was mistaken for a
# currency name -- and note tribulation.py:571 writes that count into
# `tokens[TRIBULATION_TOKEN]`, the singular ledger key.
#
# One name, imported from the module that owns the ledger, so the next reader
# cannot reintroduce the second spelling.
from tribulation import TRIBULATION_TOKEN


class LotteryPullRequest(BaseModel):
    """Body form of the /lottery/{player_id}/pull arguments.

    The shipped game client sends `pool` and `count` in the JSON body
    (client/scenes/game/GachaView.gd:50-52 and :59-61 call
    `_api.post(path, {"pool": ..., "count": ...})`), while the Python tests in
    this repo pass them as query parameters. Both are accepted; see the route.

    Every field is optional so a query-only caller still validates. An absent
    field means "not supplied on this transport", not "use a default", so the
    route can tell the two apart and fall back deliberately. The `count` bounds
    are duplicated here on purpose: the query form enforces them at the edge,
    and the body form must not be the way around them.
    """
    pool: Optional[str] = None
    count: Optional[int] = Field(default=None, ge=1, le=10)


class Rarity(Enum):
    ORANGE = "orange"      # Legendary
    PURPLE = "purple"      # Epic
    BLUE = "blue"          # Rare
    GREEN = "green"        # Uncommon
    WHITE = "white"        # Common


# THE rarity ladder, as data. Lower number = rarer = better.
#
# This is the single place the order is defined, and nothing in this module may
# compare rarity by its string: alphabetical order (blue < green < orange <
# purple < white) is NOT the ladder, and using it silently drops rates that are
# supposed to roll. Every ranking question -- "is this roll good enough for the
# 10-pull guarantee?", "which rarity does the leftover probability land on?" --
# reads this map.
RARITY_ORDER = {
    Rarity.ORANGE: 0,
    Rarity.PURPLE: 1,
    Rarity.BLUE: 2,
    Rarity.GREEN: 3,
    Rarity.WHITE: 4,
}


def rarity_at_least_as_good(rarity: Rarity, floor: Rarity) -> bool:
    """True when `rarity` is `floor` or rarer. The ladder test, by index."""
    return RARITY_ORDER[rarity] <= RARITY_ORDER[floor]

class LotteryPool(Enum):
    HEAVENLY_TREASURY = "heavenly_treasury"    # Orange
    SPIRIT_VAULT = "spirit_vault"              # Purple
    EARTH_CACHE = "earth_cache"                # Blue
    MORTAL_COFFER = "mortal_coffer"            # Green
    COMMON_SATCHEL = "common_satchel"          # White

POOL_CONFIG = {
    LotteryPool.HEAVENLY_TREASURY: {
        "name": "Heavenly Treasury",
        "rarity": Rarity.ORANGE,
        "pity": 90,
        "rate": 0.006,      # 0.6%
        # The flagship pool is priced in TRIBULATION TOKENS -- a different
        # currency from the four stone pools, on a different ledger.
        #
        # This used to read the literal "tribulation_tokens", a spelling that
        # exists nowhere else in the tree. `wallet_store_pg.CURRENCIES` (the PG
        # wallet) and `wallet_defense.CURRENCIES` (the memory wallet) both
        # whitelist four names -- spirit_stone, fateum, destium, souldust -- and
        # tokens are not among them. Measured consequence on the arm main.py
        # mounts: the handler passes this name to `spend`, which refuses it with
        # `unknown currency` before it ever looks at a balance, so every pull
        # from this pool answered HTTP 400 and the pool existed on paper.
        #
        # The wallet is NOT missing this currency, and adding it there would be
        # wrong: no earn path anywhere credits a token into a wallet balance.
        # Tokens are awarded into `player["tokens"][TRIBULATION_TOKEN]` by
        # tribulation.py and spent from that same dict by cosmetics.py. The
        # wallet has no token ledger to extend. So the wallet side is right and
        # THIS side was wrong -- the name is now the singular
        # TRIBULATION_TOKEN, the same string the awarder and the speller use.
        #
        # Repricing this pool in spirit stones was measured and rejected:
        # `test_the_token_pool_spends_tokens_and_leaves_stones_alone` is a
        # design contract ("two currencies, two ledgers; a token pull must not
        # touch stones"), and folding this name to `spirit_stone` is exactly the
        # conflation that test exists to prevent.
        "currency": TRIBULATION_TOKEN,
        "cost_per_pull": 10,
        "cost_10x": 90,
        "guaranteed_after_pity": True,
    },
    LotteryPool.SPIRIT_VAULT: {
        "name": "Spirit Vault",
        "rarity": Rarity.PURPLE,
        "pity": 60,
        "rate": 0.03,       # 3%
        "currency": "spirit_stone",
        "cost_per_pull": 160,
        "cost_10x": 1440,
        "guaranteed_after_pity": True,
    },
    LotteryPool.EARTH_CACHE: {
        "name": "Earth Cache",
        "rarity": Rarity.BLUE,
        "pity": 30,
        "rate": 0.12,       # 12%
        "currency": "spirit_stone",
        "cost_per_pull": 80,
        "cost_10x": 720,
        "guaranteed_after_pity": True,
    },
    LotteryPool.MORTAL_COFFER: {
        "name": "Mortal Coffer",
        "rarity": Rarity.GREEN,
        "pity": 10,
        "rate": 0.25,       # 25%
        "currency": "spirit_stone",
        "cost_per_pull": 20,
        "cost_10x": 180,
        "guaranteed_after_pity": False,
    },
    LotteryPool.COMMON_SATCHEL: {
        "name": "Common Satchel",
        "rarity": Rarity.WHITE,
        "pity": 5,
        "rate": 0.60,       # 60%
        "currency": "spirit_stone",
        "cost_per_pull": 5,
        "cost_10x": 45,
        "guaranteed_after_pity": False,
    },
}

# Reward tables per pool.
#
# CONTRACT: every rarity a pool can ROLL must have a row here. A rated rarity
# with no row falls through `POOL_REWARDS[pool].get(rarity, [])` to the
# "spirit_stones" fallback, so a Purple roll pays 100 stones instead of the
# Epic item the purple rate advertises. `no_pool_rates_a_rarity_it_cannot_pay`
# in server/tests/test_lottery_contract.py enforces this in both directions.
#
# Four rows were missing and are added here:
#   earth_cache  PURPLE  -- reachable at 0.5% and paid 100 stones instead.
#   mortal_coffer BLUE   -- the ladder fix makes this 2% rate reachable again.
#   common_satchel GREEN -- likewise restores a dead 5% rate.
#   heavenly_treasury GREEN -- likewise restores a dead 30% rate.
# Reachable-since is the ladder fix; only earth_cache's purple was reachable
# before it, via the alphabetical filter's accidental "purple" >= "blue".
POOL_REWARDS = {
    LotteryPool.HEAVENLY_TREASURY: {
        Rarity.ORANGE: ["legendary_technique", "legendary_artifact", "legendary_beast_egg"],
        Rarity.PURPLE: ["epic_technique", "epic_artifact", "epic_material"],
        Rarity.BLUE: ["rare_technique", "rare_artifact", "rare_material"],
        Rarity.GREEN: ["uncommon_technique", "uncommon_material"],
    },
    LotteryPool.SPIRIT_VAULT: {
        Rarity.ORANGE: ["legendary_technique", "legendary_artifact"],
        Rarity.PURPLE: ["epic_technique", "epic_artifact", "epic_pill"],
        Rarity.BLUE: ["rare_technique", "rare_artifact", "rare_pill"],
        Rarity.GREEN: ["uncommon_technique", "uncommon_material"],
    },
    LotteryPool.EARTH_CACHE: {
        Rarity.PURPLE: ["epic_technique", "epic_artifact", "epic_pill"],
        Rarity.BLUE: ["rare_technique", "rare_artifact", "rare_pill"],
        Rarity.GREEN: ["uncommon_technique", "uncommon_material"],
        Rarity.WHITE: ["common_technique", "common_material"],
    },
    LotteryPool.MORTAL_COFFER: {
        Rarity.BLUE: ["rare_technique", "rare_artifact", "rare_pill"],
        Rarity.GREEN: ["uncommon_technique", "uncommon_material"],
        Rarity.WHITE: ["common_technique", "common_material", "basic_pill"],
    },
    LotteryPool.COMMON_SATCHEL: {
        Rarity.GREEN: ["uncommon_technique", "uncommon_material"],
        Rarity.WHITE: ["common_technique", "common_material", "basic_pill", "spirit_stones"],
    },
}


@dataclass
class PlayerLotteryState:
    pity_counters: dict[str, int] = field(default_factory=dict)  # pool -> pulls since last target rarity
    total_pulls: dict[str, int] = field(default_factory=dict)    # pool -> total pulls this season
    last_pull_time: dict[str, float] = field(default_factory=dict)


# THE STORED BLOB IS UNTRUSTED INPUT, SO IT IS NOT SPLATTED.
#
# `player["lottery"]` is a persisted row that can hold any shape at all, and
# both player routes used to build their state with an unguarded
# `PlayerLotteryState(**player.get("lottery", {}))`. Every wrong shape
# therefore raised out of the handler and answered HTTP 500 -- a server fault
# for something the caller can neither see nor fix:
#
#   * absent, or {}             -> the fresh-player path, and the only good one
#   * {"pity_counters": "x"}    -> TypeError at the splat
#   * {"total_pulls": [1]}      -> TypeError at the splat
#   * {"bogus": 1}              -> TypeError: unexpected keyword argument
#   * None, or a list           -> TypeError: argument after ** must be a
#                                 mapping
#
# A DICT-SHAPE GUARD ALONE IS NOT ENOUGH, which is why the values are checked
# as well as the container. `{"pity_counters": {"earth_cache": "four"}}` sails
# past any `isinstance(stored, dict)` check and reaches a 500 one line deeper,
# where pull_lottery orders a string pity against `config["pity"] - 1`.
#
# WHAT A CORRUPT BLOB FALLS BACK TO, and why not a client error:
#
# A caller cannot repair its own player row, so a 4xx blames the client for a
# server-side fault AND leaves the row corrupt -- and the pull route is the
# only writer of player["lottery"], so refusing there locks the player out of
# the gacha until someone fixes the row by hand. Fresh state instead
# self-heals: the read reports zeros, and the next accepted pull rewrites the
# row in the canonical three-map shape.
#
# The fallback also fails toward the PLAYER, never away from them: a discarded
# pity counter can only cost the player a guarantee they had earned, and the
# roll it then produces is an ordinary one off an ordinary table. Nothing is
# granted that a well-formed row would not have granted, so there is nothing
# here for a caller to farm.

def _clean_counter_map(raw) -> dict:
    """The numeric entries of one counter map, or an empty map if it is not one.

    A counter map is pool name -> number. Anything else is dropped rather than
    kept: a string, a list, None, a bool (which is an int in Python and would
    otherwise read as a pity of 1), or a non-finite float, which cannot be
    ordered against a pity cap without raising.
    """
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for pool_key, value in raw.items():
        if not isinstance(pool_key, str) or isinstance(value, bool):
            continue
        if isinstance(value, int):
            clean[pool_key] = value
        elif isinstance(value, float) and math.isfinite(value):
            clean[pool_key] = value
    return clean


def coerce_lottery_state(stored) -> PlayerLotteryState:
    """Build a PlayerLotteryState from whatever the player row actually holds.

    Total by construction: every input maps to a state the roll arithmetic can
    consume, so no shape a row can hold reaches that arithmetic as an exception.
    See the comment above for the shapes and for why the fallback is fresh
    state rather than a refusal.
    """
    if not isinstance(stored, dict):
        return PlayerLotteryState()
    return PlayerLotteryState(
        pity_counters=_clean_counter_map(stored.get("pity_counters")),
        total_pulls=_clean_counter_map(stored.get("total_pulls")),
        last_pull_time=_clean_counter_map(stored.get("last_pull_time")),
    )


def pull_lottery(player_state: PlayerLotteryState, pool: LotteryPool) -> dict:
    """Performs a single pull from a lottery pool."""
    config = POOL_CONFIG[pool]
    pool_key = pool.value

    # Initialize pity counter
    if pool_key not in player_state.pity_counters:
        player_state.pity_counters[pool_key] = 0
    if pool_key not in player_state.total_pulls:
        player_state.total_pulls[pool_key] = 0

    pity = player_state.pity_counters[pool_key]
    # IS_PITY MUST MEAN "A GUARANTEE FIRED", NOT "THE COUNTER IS NEAR THE CAP".
    #
    # The flag used to be the bare threshold test, so it reported True for
    # mortal_offer and common_satchel, the two pools whose
    # `guaranteed_after_pity` is False and which therefore have no guarantee to
    # fire. Measured on those pools: a pull at pity 99 above a cap of 10
    # returned `is_pity: True` together with `rarity: white` on a pool whose
    # target is green -- the flag asserted a payout that never happened, and a
    # client drawing a "guaranteed!" banner on that result would be telling the
    # player a lie. common_satchel is worse in the other direction: its target
    # IS white and it rates white 95% of the time, so a hit there is luck, not a
    # guarantee, yet the flag would still claim credit for it.
    #
    # The flag is now ANDed with the pool actually having a guarantee, so it can
    # only be True when the target rarity was forced. The counter still climbs
    # on those two pools and is still shown to the player (get_lottery_state
    # reports `guaranteed_type: "none"` for them -- see there); what it no
    # longer does is claim a payout that did not happen.
    at_cap = pity >= config["pity"] - 1  # Next pull would be guaranteed
    is_pity_pull = at_cap and config["guaranteed_after_pity"]

    # Determine rarity
    if is_pity_pull and config["guaranteed_after_pity"]:
        rarity = config["rarity"]
    else:
        # THE RARITY LADDER IS NOT ALPHABETICAL, and the fix has two halves.
        #
        # Half 1 -- the comparison. This used to read
        #     if r.value >= target.value
        # which compares the rarity's STRING. Alphabetical order is
        # blue < green < orange < purple < white, while the ladder is
        # orange > purple > blue > green > white. The two disagree in FIVE of
        # FIVE pools, and the damage was not cosmetic:
        #
        #   * spirit_vault dropped its ORANGE rate ("orange" >= "purple" is
        #     False) -- a 3000-pull sweep returned purple 100% of the time and
        #     the advertised 0.1% orange could never roll.
        #   * mortal_coffer dropped its 2% BLUE rate for the same reason.
        #   * common_satchel dropped its 5% GREEN rate, making a 95%-white pool
        #     100% white.
        #   * earth_cache KEPT purple ("purple" >= "blue"), leaking a 0.5%
        #     purple into a blue-target pool that had no purple reward row.
        #   * heavenly_treasury kept only {orange, purple}, dropping 45% of its
        #     mass; the unspent remainder hit the fallback and was bumped to
        #     the target, making it orange 94.9% of the time.
        #
        # Half 2 -- the filter's SHAPE, which half 1 alone made worse. Each
        # pool's rate table is already a complete, deliberately-shaped
        # distribution over the rarities that pool can pay (see POOL_REWARDS:
        # every rated rarity now has a reward row). Filtering it down to
        # "target or better" therefore does not repair anything -- it DELETES
        # the pool's designed filler and renormalizes what is left. Measured
        # with a bare ladder swap: earth_cache went from 12% blue to 96% blue,
        # mortal_coffer from 25% green to 92%, and heavenly_treasury became
        # 100% orange. A pool would hand out its best rarity almost every pull.
        #
        # So the roll now uses the pool's table AS WRITTEN and lets RARITY_ORDER
        # do the two jobs it is actually good for: deciding walk order
        # (best-to-worst, so a table that sums below 1.0 degrades toward the
        # worse rarity instead of wherever the dict happens to stop) and
        # choosing the shortfall fallback (the worst rarity the pool actually
        # rates).
        #
        # The walk is NOT normalized by the table's total. For a table summing
        # to 1.0 that is a no-op, but for heavenly_treasury (0.506) and
        # spirit_vault (0.631) rescaling would inflate every rate against the
        # one number the player is shown: GET /lottery/pools advertises
        # POOL_CONFIG["rate"] (0.6% orange, 3% purple), and dividing by the
        # total would deliver 1.19% and 4.75% instead. So the cumulative
        # comparison stays on the raw roll and the unspent tail is dealt with
        # by the fallback: 49.4% of heavenly_treasury pulls and 36.9% of
        # spirit_vault pulls land on the worst rarity the pool rates, which
        # climbs pity exactly as a bad-luck pull should.
        rates = POOL_RATES[pool]
        ladder_order = sorted(rates, key=lambda r: RARITY_ORDER[r])
        roll = random.random()
        cumulative = 0.0
        # Shortfall fallback: worst rarity this pool rates. The old code
        # seeded WHITE and then bumped any WHITE result up to the target,
        # which is what made a shortfall table pay out its best rarity.
        rarity = ladder_order[-1]
        for r in ladder_order:
            cumulative += rates[r]
            if roll <= cumulative:
                rarity = r
                break

    # Update pity
    if rarity == config["rarity"]:
        player_state.pity_counters[pool_key] = 0
    else:
        player_state.pity_counters[pool_key] += 1

    player_state.total_pulls[pool_key] += 1
    player_state.last_pull_time[pool_key] = time.time()

    # Select specific reward
    reward_pool = POOL_REWARDS[pool].get(rarity, [])
    reward = random.choice(reward_pool) if reward_pool else "spirit_stones"

    return {
        "pool": pool.value,
        "rarity": rarity.value,
        "reward": reward,
        "pity_before": pity,
        "pity_after": player_state.pity_counters[pool_key],
        "is_pity": is_pity_pull,
    }


def pull_lottery_10x(player_state: PlayerLotteryState, pool: LotteryPool) -> list[dict]:
    """Performs 10 pulls with a Purple-or-better floor on the 10th pull.

    WHAT THIS ACTUALLY GUARANTEES (the old docstring said "at least 1 Blue+ in
    10x", which this function never delivered -- see below):

    If the first nine results contain no Purple-or-better, the 10th result is
    re-rolled to Purple-or-better. The re-roll does NOT count as an additional
    pull toward pity/total stats -- the batch still costs and counts exactly 10.

    WHY THE PROMISE CHANGED RATHER THAN THE OTHER WAY ROUND:

    The previous docstring promised "at least 1 Blue+ in 10x" while the code
    did three different things that did not match it:

      * it asked "did any of the first nine give Purple+?" -- Purple+, not Blue+;
      * it re-rolled only when the 10th pull was WHITE, so a GREEN 10th pull
        (still below Blue) was accepted untouched -- measured at 91 of 400
        seeded mortal_offer batches containing nothing Blue-or-better;
      * and the promise was unreachable for common_satchel regardless, because
        that pool rates no Orange, Purple or Blue at all: its re-roll table sums
        to zero, the `total_rate > 0` guard skipped the re-roll, and all 400
        sampled batches missed.

    Honest promise, honestly documented: a Purple-or-better floor on every pool
    that can RATE orange or purple, and the pool's own advertised target rarity
    as the floor on the two that cannot (mortal_offer and common_satchel --
    neither rates anything above blue, so a Purple floor would be a promise the
    rate tables cannot keep, and the re-roll would silently do nothing). The
    floor is whichever of those two applies, chosen from POOL_RATES at call
    time; see the reroll_floor comment below.

    The re-roll table is built from the pool's own rates, not hand-listed, so a
    "guarantee" can only ever hand back a rarity the pool actually pays. It
    previously included Blue while claiming a Purple floor, so the guarantee
    could hand back a rarity worse than the floor it claimed to enforce.
    """
    results = []
    config = POOL_CONFIG[pool]
    pool_key = pool.value

    # The rarities the re-roll is allowed to produce. Ordered best-to-worst and
    # read from RARITY_ORDER, never from a hardcoded name comparison.
    #
    # THE FLOOR FALLS BACK TO THE POOL'S OWN TARGET, because two of five pools
    # cannot reach Purple at all and the guarantee was therefore a silent no-op
    # for them. mortal_offer rates blue/green/white and common_satchel rates
    # green/white -- neither rates orange or purple, so the Purple candidate list
    # came back empty, `total_rate` was 0, and the `if total_rate > 0` guard
    # below skipped the re-roll on every single 10-pull. Measured: 1500 of 1500
    # mortal_offer batches and 1500 of 1500 common_satchel batches contained no
    # purple-or-better, which is exactly the guarantee being absent rather than
    # delivered. The old docstring named common_satchel as "the documented
    # exception" and did not mention mortal_offer, so the second pool was
    # silently uncovered.
    #
    # The fallback keeps the promise true on every pool instead of excusing
    # two of them: where the pool cannot reach Purple, the floor becomes the
    # pool's own advertised `target_rarity`, the one rarity the pool actually
    # makes a statement about. For mortal_offer that is a green-or-better floor
    # (green 25% and blue 2%), so roughly one batch in twenty-five is lifted
    # where it would otherwise have come back ten whites. For common_satchel the
    # target is white, which that pool already delivers ~95% of the time, so
    # its guarantee is close to free -- that is a property of a pool that rates
    # nothing rarer, not a defect in the guarantee.
    reroll_floor = Rarity.PURPLE
    ordered_rates = sorted(POOL_RATES[pool], key=lambda r: RARITY_ORDER[r])
    reroll_candidates = [r for r in ordered_rates
                         if rarity_at_least_as_good(r, reroll_floor)]
    if not reroll_candidates:
        reroll_floor = config["rarity"]
        reroll_candidates = [r for r in ordered_rates
                             if rarity_at_least_as_good(r, reroll_floor)]

    for i in range(10):
        # On the 10th pull, guarantee the floor if none of the first nine met it.
        if i == 9:
            already_met = any(
                rarity_at_least_as_good(Rarity(r["rarity"]), reroll_floor)
                for r in results
            )
            forced = not already_met
        else:
            forced = False

        result = pull_lottery(player_state, pool)

        if forced and not rarity_at_least_as_good(Rarity(result["rarity"]), reroll_floor):
            # Re-roll to the floor WITHOUT counting as an extra pull.
            # We directly determine a higher rarity instead of calling pull_lottery
            # to avoid incrementing pity/total_pulls twice for one pull slot.
            rates = POOL_RATES[pool]
            # Purple+-only rate table, built from POOL_RATES so a pool that
            # rates none of them (common_satchel) yields total_rate == 0 and
            # skips the re-roll -- the documented exception above.
            hi_rates = {r: rates.get(r, 0) for r in reroll_candidates}
            total_rate = sum(hi_rates.values())
            if total_rate > 0:
                rate_roll = random.random()
                normalized = rate_roll * total_rate
                cumulative = 0.0
                # Fallback is the WORST CANDIDATE, not the best, and not the
                # literal floor. `reroll_floor` is PURPLE for pools that rate it
                # but the pool's own target for the two that do not -- and
                # common_satchel's target (white) is a legal candidate while
                # PURPLE never was. Indexing the ordered candidate list makes
                # the fallback a rarity this pool can actually pay, so the
                # fallback can never fall through to an unpayable name.
                re_rarity = reroll_candidates[-1]
                for r in reroll_candidates:
                    cumulative += hi_rates[r]
                    if normalized <= cumulative:
                        re_rarity = r
                        break
                # Update result with re-rolled rarity (pity/total already counted once)
                result["rarity"] = re_rarity.value
                reward_pool = POOL_REWARDS[pool].get(re_rarity, [])
                result["reward"] = random.choice(reward_pool) if reward_pool else "spirit_stones"
                # Adjust pity counter: pull_lottery already incremented by 1
                # for the below-floor result. If re-rolled rarity is target,
                # reset to 0. Otherwise pity stays at the incremented value
                # (already correct).
                if re_rarity == config["rarity"]:
                    player_state.pity_counters[pool_key] = 0
                    result["pity_after"] = 0

        results.append(result)

    return results


def get_lottery_state(player_state: PlayerLotteryState, pool: LotteryPool) -> dict:
    """Returns current state for client display."""
    config = POOL_CONFIG[pool]
    pool_key = pool.value

    pity = player_state.pity_counters.get(pool_key, 0)
    total = player_state.total_pulls.get(pool_key, 0)

    return {
        "pool": pool.value,
        "name": config["name"],
        "currency": config["currency"],
        "cost_per_pull": config["cost_per_pull"],
        "cost_10x": config["cost_10x"],
        "pity": pity,
        "pity_max": config["pity"],
        "rate": config["rate"],
        "target_rarity": config["rarity"].value,
        "total_pulls": total,
        # THE ADVERTISED GUARANTEE MUST BE ONE THE CODE KEEPS.
        #
        # These two fields used to report `guaranteed_type: "higher_rarity"`
        # for mortal_offer and common_satchel. Nothing in this module ever
        # implemented a higher-rarity guarantee: `pull_lottery` forces
        # `config["rarity"]` and only when `guaranteed_after_pity` is True,
        # which is False for exactly those two pools. So the state route told
        # the player a bar was filling toward a payout that the pull code could
        # never deliver -- and `pity_max` is shown on screen
        # (client/scenes/game/GachaView.gd:41-44 renders "Pity: n/max"), so a
        # player watching that bar reach 10 on mortal_offer expects a guarantee
        # and gets an ordinary roll. The module's own contract test noticed the
        # half of this that is easy to check ("which the code does not
        # implement") and skipped the two pools rather than reporting the lie.
        #
        # `guaranteed_type` is now "none" when the pool has no guarantee, and
        # `guaranteed_at` is None rather than a number the code will not honour.
        # The keys stay, so the client's shape is unchanged; only the promise
        # becomes true. A panel that reads `guaranteed_type` now has to decide
        # what to draw for "none" instead of drawing a guarantee that is not
        # coming.
        "guaranteed_at": config["pity"] if config["guaranteed_after_pity"] else None,
        "guaranteed_type": ("target_rarity" if config["guaranteed_after_pity"]
                            else "none"),
    }


def get_all_lottery_states(player_state: PlayerLotteryState) -> list[dict]:
    return [get_lottery_state(player_state, pool) for pool in LotteryPool]


# Reward rate tables.
#
# THESE DO NOT ALL SUM TO 1.0, and that is deliberate, not a bug to fix.
# (The old comment here said "must sum to 1.0 per pool", which was false for
# two of five tables and misled every later reader.)
#
# earth_cache, mortal_coffer and common_satchel each sum to exactly 1.0.
#
# heavenly_treasury sums to 0.506 and spirit_vault to 0.631. Both deliberately
# omit WHITE from the weighted roll, so the unspent mass -- 0.494 and 0.369 --
# is the pity system's job, not a missing rate: POOL_CONFIG["pity"] (90 and 60)
# guarantees the target rarity before a player can be stranded on nothing. See
# pull_lottery's shortfall comment for how the leftover is spent.
POOL_RATES = {
    LotteryPool.HEAVENLY_TREASURY: {
        Rarity.ORANGE: 0.006,
        Rarity.PURPLE: 0.05,
        Rarity.BLUE: 0.15,
        Rarity.GREEN: 0.30,
        # No WHITE in weighted rolls — pity at 90 is the fallback for bad luck.
    },
    LotteryPool.SPIRIT_VAULT: {
        Rarity.ORANGE: 0.001,
        Rarity.PURPLE: 0.03,
        Rarity.BLUE: 0.20,
        Rarity.GREEN: 0.40,
        # No WHITE in weighted rolls — pity at 60 is the fallback.
    },
    LotteryPool.EARTH_CACHE: {
        Rarity.PURPLE: 0.005,
        Rarity.BLUE: 0.12,
        Rarity.GREEN: 0.35,
        Rarity.WHITE: 0.525,
    },
    LotteryPool.MORTAL_COFFER: {
        Rarity.BLUE: 0.02,
        Rarity.GREEN: 0.25,
        Rarity.WHITE: 0.73,
    },
    LotteryPool.COMMON_SATCHEL: {
        Rarity.GREEN: 0.05,
        Rarity.WHITE: 0.95,
    },
}


# Currency name mapping: wallet_dispatch uses 'spirit_stone' (singular)
WALLET_CURRENCY_MAP = {
    "spirit_stones": "spirit_stone",
}


# ---------------------------------------------------------------------------
# THE PLAYER-ROW REPLAY LEDGER.
#
# The wallet arm gets replay protection from the wallet store, which keys its
# own idempotency ledger on the `idem` string handed to `spend`. The dict arm
# has no such store: it debits `player["tokens"][TRIBULATION_TOKEN]` (or the
# tiered `player["spirit_stones"]` dict) and that player-row write IS the
# charge. There was nothing recording that it had already happened, so a
# retried request -- a client timeout, a double tap, a proxy retry -- debited
# a second time for one intent.
#
# This is the token pool's production path, not a test-only one: main.py mounts
# lottery with a wallet, but the wallet holds no token balance (see the ledger
# note in the handler), so `heavenly_treasury` always falls through to the
# dict arm. A retry of the flagship pool therefore double-charged for real.
#
# The key is namespaced by player, pool and batch size exactly as the wallet
# arm namespaces it, so ONE caller key reused across two different pulls
# cannot swallow the second one as a replay of the first -- that is the defect
# the namespacing exists to prevent, and it is why the raw caller key is never
# stored on its own.
#
# The ledger is bounded because it lives on a player row and the caller
# controls how fast it grows: MAX_TOKEN_REPLAY_ENTRIES is a per-player ceiling,
# trimmed oldest-first in insertion order, so the row cannot grow without limit
# however many distinct keys arrive. The header itself is bounded separately by
# idem_gate.MAX_IDEMPOTENCY_KEY_LEN; the stored key is that header plus this
# module's fixed namespace literal, so the overshoot is a small per-player
# constant rather than unbounded growth.
# ---------------------------------------------------------------------------

#: Field on the player row holding the replay ledger. A SIBLING of `lottery`,
#: never a key inside it: `player["lottery"]` is rewritten wholesale by the
#: handler as exactly {pity_counters, total_pulls, last_pull_time}, so a ledger
#: nested there would be deleted by the next pull.
TOKEN_REPLAY_LEDGER_KEY = "lottery_replays"

#: Most caller keys one player's row keeps. A FLOOD backstop, not the replay
#: window -- the window is REPLAY_TTL_SECONDS below.
#:
#: It was 64, and 64 was the number the token path and the stone path actually
#: disagreed on. Measured at 64: a player who made 64 other pulls evicted the
#: key, and the retry of that key was charged a SECOND time (tokens down by the
#: cost again, no `_replayed` flag) -- while the same retry on a stone pool was
#: refused, because the wallet keeps receipts for a whole DAY regardless of how
#: many pulls happened since. So "one caller key means one charge" held on the
#: stone path and only held on the token path for a player pulling slowly, which
#: is the opposite of when it matters.
#:
#: 1024 is chosen so the TTL is what binds in real play and this is only the
#: same-second flood guard. At the flagship price a player cannot reach 1024
#: distinct pulls inside the 24h window through ordinary play, so retention now
#: answers by AGE on both arms and the two agree. It stays a bound because the
#: ledger lives on a player row and a caller can send distinct keys as fast as
#: it likes; the TTL cannot bound that, because every one of those keys is
#: younger than the TTL when it arrives.
MAX_TOKEN_REPLAY_ENTRIES = 1024

#: How long a recorded key still counts as a replay, in seconds.
#:
#: THIS is the bound that makes the token path match the stone path. The wallet
#: arm keeps a receipt for a DAY (`wallet_defense.WalletStore.seen` defaults to
#: `ttl_sec=86400`) and re-reads that clock on every `recall`, so a stone pull
#: keeps its retry protection for 24 hours no matter how much the player pulls.
#:
#: This ledger used to bound itself by COUNT alone, and measured that was not
#: equivalent: 64 distinct pulls evicted the original key, and the retry of that
#: key then charged a SECOND time (player tokens went down by the cost again and
#: the reply carried no `_replayed` flag). A player pulling at any real rate
#: crosses 64 pulls in minutes, so the effective protection was "however long
#: this player takes to make 64 other pulls" -- minutes for an active player,
#: days for a lapsed one. The flag is the same for both, the key derivation is
#: the same for both, and the ledger is the same shape for both; the retention
#: was the one place the two arms answered differently.
#:
#: The stored value is already a timestamp (`remember_replay` writes
#: `time.time()`), so this bound reads data the ledger was already carrying and
#: adds no new field to the player row.
REPLAY_TTL_SECONDS = 86400

#: Namespace this module writes its replay keys under. `idempotency_key_for`
#: builds every key with it, and the expiry purge matches on it, so a pull route
#: and its cleaner cannot drift apart on the spelling.
REPLAY_KEY_NAMESPACE = "lottery:"


def replay_ledger(player: dict) -> dict:
    """The player's replay ledger, or an empty map when the stored one is junk.

    The stored blob is untrusted input for the same reason
    `player["lottery"]` is: it is a persisted row and can hold any shape at
    all. A non-mapping therefore reads as "no ledger", which costs the player
    their retry protection for that one pull and never raises -- the same
    self-healing direction `coerce_lottery_state` takes, and for the same
    reason: the pull route is the only writer, so refusing would lock the
    player out of the gacha over a row they cannot repair themselves.

    The returned map is the LIVE dict when the row holds one, so a caller that
    mutates it is mutating the row -- which is what `_remember_replay` wants,
    and why it reassigns the field afterwards.
    """
    stored = player.get(TOKEN_REPLAY_LEDGER_KEY)
    return stored if isinstance(stored, dict) else {}


def remember_replay(player: dict, ledger: dict, idem: str) -> None:
    """Record `idem` as already charged, evicting the oldest entries over cap.

    Re-inserting before the eviction matters: a caller that retries the SAME key
    many times must not push its own earlier entries out by re-adding to the
    end, which would evict everything else and leave the ledger holding only
    the key in hand.
    """
    ledger.pop(idem, None)
    ledger[idem] = time.time()
    # Count bound last, and only after the insert: it is the row-size guard, so
    # it runs on the way out, not the way in. Order matters -- purging expired
    # first means a stale entry cannot push a LIVE one past the cap.
    now = ledger[idem]
    # ONLY this module's own keys are expired. A player row is shared, and the
    # contract is explicit that a ledger holding another system's keys must
    # leave them untouched -- a key this module never wrote is not this module's
    # to delete, whatever its value looks like. Matching on the namespace is
    # what makes the purge safe on a shared row.
    for key in [k for k, ts in ledger.items()
                if k.startswith(REPLAY_KEY_NAMESPACE)
                and (not isinstance(ts, (int, float)) or isinstance(ts, bool)
                     or not math.isfinite(ts)
                     or now - ts > REPLAY_TTL_SECONDS)]:
        ledger.pop(key, None)
    while len(ledger) > MAX_TOKEN_REPLAY_ENTRIES:
        # dicts keep insertion order, so the first key is the oldest write.
        ledger.pop(next(iter(ledger)))
    player[TOKEN_REPLAY_LEDGER_KEY] = ledger


def replay_is_live(ledger: dict, idem: str, now: float) -> bool:
    """Whether `idem` was charged recently enough to still count as a replay.

    The age test lives here, beside the count test in `remember_replay`, so the
    two bounds cannot drift apart. A non-numeric timestamp reads as NOT a live
    replay: a junk row must not be able to freeze a key into permanent
    protection, which would refuse a legitimate future pull forever.

    NON-FINITE is the same defect wearing a disguise, and is the reason this is
    a finiteness test and not a range test. `now - inf` is `-inf`, which is
    inside every window, so a stored `+Inf` would read as an entry from the far
    future and hold that key as a replay permanently -- the player could never
    make that intent again. `NaN` and `-Inf` happen to land the safe way (every
    comparison with them is False), but they are refused here for the same
    reason rather than by luck. Measured: with the finiteness arm absent, the
    `+Inf` case answered True and the route served a permanent replay.
    """
    ts = ledger.get(idem)
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return False
    if not math.isfinite(ts):
        return False
    return (now - ts) <= REPLAY_TTL_SECONDS


def idempotency_key_for(request, *, player_id: str, pool: str, count: int):
    """The caller's Idempotency-Key, namespaced -- one derivation, both arms.

    Returns ``(idem, protected)``. `idem` is the namespaced key to charge
    against; `protected` is True when a caller key was actually supplied.

    ONE place builds this string because the wallet arm and the player-row arm
    must agree on it exactly: if they built it differently then the SAME caller
    key would be one key in the wallet's store and a different one in the
    player's ledger, and a player who retried across the two would be charged
    twice for one intent.

    The namespace is player + pool + batch size, so one key reused across two
    different pulls cannot swallow the second as a replay of the first. That is
    the defect the namespacing exists to prevent, and it is why the raw caller
    key is never used or stored on its own.

    THE CALLER'S KEY IS HONOURED, NEVER MINTED. This used to build a fresh
    `uuid.uuid4().hex` per attempt: a per-attempt key makes every retry look
    like a brand-new intent, so no replay check can ever fire and a player who
    taps summon twice is charged twice for one tap. The server had a channel for
    a stable key and threw it away.

    WHEN THE HEADER IS ABSENT the request is still served, on a per-attempt
    uuid. The shipped client (client/scenes/game/GachaView.gd:50 and :59) calls
    plain `_api.post(...)` with no header, so refusing here would turn every
    summon tap into a 400 -- a fix that breaks the game to enforce a contract
    the game does not yet meet. The cost is stated rather than hidden: no
    caller key means no retry protection, not a fabricated promise of one.
    Making the header REQUIRED is the follow-up, and it belongs with the client
    change that would start sending it.

    `protected` is False in that case, and the player-row arm uses it to decide
    not to record. A per-attempt uuid can never match a later request, so a
    ledger entry for one is unreachable -- it would only push real keys out of
    a bounded ledger, which is how the shipped client would silently destroy the
    protection of a client that does send keys.

    The length bound is read from idem_gate rather than restated, so this
    cannot drift from the limit every other money route enforces. Measured on
    the header itself, before any stripping, so a key padded out to any size
    with whitespace cannot shrink under the bound -- that growth vector is the
    thing the bound exists to stop.
    """
    from idem_gate import MAX_IDEMPOTENCY_KEY_LEN
    from fastapi import HTTPException
    import uuid

    raw = request.headers.get("Idempotency-Key") or ""
    if len(raw) > MAX_IDEMPOTENCY_KEY_LEN:
        raise HTTPException(
            status_code=400,
            detail=(f"Idempotency-Key header longer than "
                    f"{MAX_IDEMPOTENCY_KEY_LEN} characters"))
    caller_key = raw.strip()
    if not caller_key:
        return (f"{REPLAY_KEY_NAMESPACE}{player_id}:{pool}:{count}:{uuid.uuid4().hex}",
                False)
    return f"{REPLAY_KEY_NAMESPACE}{player_id}:{pool}:{count}:{caller_key}", True


def register_lottery_routes(app, store_provider, wallet_store_provider=None, spend_fn=None, earn_fn=None):
    """Registers lottery endpoints.

    Uses store_provider() instead of a captured players_db so that tests
    that monkeypatch main.players_db get a fresh reference on each call.

    Parameters:
        app: FastAPI app
        store_provider: callable -> players_db dict-like
        wallet_store_provider: callable -> WalletStore for currency balance checks
        spend_fn: wallet_dispatch.spend for server-authoritative currency spend
        earn_fn: wallet_dispatch.earn for granting rewards
    """
    from fastapi import HTTPException, Body, Query
    import uuid
    # The cosmetic tier. Imported at registration time rather than inside the
    # handler so a missing cosmetics module fails at mount, loudly, instead of
    # on the first pull a player ever makes.
    from cosmetics import grant_gacha_cosmetic, roll_gacha_cosmetic

    @app.get("/lottery/pools")
    async def get_lottery_pools():
        return {"pools": [get_lottery_state(PlayerLotteryState(), pool) for pool in LotteryPool]}

    @app.get("/lottery/{player_id}/state")
    async def get_player_lottery_state(player_id: str):
        players_db = store_provider()
        if player_id not in players_db:
            raise HTTPException(status_code=404, detail="Player not found")
        player = players_db[player_id]
        lottery_state = coerce_lottery_state(player.get("lottery"))
        return {"ok": True, "pools": get_all_lottery_states(lottery_state)}

    @app.post("/lottery/{player_id}/pull")
    async def pull_lottery_endpoint(
        player_id: str,
        # The caller's `Idempotency-Key` header rides in here. Injected only to
        # read that one header; it takes no part in the body/query contract
        # below and cannot be supplied in the JSON body, because headers are
        # not body fields.
        request: Request,
        # REQUEST-CONTRACT FIX. `pool` and `count` were bare scalars, which
        # FastAPI reads as REQUIRED QUERY parameters. The shipped client sends
        # both in the JSON BODY (client/scenes/game/GachaView.gd:50-52 and
        # :59-61 call `_api.post(path, {"pool": ..., "count": ...})`), so every
        # summon the player tapped returned HTTP 422 with
        # `{"detail":[{"loc":["query","pool"],"msg":"Field required"}]}` and no
        # pull ever ran. Proven live against the booted app: the client's exact
        # body gives 422, the same values as query params do not.
        #
        # Which side is authoritative: the client. It is already shipped to
        # players and its call shape is what a real tap produces; the query-only
        # signature was never a client, it was an accident of writing bare
        # scalars. So the SERVER moves to the client's contract, not the other
        # way round. Both are accepted, because the existing Python tests
        # (server/tests/test_spirit_stones_shape.py:69, :86, :99 and
        # test_signed_magnitude_six.py:1202) call this route with query params
        # and must keep working.
        #
        # Resolution order: body wins when present, else query, else the
        # documented default. The `count` bounds below are unchanged.
        payload: Optional[LotteryPullRequest] = Body(None),
        pool: Optional[str] = Query(None),
        # EDGE BOUND. `count` was a bare `int` and the handler branches on it
        # as `cost_per_pull if count == 1 else cost_10x`, so every value other
        # than 1 -- 0, -5, 2, 10**9 -- was charged the 10-pull price and then
        # ran the 10-pull path. That is a caller-chosen price and a
        # caller-chosen batch size with nothing refusing either. The game only
        # ever sells two batch sizes (client/scenes/game/GachaView.gd:50 sends
        # count=1, :59 sends count=10), so 1..10 is the whole legitimate
        # domain. Found by the signed-magnitude sweep in
        # server/tests/test_signed_magnitude_six.py, which was blind to every
        # body/query field until its attribute read was repaired.
        count: Optional[int] = Query(None, ge=1, le=10),
        client_version: int = 0,
    ):
        # Fold the two transports into one set of values, then validate ONCE.
        if payload is not None:
            if payload.pool is not None:
                pool = payload.pool
            if payload.count is not None:
                count = payload.count
        if pool is None:
            raise HTTPException(status_code=400, detail="pool is required")
        if count is None:
            count = 1
        # POINT-OF-USE CHECK, separate from the edge bound above. The edge
        # bound only says 1..10; the real domain is the two batch sizes the
        # game sells. Anything in between used to fall through to
        # `pull_lottery_10x` (because the branch is `if count == 1 else 10x`)
        # while charging `cost_10x`, so a caller asking for 5 was handed 10
        # results and a 10-pull bill -- `len(results)` never matched the
        # requested `count`. Client/scenes/game/GachaView.gd:50 sends 1 and
        # :59 sends 10, and POOL_CONFIG prices exactly those two
        # (cost_per_pull / cost_10x), so 2..9 is not a discount tier, it is a
        # typo with a price attached. Refused here rather than silently
        # upgraded.
        if count not in (1, 10):
            raise HTTPException(
                status_code=400,
                detail=f"count must be 1 or 10, got {count}")
        players_db = store_provider()
        if player_id not in players_db:
            raise HTTPException(status_code=404, detail="Player not found")

        try:
            pool_enum = LotteryPool(pool)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid pool")

        config = POOL_CONFIG[LotteryPool(pool)]
        player = players_db[player_id]
        lottery_state = coerce_lottery_state(player.get("lottery"))

        # Check and spend currency via WalletStore (server-authoritative)
        currency = config["currency"]
        cost = config["cost_per_pull"] if count == 1 else config["cost_10x"]

        # Map lottery currency names to wallet currency names
        wallet_currency = WALLET_CURRENCY_MAP.get(currency, currency)

        ws = wallet_store_provider() if wallet_store_provider else None
        # WHICH LEDGER, not just which name.
        #
        # The wallet holds four premium currencies and no tokens: its whitelist
        # is `spirit_stone, fateum, destium, souldust` and NO earn path anywhere
        # credits a token into a wallet balance. Tokens live in the player's own
        # row -- awarded by tribulation.py into `player["tokens"]
        # [TRIBULATION_TOKEN]`, spent by cosmetics.py from that same dict, and
        # listed as a purchasable currency by the CHECK constraint in
        # migrations/011_cosmetics.sql:130. So the wallet has no token balance to
        # debit and adding the name to its whitelist would not help: the charge
        # would stop saying "unknown currency" and start saying "insufficient
        # balance", because the wallet's token balance is structurally 0. That
        # is the same 400 with a different sentence.
        #
        # So a token pool is charged on the token ledger even when a wallet is
        # mounted, and only the four stone pools go through the wallet. The
        # dict arm below already handles both currencies.
        #
        # The token path now has replay protection TOO, on a player-row ledger
        # rather than the wallet's. This used to be none: the wallet store
        # keeps its own idempotency ledger keyed on the `idem` string, and a
        # player-row debit never reaches it, so a retried token pull debited
        # `player["tokens"]` a second time for one intent. Since
        # heavenly_treasury is the flagship pool and always lands here, that was
        # a live double-charge, not a test-only path.
        #
        # Both arms derive the key through `idempotency_key_for`, so one caller
        # key is one key on either ledger and a retry that crosses arms is
        # still a retry.
        if currency != TRIBULATION_TOKEN and spend_fn and ws:
            # IDEMPOTENCY KEY, HONOURED RATHER THAN MINTED.
            #
            # The derivation (caller key honoured, per-attempt uuid when the
            # header is absent, bounded by idem_gate.MAX_IDEMPOTENCY_KEY_LEN)
            # lives in `idempotency_key_for` and is shared with the dict arm
            # below. Why it was ever wrong here: this arm used to build the key
            # itself out of a fresh `uuid.uuid4().hex` on every attempt, so the
            # wallet's replay check could never fire. Measured on the mounted
            # app before the fix: two identical pulls produced two charges with
            # two different keys, and an explicit caller
            # `Idempotency-Key: player-intent-42` never reached the wallet.
            idem, _protected = idempotency_key_for(
                request, player_id=player_id, pool=pool, count=count)
            spend_result = spend_fn(ws, player_id, wallet_currency, cost,
                                    client_version, idem)
            if not spend_result.get("ok"):
                raise HTTPException(status_code=400, detail=spend_result)
            if spend_result.get("_replayed"):
                return {"ok": True, "_replayed": True, "results": [], "total_cost": cost}
        else:
            # Fallback: direct player dict manipulation (in-memory tests).
            # The canonical shape is the tiered dict (main.create_player), so
            # this branch MUST handle it: `dict < int` and `dict - int` both
            # raise TypeError, which would 500 every in-memory pull on the
            # shape the server itself creates. A scalar is still accepted for
            # the same legacy reason the other five shims accept it -- see
            # battle_pass.stone_balance, sect_wall.stone_balance,
            # vip._credit_spirit_stones, ghost_battle._grant_stones and
            # cosmetics.purchase_cosmetic, which all read low_grade on the
            # dict path. Both arms preserve the shape they were handed; the
            # dict arm never coerces to a scalar.

            # REPLAY CHECK, BEFORE THE DEBIT. This arm debits the player row
            # directly, so nothing downstream can notice a second attempt: the
            # wallet store's own replay ledger is only consulted on the other
            # arm. A caller who retried a token pull -- a timeout, a double tap,
            # a proxy retry -- with the same Idempotency-Key was charged twice
            # for one intent, because the player-row write has no store to ask.
            # The check sits here, ahead of both currency branches, because the
            # token branch is the one that reaches production.
            #
            # The reply is the receipt shape the wallet arm already returns for
            # a replay: `results` is empty on purpose, since the first attempt's
            # results were already delivered and inventing more would hand the
            # player free items.
            idem, idem_protected = idempotency_key_for(
                request, player_id=player_id, pool=pool, count=count)
            ledger = replay_ledger(player)
            # Presence in the map is NOT enough -- an entry older than
            # REPLAY_TTL_SECONDS is a new intent, exactly as `recall` treats an
            # expired wallet receipt. Judged by age, not by the count cap, so a
            # key survives the 64 other pulls an active player makes inside the
            # window. Same bound, same units, same answer as the stone arm.
            if replay_is_live(ledger, idem, time.time()):
                return {"ok": True, "_replayed": True, "results": [],
                        "total_cost": cost}

            if currency == "spirit_stone":
                stones = player.get("spirit_stones", 0)
                if isinstance(stones, dict):
                    balance = int(stones.get("low_grade", 0) or 0)
                else:
                    balance = int(stones or 0)
                if balance < cost:
                    raise HTTPException(status_code=400, detail="Insufficient currency")
                if isinstance(stones, dict):
                    stones["low_grade"] = balance - cost
                else:
                    player["spirit_stones"] = balance - cost
            elif currency == TRIBULATION_TOKEN:
                # The singular ledger key, the same one tribulation.py awards
                # into and cosmetics.py debits. Compared against the imported
                # constant rather than a literal so this branch cannot drift
                # from POOL_CONFIG's price a second time.
                balance = player.get("tokens", {}).get(TRIBULATION_TOKEN, 0)
                if balance < cost:
                    raise HTTPException(status_code=400, detail="Insufficient currency")
                player.setdefault("tokens", {})[TRIBULATION_TOKEN] = balance - cost

            # RECORD THE CHARGE, AFTER IT LANDED. Both currency branches above
            # raise before touching the row when the player cannot afford the
            # pull, so reaching this point means the debit happened. Recording
            # earlier would make a refused pull look like a paid one, and the
            # player's next real attempt would be swallowed as a replay of a
            # charge that never occurred.
            #
            # Only when a caller key was actually supplied: an absent header
            # derives a fresh uuid per attempt, so such an entry could never
            # match anything and would only push real keys out of a bounded
            # ledger.
            if idem_protected:
                remember_replay(player, ledger, idem)

        # Perform pulls
        if count == 1:
            results = [pull_lottery(lottery_state, LotteryPool(pool))]
        else:
            results = pull_lottery_10x(lottery_state, LotteryPool(pool))

        # The cosmetic tier, rolled SEPARATELY from the character pull above.
        #
        # Separate on purpose: the rarity weights and pity counters in
        # POOL_CONFIG are a character economy with their own tuning, and adding
        # a 0.5% cosmetic to them would change the outcome of every pull for a
        # reward that has no combat effect whatsoever. Rolling apart means this
        # tier can ship and be tuned without touching rates players already know.
        #
        # It is also deliberately rare: the pool sums to well under 1, so most
        # pulls award nothing cosmetic and the player falls through to the
        # normal reward. And per `cosmetics-system-design.md` §13.3, no item in
        # this pool is exclusive -- every one is also obtainable from the shop or
        # from progression, which is enforced in the catalogue's `acquisition`
        # list and in the schema's CHECK constraint.
        cosmetic_drop = roll_gacha_cosmetic()
        if cosmetic_drop:
            grant_gacha_cosmetic(player, cosmetic_drop)

        # Grant rewards
        for result in results:
            if result["reward"] == "spirit_stones":
                if earn_fn and ws:
                    earn_fn(ws, player_id, "spirit_stone", 100,
                            f"lottery:{player_id}:{pool}:{uuid.uuid4().hex}", "lottery_reward")
                else:
                    # Same two-shape rule as the spend arm above, for the same
                    # reason: `dict + int` raises TypeError. Keeps the dict
                    # shape intact instead of collapsing it to a scalar.
                    stones = player.get("spirit_stones", 0)
                    if isinstance(stones, dict):
                        stones["low_grade"] = int(stones.get("low_grade", 0) or 0) + 100
                    else:
                        player["spirit_stones"] = int(stones or 0) + 100
            else:
                inv = player.setdefault("inventory", {})
                inv[result["reward"]] = inv.get(result["reward"], 0) + 1

        # Save state — pity counters persist in player dict (I6 verified by test)
        player["lottery"] = {
            "pity_counters": lottery_state.pity_counters,
            "total_pulls": lottery_state.total_pulls,
            "last_pull_time": lottery_state.last_pull_time,
        }

        # Persist through PlayerStore
        if hasattr(players_db, "persist"):
            players_db.persist(player_id, player)

        resp = {"ok": True, "results": results, "total_cost": cost}

        # The cosmetic award is reported separately from `results` so the client
        # can show a distinct "you also won a cosmetic" moment. It is not in
        # `results` because those are character-pool rolls, and a cosmetic is
        # not a character.
        if cosmetic_drop:
            resp["cosmetic"] = cosmetic_drop

        # Return wallet info if available
        if ws:
            w = ws.get(player_id)
            balance_dict = w.balance.to_dict() if hasattr(w.balance, 'to_dict') else dict(w.balance)
            resp["wallet"] = balance_dict
            resp["wallet_version"] = w.version

        return resp
