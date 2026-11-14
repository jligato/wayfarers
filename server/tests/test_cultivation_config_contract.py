"""
CONTRACT TESTS: server/cultivation_config.py

WHAT THIS MODULE IS
-------------------
``server/cultivation_config.py`` is the realm CONFIG TABLE plus nine pure
readers over it. It holds no routes, no store and no database: importing it
touches nothing but ``dataclasses``. Everything below is therefore a direct
function call -- no ``server.main``, no TestClient, no session, no PostgreSQL.

The three claims this file pins, in the order they matter:

1. THE TABLE'S SHAPE IS A CONTRACT. ``StageConfig`` / ``RealmConfig`` field
   names are read by name across the engine and by four routes. Renaming a
   field is a silent break for every caller that already parses it, so the
   field-name SET is frozen here. The VALUES are not frozen -- see below.
2. THE TABLE IS INTERNALLY CONSISTENT. ``get_next_stage`` does
   ``REALMS[realm.order + 1]``, so a realm's ``order`` must equal its list
   index or the ladder silently walks the wrong realm. Every invariant a
   retune must preserve is checked by one reusable checker, and that checker
   is itself proven non-vacuous against a deliberately corrupted copy.
3. THE READERS REFUSE RATHER THAN GUESS. A stage the table cannot price
   raises ``KeyError``; an unpriceable breakthrough rung reports 0.0 chance
   rather than a default. Guessed fallbacks would be indistinguishable from
   tuned ones. The one exception is a key Python cannot hash at all -- a list
   or a dict read from a corrupt stored row -- which is answered as a MISS by
   every reader, because a name that cannot be looked up is not a priceable
   rung and raising would be a crash rather than a report.

FROZEN NUMBERS vs. RELATIONSHIPS
--------------------------------
Frozen: the clamp bounds 0.05/0.95, the doubling rate of tribulation damage
(10.0 * 2**order -- the docstring's own comment names "10, 20, 40, 80, 160,
320"), the two terminal stage names, the alias table's contents. Those are
the formulas, not the balance.

Read at run time as RELATIONSHIPS (rebalancing stays green):
    "the rate falls along the ladder", "the lifespan reader returns the row's
    own number", "a mutated table changes what the readers answer".

SIBLING COVERAGE -- WHAT IS ALREADY OWNED ELSEWHERE
---------------------------------------------------
``server/tests/test_cultivation_contract.py`` covers the ladder walk, the
alias uniformity, the rate/chance trends and the offline clamp against the
engine. ``test_cultivation_remainder_clamp.py`` and
``test_offline_terminal_stage_collectable.py`` own the clamp arithmetic and
the peak/breakthrough 500 regression over the live app.
``test_cultivation_config_nonfinite_guard.py`` owns the ``_finite_float``
guard by mutating TABLE CELLS. ``test_breakthrough.py`` owns the engine-side
end-to-end roll. This file does NOT repeat those; it owns the table's own
invariants, the reader refusal semantics, non-string and out-of-range inputs,
the ``default`` parameter contract, and -- in section 12 -- the
``LEGACY_BONUS_PER_POINT`` contract and the ``legacy_bonus_of`` reader, which
the mutation control below showed NOTHING in this file detected.

Run
---
    tools/wc_pytest.sh server/tests/test_cultivation_config_contract.py
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()
SERVER_DIR = _HERE.parents[1]
REPO_ROOT = _HERE.parents[2]
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Pure module: no store to select. Set anyway so a single-file run cannot pick
# up a database backend from the environment and report unrelated errors.
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")
for _pg in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_pg, None)

import pytest  # noqa: E402

import cultivation_config as cfg  # noqa: E402


# ---------------------------------------------------------------------------
# Frozen expectations
# ---------------------------------------------------------------------------

#: Field NAMES of the two dataclasses. Renaming one breaks every caller that
#: reads it by attribute; adding one is safe.
STAGE_FIELDS = (
    "name",
    "order",
    "progress_required",
    "breakthrough_base_chance",
    "lifespan_days_added",
    "cultivation_rate_per_hour",
)
REALM_FIELDS = (
    "name",
    "order",
    "stages",
    "base_lifespan_days",
    "tribulation_scales_from",
)

#: The two clamp bounds ``calculate_breakthrough_chance`` applies to every
#: input. The formula, so frozen.
CHANCE_FLOOR = 0.05
CHANCE_CEILING = 0.95

#: Tribulation damage is ``10.0 * 2 ** realm_order``; this is the seed the
#: docstring's own comment states.
TRIBULATION_SEED = 10.0

#: The offline window the settlement loop caps at (72h), used to prove the
#: clamp's ceiling is the REMAINDER and not the elapsed time.
OFFLINE_CAP_HOURS = 72.0

#: Flattened (realm, stage) walk order -- the order a player climbs.
LADDER = [(r.name, s.name) for r in cfg.REALMS for s in r.stages]


def _rows():
    """(realm, stage) pairs in climb order."""
    return [(r, s) for r in cfg.REALMS for s in r.stages]


# ---------------------------------------------------------------------------
# 1. Data contracts -- the shape of the table
# ---------------------------------------------------------------------------

def test_the_two_dataclasses_expose_exactly_the_documented_field_names():
    """The wire shape other modules read by attribute. Rename = silent break."""
    assert tuple(f.name for f in dataclasses.fields(cfg.StageConfig)) == STAGE_FIELDS
    assert tuple(f.name for f in dataclasses.fields(cfg.RealmConfig)) == REALM_FIELDS


def test_every_configured_field_carries_the_type_its_own_docstring_declares():
    """Each field's value type matches the range its inline comment promises."""
    for realm, stage in _rows():
        assert isinstance(stage.name, str) and stage.name
        assert isinstance(stage.order, int)
        assert isinstance(stage.progress_required, float)
        # comment: "breakthrough_base_chance: 0.0 - 1.0"
        assert 0.0 <= stage.breakthrough_base_chance <= 1.0
        assert isinstance(stage.lifespan_days_added, int)
        assert isinstance(stage.cultivation_rate_per_hour, float)
        assert isinstance(realm.name, str) and realm.name
        assert isinstance(realm.order, int)
        assert isinstance(realm.base_lifespan_days, int)
        assert isinstance(realm.tribulation_scales_from, int)


def test_REALM_MAP_holds_the_same_objects_REALMS_does():
    """``get_realm`` reads REALM_MAP, so the two must be the same rows.

    A rebuilt map (equal names, fresh objects) would let a test mutate a copy
    and still see the change through the readers.
    """
    for realm in cfg.REALMS:
        assert cfg.REALM_MAP[realm.name] is realm
    assert set(cfg.REALM_MAP) == {r.name for r in cfg.REALMS}


def test_a_realm_row_and_its_stage_rows_are_plain_mutable_dataclasses():
    """Callers build their own rows (tests, future realms); the table is data."""
    row = cfg.StageConfig(
        name="probe", order=0, progress_required=1.0,
        breakthrough_base_chance=0.5, lifespan_days_added=1,
        cultivation_rate_per_hour=1.0,
    )
    assert row == cfg.StageConfig(
        name="probe", order=0, progress_required=1.0,
        breakthrough_base_chance=0.5, lifespan_days_added=1,
        cultivation_rate_per_hour=1.0,
    )
    assert row != cfg.StageConfig(
        name="probe", order=0, progress_required=1.0,
        breakthrough_base_chance=0.5, lifespan_days_added=1,
        cultivation_rate_per_hour=2.0,
    )


# ---------------------------------------------------------------------------
# 2. Config validation -- the invariants a retune must preserve
# ---------------------------------------------------------------------------

def _table_violations(realms):
    """Every internal-consistency rule the table must satisfy.

    Returned as a list of human-readable strings so one failure names the row.
    Used both against the shipped table and against a corrupted copy, which is
    what proves the checker is not vacuous.
    """
    bad = []
    ladder = list(realms)

    # get_next_stage walks REALMS[realm.order + 1], so order MUST be the index.
    for idx, realm in enumerate(ladder):
        if realm.order != idx:
            bad.append(f"{realm.name}: order {realm.order} != list index {idx}")
        if not realm.stages:
            bad.append(f"{realm.name}: no stages configured")
        if realm.base_lifespan_days <= 0:
            bad.append(f"{realm.name}: base lifespan {realm.base_lifespan_days} <= 0")
        if not 0 <= realm.tribulation_scales_from <= realm.order:
            bad.append(
                f"{realm.name}: tribulation_scales_from "
                f"{realm.tribulation_scales_from} outside 0..{realm.order}"
            )
        for s_idx, stage in enumerate(realm.stages):
            tag = f"{realm.name}.{stage.name}"
            if stage.order != s_idx:
                bad.append(f"{tag}: order {stage.order} != stage index {s_idx}")
            if stage.progress_required <= 0:
                bad.append(f"{tag}: progress_required {stage.progress_required} <= 0")
            if not 0.0 < stage.breakthrough_base_chance <= 1.0:
                bad.append(
                    f"{tag}: breakthrough_base_chance "
                    f"{stage.breakthrough_base_chance} outside (0.0, 1.0]"
                )
            if stage.cultivation_rate_per_hour <= 0:
                bad.append(f"{tag}: cultivation_rate_per_hour "
                           f"{stage.cultivation_rate_per_hour} <= 0")
            if stage.lifespan_days_added <= 0:
                bad.append(f"{tag}: lifespan_days_added "
                           f"{stage.lifespan_days_added} <= 0")

    # Names must be unique: REALM_MAP and get_stage both key on them.
    realm_names = [r.name for r in ladder]
    if len(set(realm_names)) != len(realm_names):
        bad.append("duplicate realm names")

    # Climbing must slow down and must get harder, at every one of the 18 rungs.
    flat_rate = [s.cultivation_rate_per_hour for r in ladder for s in r.stages]
    for i in range(len(flat_rate) - 1):
        if not flat_rate[i] > flat_rate[i + 1]:
            bad.append(f"rate does not fall between rung {i} and {i + 1}")
    flat_chance = [s.breakthrough_base_chance for r in ladder for s in r.stages]
    for i in range(len(flat_chance) - 1):
        if not flat_chance[i] > flat_chance[i + 1]:
            bad.append(f"chance does not fall between rung {i} and {i + 1}")

    # Lifespan must rise with the ladder, both the row value and the row plus
    # what its own stages add.
    base = [r.base_lifespan_days for r in ladder]
    for i in range(len(base) - 1):
        if not base[i] < base[i + 1]:
            bad.append(f"base lifespan does not rise between realm {i} and {i + 1}")
    total = [r.base_lifespan_days + sum(s.lifespan_days_added for s in r.stages)
             for r in ladder]
    for i in range(len(total) - 1):
        if not total[i] < total[i + 1]:
            bad.append(f"total lifespan does not rise between realm {i} and {i + 1}")

    # Every alias must land on a name this table actually holds, in EVERY realm.
    config_stage_names = {s.name for r in ladder for s in r.stages}
    for engine_name, config_name in cfg.STAGE_NAME_ALIASES.items():
        if config_name not in config_stage_names:
            bad.append(f"alias {engine_name!r} targets {config_name!r}, "
                       "which no realm defines")
        for realm in ladder:
            if config_name not in {s.name for s in realm.stages}:
                bad.append(f"alias {engine_name!r} unusable in realm "
                           f"{realm.name!r}")

    # The two terminal names must not shadow a configured stage, or the
    # last-stage fallback would silently serve the wrong rate.
    for clash in cfg.TERMINAL_STAGE_NAMES & config_stage_names:
        bad.append(f"terminal name {clash!r} is also a configured stage")

    return bad


def test_the_shipped_table_satisfies_every_invariant():
    """The positive control for the checker below: the live table is clean."""
    violations = _table_violations(cfg.REALMS)
    assert violations == [], "cultivation_config.REALMS invariants broken:\n" + \
        "\n".join(violations)


def test_the_invariant_checker_actually_bites_a_corrupted_table():
    """Negative control: prove ``_table_violations`` is not a vacuous ``[]``.

    Each mutated copy is built here, never written into the module, so the
    shipped table is untouched. If a mutation survives, the rule it targets is
    not actually checked and the positive control above proves nothing.
    """
    def fresh():
        return [
            cfg.RealmConfig(
                name=r.name, order=r.order,
                stages=[cfg.StageConfig(**vars(s)) for s in r.stages],
                base_lifespan_days=r.base_lifespan_days,
                tribulation_scales_from=r.tribulation_scales_from,
            )
            for r in cfg.REALMS
        ]

    # 1. order no longer matches the list index -> get_next_stage would walk
    #    the wrong realm.
    rows = fresh()
    rows[2].order = 5
    assert any("list index" in v for v in _table_violations(rows)), \
        "a realm whose order != its index must be reported"

    # 2. A rate that rises while climbing -> the player gets faster, not slower.
    rows = fresh()
    rows[0].stages[0].cultivation_rate_per_hour = 0.0001
    assert any("rate does not fall" in v for v in _table_violations(rows)), \
        "a non-falling rate ladder must be reported"

    # 3. A stage order that does not match its position.
    rows = fresh()
    rows[1].stages[1].order = 5
    assert any("stage index" in v for v in _table_violations(rows)), \
        "a stage whose order != its index must be reported"

    # 4. An alias pointing at a stage name no realm defines.
    saved = dict(cfg.STAGE_NAME_ALIASES)
    try:
        cfg.STAGE_NAME_ALIASES.clear()
        cfg.STAGE_NAME_ALIASES.update({"mid_late": "midle"})
        assert any("alias" in v for v in _table_violations(fresh())), \
            "a dangling alias must be reported"
    finally:
        cfg.STAGE_NAME_ALIASES.clear()
        cfg.STAGE_NAME_ALIASES.update(saved)

    # 5. A terminal name that collides with a configured stage.
    saved_terminal = cfg.TERMINAL_STAGE_NAMES
    try:
        cfg.TERMINAL_STAGE_NAMES = frozenset({"late", "peak"})
        assert any("terminal name" in v for v in _table_violations(fresh())), \
            "a terminal/configured name clash must be reported"
    finally:
        cfg.TERMINAL_STAGE_NAMES = saved_terminal


def test_the_reader_returns_its_own_rows_value_so_the_table_is_the_single_source():
    """No reader hardcodes a number. Change a row; the reader must follow.

    ``monkeypatch`` restores the attribute, so the shipped table is unchanged
    for the rest of the run.
    """
    realm, stage = cfg.REALMS[0], cfg.REALMS[0].stages[0]

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(stage, "progress_required", 42.5)
        monkey.setattr(stage, "cultivation_rate_per_hour", 3.25)
        monkey.setattr(stage, "breakthrough_base_chance", 0.5)
        monkey.setattr(realm, "base_lifespan_days", 123)

        assert cfg.stage_progress_required(realm.name, stage.name) == 42.5
        assert cfg.stage_rate_per_hour(realm.name, stage.name) == 3.25
        assert cfg.calculate_breakthrough_chance(realm.name, stage.name) == 0.5
        assert cfg.calculate_lifespan(realm.name) == 123.0
    finally:
        monkey.undo()


# ---------------------------------------------------------------------------
# 3. Lookup contracts -- get_realm / get_stage / get_next_stage
# ---------------------------------------------------------------------------

def test_a_known_realm_name_returns_that_exact_row_and_its_stages():
    for realm in cfg.REALMS:
        found = cfg.get_realm(realm.name)
        assert found is realm
        for stage in realm.stages:
            assert cfg.get_stage(realm.name, stage.name) is stage


def test_an_unknown_realm_or_stage_name_answers_none_rather_than_raising():
    """Refusal by absence. A missing name is not a crash and not a default row."""
    assert cfg.get_realm("immortal") is None          # engine realm, no config row
    assert cfg.get_realm("") is None
    assert cfg.get_stage("immortal", "early") is None
    assert cfg.get_stage("mortal", "peak") is None      # terminal, no own row
    assert cfg.get_stage("mortal", "midle") is None     # typo


def test_a_non_string_lookup_key_is_a_miss_not_a_type_error():
    """Callers read this row from player data; a null must not 500 a route.

    Measured: every HASHABLE non-string key is an ordinary miss. ``REALM_MAP``
    is a plain dict, so a key that can be hashed but not found returns None and
    every reader downstream falls back rather than raising.
    """
    for key in (None, 0, 1, 3.5, object(), True, ("mortal",), b"mortal"):
        assert cfg.get_realm(key) is None, key
        assert cfg.get_stage(key, key) is None, key
        assert cfg.calculate_lifespan(key) == 30.0, key
        assert cfg.get_next_stage(key, key) == (None, None), key
        assert cfg.calculate_breakthrough_chance(key, key) == 0.0, key
        assert cfg.stage_progress_required(key, key) == \
            cfg.DEFAULT_STAGE_PROGRESS_REQUIRED, key


def test_an_UNHASHABLE_lookup_key_reads_as_a_miss_rather_than_raising_TypeError():
    """A list or dict key is a corrupt name, so it answers a gap like any other.

    Before this was fixed, a plain dict lookup hashed its argument, so an
    unhashable key raised ``TypeError`` out of ``get_realm`` and out of every
    reader built on it. A corrupt key was the ONLY shape that took a reader
    down: ``get_realm("zzz_unknown")`` already returned ``None``.

    Three sites did it, and all three are covered here because hardening only
    ``get_realm`` would leave the alias lookups and the terminal-name
    membership still crashing:

        REALM_MAP.get(name)          -- the realm lookup itself
        STAGE_NAME_ALIASES.get(name) -- reached from FOUR readers
        name in TERMINAL_STAGE_NAMES -- a frozenset test, which also hashes

    So each reader is called with an unhashable key in EVERY argument position,
    and each must answer the same value its hashable-miss twin answers (the
    sibling test above pins that twin). Equality with the miss is the contract:
    a corrupt name is not a special case that deserves its own answer.
    """
    for key in (["mortal"], {"realm": "mortal"}, ["mortal", "foundation"]):
        assert cfg.get_realm(key) is None, key
        assert cfg.get_realm(key) == cfg.get_realm("zzz_unknown"), key

        # A bad realm name, with the stage name left valid.
        assert cfg.get_stage(key, "early") is None, key
        assert cfg.get_next_stage(key, "early") == (None, None), key
        assert cfg.calculate_lifespan(key) == 30.0, key
        assert cfg.stage_progress_required(key, "early") == \
            cfg.DEFAULT_STAGE_PROGRESS_REQUIRED, key

        # A bad STAGE name with a valid realm: this is the path that reaches
        # STAGE_NAME_ALIASES, which four readers share.
        assert cfg.get_stage("mortal", key) is None, key
        assert cfg.get_next_stage("mortal", key) == (None, None), key
        assert cfg.stage_progress_required("mortal", key) == \
            cfg.DEFAULT_STAGE_PROGRESS_REQUIRED, key
        assert cfg.calculate_breakthrough_chance("mortal", key) == 0.0, key

        # Both positions at once, so neither argument can be the excuse.
        assert cfg.get_stage(key, key) is None, key
        assert cfg.calculate_breakthrough_chance(key, key) == 0.0, key

    # A bad key never reaches a REAL row. Measured with a realm whose own
    # numbers differ from the fallback's, because mortal's base lifespan
    # happens to equal the hardcoded 30.0 and would make this vacuous.
    for key in (["mortal"], {"realm": "mortal"}):
        assert cfg.get_realm(key) is None, key
        assert cfg.get_realm(key) is not cfg.get_realm("mortal"), key
        assert cfg.get_realm(key) is not cfg.get_realm("core"), key
        assert cfg.calculate_lifespan(key) == cfg.calculate_lifespan("zzz_unknown"), key
        assert cfg.calculate_lifespan(key) != cfg.calculate_lifespan("core"), \
            "a corrupt name must not be answered from another realm's row"


def test_the_rate_reader_still_REFUSES_an_unhashable_stage_name_rather_than_inventing_one():
    """The one reader whose contract is to raise keeps raising -- loudly.

    ``stage_rate_per_hour`` documents that a gap is loud when the caller
    supplies no default, because an invented rate would be indistinguishable
    from a tuned one. Hardening the lookups must not quietly turn that refusal
    into a number: the fix removes the ``TypeError`` crash, and this test is
    what proves the deliberate ``KeyError`` survived it.

    The distinction is the whole point. ``TypeError`` is a crash; ``KeyError``
    is a report that names the realm and the stage that could not be priced.
    """
    for key in (["mortal"], {"realm": "mortal"}):
        with pytest.raises(KeyError) as excinfo:
            cfg.stage_rate_per_hour("mortal", key)
        # The message must still name the lookup that failed, so the report
        # survives the hardening.
        assert "mortal" in str(excinfo.value), key

        # A caller-supplied default still rescues it, exactly as it does for
        # any other unpriceable stage.
        assert cfg.stage_rate_per_hour("mortal", key, default=9.0) == 9.0, key
        assert cfg.stage_rate_per_hour(key, "early", default=9.0) == 9.0, key

    # MEASURED: an unhashable name is a MISS, so the caller's default is what comes
    # back -- the same as any other undescribed stage name. It is NOT answered
    # from the row whose string it resembles, which is why the default survives
    # here. A configured row's own rate is untouched by its corrupt neighbour.
    assert cfg.stage_rate_per_hour("mortal", ["early"], default=99.0) == 99.0, \
        "a corrupt name is a miss, so it takes the default rather than a row"
    assert cfg.stage_rate_per_hour("mortal", "early", default=99.0) == \
        float(cfg.REALMS[0].stages[0].cultivation_rate_per_hour)


def test_the_terminal_fallback_never_fires_for_an_unhashable_stage_name():
    """A frozenset membership test hashes its argument, so it was a third crash.

    peak/breakthrough have no configured row and are paid the realm's last
    rate. An unhashable name is not one of those two exact strings, so it must
    take the caller's default instead of being served that rate -- otherwise a
    corrupt key would be quietly answered as a legal game state.
    """
    for key in (["peak"], {"stage": "breakthrough"}, []):
        assert cfg.stage_rate_per_hour("mortal", key, default=7.0) == 7.0, key
        assert cfg.stage_progress_required("mortal", key) == \
            cfg.DEFAULT_STAGE_PROGRESS_REQUIRED, key
        assert cfg.calculate_breakthrough_chance("mortal", key) == 0.0, key
        assert cfg.get_stage("mortal", key) is None, key

    # Control: the two exact names DO still fire, so the passes above are the
    # membership test rejecting the key rather than the branch being dead.
    for realm in cfg.REALMS:
        expected = float(realm.stages[-1].cultivation_rate_per_hour)
        for name in sorted(cfg.TERMINAL_STAGE_NAMES):
            assert cfg.stage_rate_per_hour(realm.name, name) == expected, \
                (realm.name, name)


def test_get_next_stage_walks_every_rung_exactly_once_and_stops_at_the_ceiling():
    """The whole ladder is reachable and the top is a real ceiling."""
    visited = [LADDER[0]]
    realm_name, stage_name = LADDER[0]
    for _ in range(len(LADDER) * 2):
        nxt = cfg.get_next_stage(realm_name, stage_name)
        if nxt == (None, None):
            break
        realm_name, stage_name = nxt
        assert (realm_name, stage_name) not in visited, "the walk looped"
        visited.append((realm_name, stage_name))
    assert visited == LADDER
    assert cfg.get_next_stage(*LADDER[-1]) == (None, None)


def test_get_next_stage_never_returns_a_rung_that_is_not_configured():
    """Whatever it hands back must resolve through ``get_stage``."""
    for realm_name, stage_name in LADDER:
        nxt_realm, nxt_stage = cfg.get_next_stage(realm_name, stage_name)
        if nxt_realm is None:
            assert nxt_stage is None, f"{realm_name}.{stage_name} half-a-step"
            continue
        assert cfg.get_stage(nxt_realm, nxt_stage) is not None, \
            f"{realm_name}.{stage_name} -> ({nxt_realm!r}, {nxt_stage!r})"


def test_a_terminal_stage_name_has_no_configured_row_so_the_walk_cannot_leave_it():
    """``get_next_stage`` applies aliases but not TERMINAL_STAGE_NAMES.

    peak/breakthrough are states a player passes through, so the walk has no
    row to advance from and answers the ceiling. That is the contract; the
    fallback for those names lives only in ``stage_rate_per_hour``.
    """
    for name in sorted(cfg.TERMINAL_STAGE_NAMES):
        assert cfg.get_stage("mortal", name) is None, name
        assert cfg.get_next_stage("mortal", name) == (None, None), name


# ---------------------------------------------------------------------------
# 4. The alias table -- one stage, one spelling, one answer
# ---------------------------------------------------------------------------

def test_every_stage_reader_resolves_an_alias_to_the_same_row_as_its_config_name():
    """The uniformity that was measured broken: four readers, one answer.

    Before the fix the rate and the requirement applied ``STAGE_NAME_ALIASES``
    while the chance and ``get_next_stage`` did not, so one engine-written
    stage read as 1.5 by one caller, 100.0 by a second, 0.0 chance (an
    unbreakthroughable player) by a third and (None, None) (read as "max
    realm") by a fourth. Asserting all four in one test is the point: it fails
    on the FIRST reader that drops the alias.
    """
    assert cfg.STAGE_NAME_ALIASES, "the alias table is the fix; it must exist"
    for engine_name, config_name in cfg.STAGE_NAME_ALIASES.items():
        for realm in cfg.REALMS:
            for reader in (
                cfg.stage_rate_per_hour,
                cfg.stage_progress_required,
                cfg.calculate_breakthrough_chance,
            ):
                assert reader(realm.name, engine_name) == \
                    reader(realm.name, config_name), (realm.name, engine_name)
            assert cfg.get_next_stage(realm.name, engine_name) == \
                cfg.get_next_stage(realm.name, config_name), (realm.name, engine_name)


def test_an_alias_never_shadows_a_name_the_table_configures_directly():
    """If a realm ever spelled the stage itself, the alias must not hijack it."""
    config_stage_names = {s.name for r in cfg.REALMS for s in r.stages}
    assert not (set(cfg.STAGE_NAME_ALIASES) & config_stage_names), \
        "an alias key that is also a configured stage would change a row's reader"


# ---------------------------------------------------------------------------
# 5. stage_progress_required -- the ceiling
# ---------------------------------------------------------------------------

def test_the_default_ceiling_is_a_full_bar_and_every_configured_row_matches_it():
    """The fallback cannot make the gate looser than the rows already require."""
    assert cfg.DEFAULT_STAGE_PROGRESS_REQUIRED == 100.0
    for realm, stage in _rows():
        assert stage.progress_required == cfg.DEFAULT_STAGE_PROGRESS_REQUIRED
        assert cfg.stage_progress_required(realm.name, stage.name) == \
            cfg.DEFAULT_STAGE_PROGRESS_REQUIRED


def test_an_undescribed_stage_falls_back_to_the_ceiling_and_honours_an_override():
    """A gap is covered, not invented: the default is a full bar, and the
    caller may pass its own."""
    for realm_name, stage_name in (("immortal", "early"), ("mortal", "peak"),
                                   ("mortal", "typo"), ("nope", "early")):
        assert cfg.stage_progress_required(realm_name, stage_name) == \
            cfg.DEFAULT_STAGE_PROGRESS_REQUIRED, (realm_name, stage_name)
    assert cfg.stage_progress_required("mortal", "typo", 55) == 55.0
    assert cfg.stage_progress_required("mortal", "early", 55) == 100.0, \
        "an override is a FALLBACK, not an override of a configured row"
    assert isinstance(cfg.stage_progress_required("mortal", "typo", 55), float)


def test_the_signature_default_is_the_module_constant_not_a_copied_literal():
    """A second literal here would drift the moment the ceiling is retuned."""
    import inspect
    default = inspect.signature(cfg.stage_progress_required).parameters["default"].default
    assert default == cfg.DEFAULT_STAGE_PROGRESS_REQUIRED


# ---------------------------------------------------------------------------
# 6. stage_rate_per_hour -- the rate, and the refusal
# ---------------------------------------------------------------------------

def test_the_rate_comes_from_the_row_and_falls_all_the_way_down_the_ladder():
    for realm in cfg.REALMS:
        for stage in realm.stages:
            assert cfg.stage_rate_per_hour(realm.name, stage.name) == \
                float(stage.cultivation_rate_per_hour)
    rates = [cfg.stage_rate_per_hour(r, s) for r, s in LADDER]
    assert all(rates[i] > rates[i + 1] for i in range(len(rates) - 1)), \
        "climbing must slow down at every one of the 18 rungs"


def test_the_two_terminal_stages_pay_the_realms_own_last_configured_rate():
    """peak/breakthrough have no row; they finish the last row the table has."""
    for realm in cfg.REALMS:
        expected = float(realm.stages[-1].cultivation_rate_per_hour)
        for name in sorted(cfg.TERMINAL_STAGE_NAMES):
            assert cfg.stage_rate_per_hour(realm.name, name) == expected, \
                (realm.name, name)


def test_the_terminal_fallback_needs_a_realm_row_and_raises_without_one():
    """``peak`` is terminal everywhere, but immortal has no row to finish."""
    with pytest.raises(KeyError):
        cfg.stage_rate_per_hour("immortal", "peak")


def test_an_unpriceable_stage_raises_and_names_both_the_realm_and_the_stage():
    """The exception is the report. It must say which lookup failed."""
    with pytest.raises(KeyError) as excinfo:
        cfg.stage_rate_per_hour("immortal", "early")
    message = str(excinfo.value)
    assert "immortal" in message and "early" in message, message


def test_a_caller_supplied_default_is_returned_for_a_gap_instead_of_raising():
    """The engine fills the one realm the table lacks; the fill is explicit."""
    assert cfg.stage_rate_per_hour("immortal", "early", default=0.25) == 0.25
    assert cfg.stage_rate_per_hour("mortal", "typo", default=0.5) == 0.5
    assert cfg.stage_rate_per_hour("mortal", "typo", default=0) == 0.0
    assert cfg.stage_rate_per_hour("mortal", "typo", default=2) == 2.0, \
        "an int default comes back as a float"


def test_a_configured_row_is_never_shadowed_by_the_default():
    """The default is a fallback. Passing one must not change a real answer."""
    for realm in cfg.REALMS:
        for stage in realm.stages:
            assert cfg.stage_rate_per_hour(realm.name, stage.name, default=99.0) == \
                float(stage.cultivation_rate_per_hour)


def test_the_default_is_none_so_the_gap_is_loud_by_default():
    """A non-None default here would silently invent a rate for every gap."""
    import inspect
    assert inspect.signature(cfg.stage_rate_per_hour).parameters["default"].default is None


# ---------------------------------------------------------------------------
# 7. calculate_breakthrough_chance -- the roll
# ---------------------------------------------------------------------------

def test_the_chance_is_the_rows_own_base_with_the_two_documented_modifiers():
    """+10% per luck point, +5% per legacy point -- the coefficients are the
    contract; the base is read from the table so retuning survives."""
    LUCK_PER_POINT = 0.1
    LEGACY_PER_POINT = 0.05
    for realm in cfg.REALMS:
        for stage in realm.stages:
            for luck, legacy in ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (2.0, 3.0)):
                expected = (stage.breakthrough_base_chance
                            + luck * LUCK_PER_POINT
                            + legacy * LEGACY_PER_POINT)
                expected = min(CHANCE_CEILING, max(CHANCE_FLOOR, expected))
                assert cfg.calculate_breakthrough_chance(
                    realm.name, stage.name, luck, legacy) == expected, \
                    (realm.name, stage.name, luck, legacy)


def test_the_chance_never_leaves_its_clamp_however_extreme_the_input():
    """No input, including a NaN-shaped one, escapes the 0.05..0.95 band."""
    inputs = (0.0, 1.0, -1.0, 1e12, -1e12, float("inf"), float("-inf"))
    for realm in cfg.REALMS:
        for stage in realm.stages:
            for luck in inputs:
                for legacy in inputs:
                    chance = cfg.calculate_breakthrough_chance(
                        realm.name, stage.name, luck, legacy)
                    assert CHANCE_FLOOR <= chance <= CHANCE_CEILING, \
                        (realm.name, stage.name, luck, legacy, chance)


def test_luck_and_legacy_only_ever_raise_the_chance():
    """The floor is reached by taking them away; the ceiling masks them."""
    for realm in cfg.REALMS:
        for stage in realm.stages:
            base = cfg.calculate_breakthrough_chance(realm.name, stage.name)
            assert cfg.calculate_breakthrough_chance(
                realm.name, stage.name, 1.0, 0.0) >= base
            assert cfg.calculate_breakthrough_chance(
                realm.name, stage.name, 0.0, 1.0) >= base
            assert cfg.calculate_breakthrough_chance(
                realm.name, stage.name, -1.0, -1.0) <= base


def test_a_rung_the_table_cannot_price_reports_zero_chance_not_a_default():
    """0.0 is a refusal to roll, deliberately distinguishable from the 0.05
    floor a real row can reach. An unpriceable rung must never be paid the
    floor, or a gap would read as a merely-hard stage."""
    assert cfg.calculate_breakthrough_chance("immortal", "early") == 0.0
    assert cfg.calculate_breakthrough_chance("mortal", "typo") == 0.0
    assert cfg.calculate_breakthrough_chance("nope", "early") == 0.0
    hardest = min(cfg.calculate_breakthrough_chance(r, s) for r, s in LADDER)
    assert hardest > 0.0, "a real row must never report the refusal value"


# ---------------------------------------------------------------------------
# 8. clamp_offline_progress -- the offline ceiling
# ---------------------------------------------------------------------------

def test_time_alone_would_chain_an_extra_realm_at_the_cap_window():
    """The premise, measured: the cap window outruns a stage at the top rate.

    Without the clamp the settlement loop would pay 144.0 against a 100.0 bar.
    """
    top_rate = max(s.cultivation_rate_per_hour for r in cfg.REALMS for s in r.stages)
    uncapped = top_rate * OFFLINE_CAP_HOURS
    assert uncapped > cfg.DEFAULT_STAGE_PROGRESS_REQUIRED, \
        "the clamp would be untestable if the cap window could never overrun"
    assert cfg.clamp_offline_progress(
        top_rate, OFFLINE_CAP_HOURS * 3600.0, 0.0, cfg.DEFAULT_STAGE_PROGRESS_REQUIRED
    ) == cfg.DEFAULT_STAGE_PROGRESS_REQUIRED


def test_the_grant_is_the_smaller_of_the_earned_amount_and_the_remainder():
    for rate in (0.005, 0.12, 2.0):
        for hours in (0.0, 0.5, 1.0, 7.0, 72.0):
            for current in (0.0, 1.0, 55.5, 80.0, 99.9):
                required = cfg.DEFAULT_STAGE_PROGRESS_REQUIRED
                expected = min(rate * hours, max(0.0, required - current))
                assert cfg.clamp_offline_progress(rate, hours * 3600.0, current,
                                                  required) == expected


def test_a_part_finished_bar_is_capped_at_what_is_left_and_never_goes_negative():
    required = cfg.DEFAULT_STAGE_PROGRESS_REQUIRED
    for current in (required, required + 10.0, required * 2):
        assert cfg.clamp_offline_progress(2.0, 3600.0, current, required) == 0.0
    assert cfg.clamp_offline_progress(2.0, 72 * 3600.0, 80.0, required) == 20.0
    assert cfg.clamp_offline_progress(2.0, 3600.0, 0.0, -5.0) == 0.0, \
        "a nonsense requirement must not mint progress"


def test_a_non_positive_elapsed_window_pays_nothing_and_zero_rate_pays_nothing():
    assert cfg.clamp_offline_progress(2.0, -10.0, 0.0, 100.0) == 0.0
    assert cfg.clamp_offline_progress(2.0, 0.0, 0.0, 100.0) == 0.0
    assert cfg.clamp_offline_progress(0.0, 3600.0, 10.0, 100.0) == 0.0
    assert cfg.clamp_offline_progress(-1.0, 3600.0, 10.0, 100.0) == 0.0


def test_an_absence_shorter_than_the_bar_pays_in_full_and_is_not_reduced():
    """A player away 10 hours earns 10 hours, not the capped remainder."""
    rate = cfg.stage_rate_per_hour("mortal", "early")
    hours = 10.0
    assert cfg.clamp_offline_progress(rate, hours * 3600.0, 0.0, 100.0) == \
        rate * hours


# ---------------------------------------------------------------------------
# 9. calculate_tribulation_damage -- the scaling curve
# ---------------------------------------------------------------------------

def test_tribulation_damage_is_the_seed_doubled_per_realm_order():
    for realm in cfg.REALMS:
        assert cfg.calculate_tribulation_damage(realm.order) == \
            TRIBULATION_SEED * (2 ** realm.order), realm.name
    assert [cfg.calculate_tribulation_damage(i) for i in range(4)] == \
        [10.0, 20.0, 40.0, 80.0]


def test_damage_doubles_across_the_whole_configured_ladder_and_never_flips():
    """Monotone and strictly rising over every configured order."""
    damages = [cfg.calculate_tribulation_damage(r.order) for r in cfg.REALMS]
    assert all(damages[i] < damages[i + 1] for i in range(len(damages) - 1))
    assert all(d > 0 for d in damages)


def test_an_order_the_table_does_not_have_is_still_a_number_not_a_crash():
    """The function is a formula, not a lookup: it must not raise.

    Callers pass ``realm.order`` from stored data, so an out-of-range or
    non-integer value has to come back as a number rather than an exception.
    """
    assert cfg.calculate_tribulation_damage(-1) == TRIBULATION_SEED / 2
    assert cfg.calculate_tribulation_damage(len(cfg.REALMS)) == \
        TRIBULATION_SEED * (2 ** len(cfg.REALMS))
    for order in (-1, 0, 99, 1000):
        assert cfg.calculate_tribulation_damage(order) > 0.0
    assert cfg.calculate_tribulation_damage(2.0) == 40.0, "a float order works too"


# ---------------------------------------------------------------------------
# 10. calculate_lifespan -- the realm's own number
# ---------------------------------------------------------------------------

def test_the_lifespan_is_read_from_the_realm_row_and_is_a_float():
    """A float return, from the row: adding a stage's days is the caller's job."""
    for realm in cfg.REALMS:
        lifespan = cfg.calculate_lifespan(realm.name)
        assert lifespan == float(realm.base_lifespan_days), realm.name
        assert isinstance(lifespan, float), realm.name


def test_a_realm_the_table_lacks_reports_the_mortal_lifespan_not_zero():
    """immortal is an engine realm with no config row. 30.0 days, never 0.0 --
    a zero would age a player out on the spot."""
    fallback = cfg.calculate_lifespan("immortal")
    assert fallback == float(cfg.REALMS[0].base_lifespan_days)
    assert fallback > 0.0
    assert cfg.calculate_lifespan("not_a_realm") == fallback
    assert cfg.calculate_lifespan("") == fallback
    assert cfg.calculate_lifespan(None) == fallback


def test_lifespan_rises_with_the_ladder_both_as_a_row_and_with_its_stages():
    """base_lifespan_days and base + the stages' days must both increase, or a
    higher realm would be a shorter life."""
    base = [cfg.calculate_lifespan(r.name) for r in cfg.REALMS]
    assert all(base[i] < base[i + 1] for i in range(len(base) - 1))
    totals = [r.base_lifespan_days + sum(s.lifespan_days_added for s in r.stages)
              for r in cfg.REALMS]
    assert all(totals[i] < totals[i + 1] for i in range(len(totals) - 1))
    for realm in cfg.REALMS:
        assert realm.base_lifespan_days < \
            realm.base_lifespan_days + sum(s.lifespan_days_added for s in realm.stages), \
            f"{realm.name}: its own stages must add life"


# ---------------------------------------------------------------------------
# 11. MEASURED LIMITS AND GAPS -- read off the module, not assumed
#
# Everything above was written against a symbol list. This section came from
# probing the edges of each reader: feeding it the values a corrupted stored
# row or a dividing-by-zero clock would actually produce (infinity, "not a
# number", an integer where a float is declared, an out-of-range index) and
# recording what came back. Each answer below was MEASURED first and then
# asserted, so a behaviour change has to be a visible diff against this file.
# ---------------------------------------------------------------------------

def test_a_not_a_number_modifier_fails_TO_the_chance_floor_not_to_the_ceiling():
    """The clamp has no NaN branch, and the answer is asymmetric on purpose.

    MEASURED: ``max(0.05, nan)`` is ``0.05`` and ``min(0.95, nan)`` is ``0.95``
    in CPython -- whichever bound is the SECOND argument loses. So a NaN in
    either modifier position lands on the FLOOR: 0.05, the worst roll in the
    game, not the 0.95 ceiling.

    That direction matters. ``calculate_breakthrough_chance`` returning 0.0 for
    a stage it cannot price is the deliberate refusal this module documents; a
    NaN modifier is a DIFFERENT failure -- the stage is real and priced, only
    the player's stat is corrupt -- and it collapses onto the same 0.05 that the
    floor produces for a player with negative luck and no legacy. So a NaN stat
    is indistinguishable from the hardest legal roll rather than from the
    refusal value, which is why it is pinned rather than left to chance.

    Note the previous test in this file says "including a NaN-shaped one" but
    sweeps only infinities. This is that case, measured.
    """
    nan = float("nan")
    assert max(0.05, nan) == 0.05 and min(0.95, nan) == 0.95, \
        "CPython's bound ordering is what makes this test's answer true"

    # Either modifier position, alone or together with a real one.
    assert cfg.calculate_breakthrough_chance("mortal", "early", nan) == CHANCE_FLOOR
    assert cfg.calculate_breakthrough_chance("mortal", "early", 0.0, nan) == CHANCE_FLOOR
    assert cfg.calculate_breakthrough_chance("mortal", "early", nan, 100.0) == CHANCE_FLOOR, \
        "a NaN modifier is not rescued by a large one in the other slot"
    assert cfg.calculate_breakthrough_chance("mortal", "early", 1.0, nan) == CHANCE_FLOOR

    # The hardcoded floor value, not whatever a retune makes the floor: the
    # clamp bounds are frozen at the top of this file.
    assert cfg.calculate_breakthrough_chance("mortal", "early", nan) == 0.05

    # And the floor is never BELOW the hardest real rung, so a corrupt stat is a
    # bad roll rather than a guarantee of failure. Read from the table.
    hardest_real = min(cfg.calculate_breakthrough_chance(r, s) for r, s in LADDER)
    assert hardest_real > CHANCE_FLOOR, \
        "a real rung must sit above the floor a NaN collapses onto"

    # The obvious defensive guard -- refusing a NaN outright -- is NOT what the
    # module does, and this line says so explicitly so the absence is on the
    # record rather than an oversight.
    with pytest.raises(TypeError):
        cfg.calculate_breakthrough_chance("mortal", "early", "not a number")


def test_a_not_a_number_in_any_offline_clamp_slot_grants_nothing_at_all():
    """The offline grant is a currency credit, so every slot fails CLOSED.

    MEASURED, one slot at a time: a NaN rate, a NaN elapsed window, a NaN
    current bar and a NaN requirement each return exactly ``0.0``. The reason
    is the module's own ``max(0.0, min(raw, remaining))``: with NaN in either
    inner term the inner ``min`` yields NaN, and the outer ``max(0.0, NaN)``
    keeps its FIRST argument, which is 0.0.

    That ordering is the safety property. The alternative -- a NaN that survived
    into the return value -- would be a number no comparison downstream could
    classify, and ``advance_progress`` would write it into the player's saved
    bar. 0.0 is inert: the claim is refused rather than corrupted.

    Infinities are the same shape and the other direction: they never mint
    progress either, because the remainder cap is the smaller term.
    """
    nan = float("nan")
    inf = float("inf")

    assert cfg.clamp_offline_progress(nan, 3600.0, 0.0, 100.0) == 0.0
    assert cfg.clamp_offline_progress(1.0, nan, 0.0, 100.0) == 0.0
    assert cfg.clamp_offline_progress(1.0, 3600.0, nan, 100.0) == 0.0
    assert cfg.clamp_offline_progress(1.0, 3600.0, 0.0, nan) == 0.0
    # Two at once, so no single-slot excuse explains it.
    assert cfg.clamp_offline_progress(nan, nan, nan, nan) == 0.0

    # Never the ceiling and never a negative: a NaN grant is a refusal, not a
    # payment and not an overpayment.
    for grant in (cfg.clamp_offline_progress(nan, 3600.0, 0.0, 100.0),
                  cfg.clamp_offline_progress(1.0, nan, 0.0, 100.0),
                  cfg.clamp_offline_progress(1.0, 3600.0, nan, 100.0)):
        assert grant == 0.0 and 0.0 <= grant <= 100.0

    # An infinite rate or window is capped by the bar's remainder, so "time
    # alone must never chain a realm" survives a clock that returns infinity.
    assert cfg.clamp_offline_progress(inf, 3600.0, 0.0, 100.0) == 100.0
    assert cfg.clamp_offline_progress(1.0, inf, 0.0, 100.0) == 100.0
    assert cfg.clamp_offline_progress(inf, inf, 0.0, 100.0) == 100.0
    assert cfg.clamp_offline_progress(inf, inf, 0.0, 0.0) == 0.0, \
        "an infinite grant into a finished bar is still nothing"


def test_a_realm_order_past_1023_OVERFLOWS_rather_than_returning_a_number():
    """MEASURED LIMITATION, pinned on purpose -- not an endorsement.

    ``calculate_tribulation_damage`` computes ``10.0 * (2 ** realm_order)``.
    The power is an exact Python ``int``, and multiplying it by a float has to
    convert that int to a float -- which raises ``OverflowError`` once the int
    no longer fits. MEASURED boundary:

        order 1023 -> inf        (float(2**1023) is finite; the times-10 overflows)
        order 1024 -> OverflowError: int too large to convert to float

    So the earlier claim in this file -- that an out-of-range order "is still a
    number, not a crash" -- holds up to 1023 and stops there. Nothing in the
    shipped callers can produce such an order: they pass ``realm.order`` from a
    table whose top row is 5. A stored row hand-edited to 1024, or a caller
    doing arithmetic on an order before passing it, would raise rather than
    return a plausible number.

    Asserted rather than skipped so the limit is on the record: if the function
    is hardened to saturate at ``inf`` or to reject the order, THIS TEST FAILS
    and names the change. Delete it in the same commit as the fix.
    """
    assert cfg.calculate_tribulation_damage(1023) == float("inf"), \
        "1023 was measured as the last order that returns a float"
    for order in (1024, 2000, 10 ** 6):
        with pytest.raises(OverflowError):
            cfg.calculate_tribulation_damage(order)

    # Just under the boundary the function still behaves as the curve says, so
    # the raise is a float-conversion limit and not a guard someone added.
    assert cfg.calculate_tribulation_damage(64) == 10.0 * (2 ** 64)
    assert cfg.calculate_tribulation_damage(1000) > 0.0


def test_a_realm_row_with_no_stages_at_all_is_refused_rather_than_indexed():
    """The terminal fallback guards on BOTH a realm and a non-empty stage list.

    ``stage_rate_per_hour``'s peak/breakthrough branch is
    ``if realm is not None and realm.stages:``. The first half is pinned already
    (``immortal`` has no row at all, so the branch is skipped and a KeyError
    follows). This covers the second half: a realm that EXISTS but was
    configured with an empty stage list.

    MEASURED, all eight readers against such a row: the rate reader raises
    ``KeyError`` for both a terminal and a configured-sounding stage name
    rather than indexing an empty list, and the five other readers each answer
    their own documented gap value instead of raising -- lifespan reads the
    realm's own (degenerate) 1.0, the requirement falls back to a full bar,
    the chance reports the 0.0 refusal, ``get_stage`` misses and the ladder walk
    reports the ceiling.

    A realm with no stages is a configuration gap like any other, so it is
    handled by the ordinary gap paths rather than by a new crash. The row is
    injected into ``REALM_MAP`` -- the dict the readers actually consult -- and
    removed again in ``finally``, so no test after this one sees it.
    """
    probe_name = "__contract_probe_void_realm"
    assert probe_name not in cfg.REALM_MAP, "the probe name must not collide"
    saved = cfg.REALM_MAP.get(probe_name)
    empty = cfg.RealmConfig(
        name=probe_name, order=99, stages=[],
        base_lifespan_days=1, tribulation_scales_from=0,
    )
    cfg.REALM_MAP[probe_name] = empty
    try:
        # A terminal name cannot be priced -- there is no last stage to finish.
        with pytest.raises(KeyError) as excinfo:
            cfg.stage_rate_per_hour(probe_name, "peak")
        assert probe_name in str(excinfo.value) and "peak" in str(excinfo.value)
        with pytest.raises(KeyError):
            cfg.stage_rate_per_hour(probe_name, "early")

        # An explicit default still rescues it: the raise is about the missing
        # row, not about the empty list being fatal on its own.
        assert cfg.stage_rate_per_hour(probe_name, "peak", default=1.0) == 1.0

        # The readers with a documented gap answer it instead of raising.
        assert cfg.calculate_lifespan(probe_name) == 1.0
        assert cfg.stage_progress_required(probe_name, "early") == \
            cfg.DEFAULT_STAGE_PROGRESS_REQUIRED
        assert cfg.calculate_breakthrough_chance(probe_name, "early") == 0.0
        assert cfg.get_stage(probe_name, "early") is None
        assert cfg.get_next_stage(probe_name, "early") == (None, None)
    finally:
        if saved is not None:
            cfg.REALM_MAP[probe_name] = saved
        else:
            cfg.REALM_MAP.pop(probe_name, None)
    assert probe_name not in cfg.REALM_MAP, "the probe row leaked into the table"


def test_a_corrupted_stage_order_walks_a_WRONG_rung_silently_instead_of_raising():
    """MEASURED LIMITATION: the ladder walk trusts ``stage.order`` as an index.

    ``get_next_stage`` compares ``current_stage.order`` against
    ``len(realm.stages) - 1`` and then reads ``realm.stages[current_stage.order
    + 1]``. Nothing clamps the value, so a stage row whose ``order`` was
    corrupted answers a wrong rung rather than refusing. MEASURED, at the
    bottom rung of ``mortal``:

        order  9 -> ("foundation", "early")   promoted, because 9 >= 2
        order -1 -> ("mortal", "early")        stayed put: index 0 is itself
        order  2 -> ("foundation", "early")   promoted, indistinguishable
                                              from a correct last stage

    The last row is the dangerous one: ``order 2`` on a three-stage realm is a
    plausible hand-edit, and it promotes exactly like a correct ``late``. The
    static checker at the top of this file reports such a row, which is the
    guard -- but the runtime does not, and this test is what records that a
    guard exists in one place and not the other.

    Restored in ``finally`` so the shipped table is unchanged for the rest of
    the run, and the correct value is asserted after restoration so a leak
    would fail here rather than in someone else's test.
    """
    realm = cfg.get_realm("mortal")
    stage = realm.stages[0]
    original = stage.order
    try:
        stage.order = 9
        assert cfg.get_next_stage("mortal", "early") == ("foundation", "early")
        stage.order = -1
        assert cfg.get_next_stage("mortal", "early") == ("mortal", "early")
        stage.order = 2
        assert cfg.get_next_stage("mortal", "early") == ("foundation", "early"), \
            "a corrupted order that lands on the last index reads as a real one"
    finally:
        stage.order = original
    assert stage.order == 0, "the corrupted stage order leaked"

    # The whole-ladder walk is unaffected once the row is restored, which is
    # the control: the answers above came from the corruption, not from the
    # table having moved.
    walked = [LADDER[0]]
    r, s = LADDER[0]
    for _ in range(len(LADDER)):
        nxt = cfg.get_next_stage(r, s)
        if nxt == (None, None):
            break
        r, s = nxt
        walked.append((r, s))
    assert walked == LADDER


def test_an_int_valued_row_is_normalised_to_a_float_by_every_reader_that_returns_one():
    """The readers promise a float, so a hand-edited integer row still returns one.

    The dataclass declares ``float`` but does not enforce it, and nothing
    validates the table at import. MEASURED with the bottom row of ``mortal``
    rewritten to integers (``progress_required=50``, rate ``3``, chance ``1``):
    every reader returns a ``float``, not the ``int`` that was stored, because
    each one wraps its result in ``float()``.

    Worth pinning because the alternative is a wire change: an int row reaching
    a route as ``50`` instead of ``50.0`` is invisible in a JSON body and
    visible anywhere the value is compared with ``isinstance(..., float)`` or
    serialised with a schema that distinguishes the two.

    The chance row is set to 1, above the 0.95 ceiling, so the assertion also
    pins that the clamp -- not the row -- is what decides the answer.
    """
    stage = cfg.REALMS[0].stages[0]
    saved = (stage.progress_required, stage.cultivation_rate_per_hour,
             stage.breakthrough_base_chance)
    try:
        stage.progress_required = 50          # int, not 50.0
        stage.cultivation_rate_per_hour = 3   # int
        stage.breakthrough_base_chance = 1    # int, and above the ceiling

        requirement = cfg.stage_progress_required("mortal", stage.name)
        rate = cfg.stage_rate_per_hour("mortal", stage.name)
        chance = cfg.calculate_breakthrough_chance("mortal", stage.name)

        assert requirement == 50.0 and isinstance(requirement, float)
        assert rate == 3.0 and isinstance(rate, float)
        assert chance == CHANCE_CEILING and isinstance(chance, float), \
            "the clamp, not the stored value, decides the chance"
    finally:
        (stage.progress_required, stage.cultivation_rate_per_hour,
         stage.breakthrough_base_chance) = saved

    # Restored, so the readers are back to reading floats.
    assert isinstance(cfg.stage_progress_required("mortal", stage.name), float)
    assert cfg.stage_rate_per_hour("mortal", stage.name) == \
        float(stage.cultivation_rate_per_hour)


def test_the_terminal_fallback_matches_the_two_names_EXACTLY_and_nothing_near_them():
    """The fallback is scoped to a closed set, so a near-miss spelling is a gap.

    ``TERMINAL_STAGE_NAMES`` is a ``frozenset`` and the reader tests membership
    with ``in``, which is an exact string match with no case folding and no
    trimming. MEASURED: ``Peak``, ``PEAK``, ``" breakthrough"`` and
    ``"breakthrough_"`` all MISS the fallback and take the caller's default
    instead -- which is the correct outcome, because each of them is corrupt
    data rather than a state the game defines, and ``_as_stage`` already
    refuses them upstream with a readable message.

    The set's immutability is part of the contract: it is a ``frozenset``, so a
    caller cannot add a name to it at run time and silently widen the fallback
    to a spelling the game does not define.
    """
    assert isinstance(cfg.TERMINAL_STAGE_NAMES, frozenset), \
        "a mutable set here could be widened at run time by any caller"
    assert sorted(cfg.TERMINAL_STAGE_NAMES) == ["breakthrough", "peak"]

    near_misses = ("Peak", "PEAK", " breakthrough", "breakthrough_",
                   "peak ", "peakpeak")
    for name in near_misses:
        assert name not in cfg.TERMINAL_STAGE_NAMES, name
        # The fallback does not fire, so the caller's default is what comes back.
        assert cfg.stage_rate_per_hour("mortal", name, default=7.0) == 7.0, name
        # And the other readers treat it as an undescribed stage.
        assert cfg.stage_progress_required("mortal", name) == \
            cfg.DEFAULT_STAGE_PROGRESS_REQUIRED, name
        assert cfg.calculate_breakthrough_chance("mortal", name) == 0.0, name

    # The two exact names DO get the fallback, so the misses above are the
    # matching working rather than the fallback never running.
    for realm in cfg.REALMS:
        expected = float(realm.stages[-1].cultivation_rate_per_hour)
        for name in sorted(cfg.TERMINAL_STAGE_NAMES):
            assert cfg.stage_rate_per_hour(realm.name, name) == expected, \
                (realm.name, name)


# ---------------------------------------------------------------------------
# 12. THE LEGACY BONUS -- one scale, one reader, and it is NOT dead
#
# MEASURED GAP THIS SECTION CLOSES. A mutation control was run against this
# file before section 12 existed. Three defects were injected into the real
# module (a fresh module object swapped into sys.modules, so the shipped file
# was never edited) and this file's 50 tests were re-run against each:
#
#   M_literal_instead_of_constant  legacy_bonus * 0.05 instead of the
#                                  constant ................... 50 passed
#   M_legacy_bonus_of_raw          skip _finite_float, return the
#                                  stored value ................ 50 passed
#   M_legacy_bonus_of_no_dict_guard  delete the isinstance guard ... 50 passed
#
# All three SURVIVED. So before this section the file asserted that
# legacy_bonus moves the chance (section 7, with the multiplier pasted in as
# a local literal) but proved nothing about WHICH 0.05 was applied, nothing
# about the reader that turns a stored field into that argument, and nothing
# about the reader's tolerance of a player that is not a dict at all.
#
# The literals in section 7 are the reason the first mutant survived: a test
# that pastes its own copy of the scale cannot notice the module using a
# different one. The fix is to read ``cfg.LEGACY_BONUS_PER_POINT`` instead.
# ---------------------------------------------------------------------------

def test_the_chance_reads_the_shared_scale_constant_rather_than_a_pasted_literal():
    """The "spelled ONCE" claim, made falsifiable.

    ``LEGACY_BONUS_PER_POINT`` exists so two readers -- this function and
    ``breakthrough.attempt_chance`` -- cannot drift onto two different values
    for what one legacy point buys. That claim is only worth anything if THIS
    function actually consults the constant. Retuning it must therefore move
    this function's answer; if the 0.05 were pasted in here, the retune would
    be invisible from this file, which is exactly what the mutation control
    measured.

    Restored in ``finally``, and the shipped value is re-asserted afterwards so
    a leak fails here rather than in a later test.
    """
    assert cfg.LEGACY_BONUS_PER_POINT == 0.05, \
        "the documented scale is five percentage points per legacy point"

    base = cfg.calculate_breakthrough_chance("mortal", "late")
    one_point = cfg.calculate_breakthrough_chance("mortal", "late", 0.0, 1.0)

    # MEASURED, exact: base 0.85 + one point 0.05 = 0.90, and float addition
    # of these two values is exact here, so == is safe rather than a tolerance.
    assert one_point == base + cfg.LEGACY_BONUS_PER_POINT

    # The hook itself: retune the constant, the answer must follow.
    try:
        cfg.LEGACY_BONUS_PER_POINT = 0.10
        retuned = cfg.calculate_breakthrough_chance("mortal", "late", 0.0, 1.0)
        assert retuned == base + 0.10, \
            "the constant was retuned but this function did not follow it"
    finally:
        cfg.LEGACY_BONUS_PER_POINT = 0.05

    assert cfg.calculate_breakthrough_chance("mortal", "late", 0.0, 1.0) == \
        one_point, "the retuned constant leaked out of the test"
    assert cfg.LEGACY_BONUS_PER_POINT == 0.05


def test_the_legacy_bonus_parameter_is_LIVE_arithmetic_not_a_dead_argument():
    """Requirement: the parameter is USED. Removal is a contract break.

    MEASURED end to end on (mortal, late), whose 0.85 base leaves headroom
    under the 0.95 ceiling so the modifiers are visible rather than masked:

        no modifiers ........ 0.85
        legacy_bonus=20.0 ... 0.95   (saturated at the ceiling)
        legacy_bonus=-20.0 .. 0.05   (saturated at the floor)

    So the parameter moves the answer in BOTH directions and saturates at the
    clamp rather than running away. A function that accepted ``legacy_bonus``
    and ignored it would return 0.85 for all three.

    The base row is chosen deliberately: ``mortal/early`` is already at 0.95,
    so ANY positive bonus there is masked by the clamp and would make this
    test pass against a dead parameter. The negative case proves the parameter
    is read at all.
    """
    base = cfg.calculate_breakthrough_chance("mortal", "late")
    assert base < CHANCE_CEILING, \
        "pick a rung with headroom, or the clamp masks a dead parameter"

    assert cfg.calculate_breakthrough_chance("mortal", "late", 0.0, -20.0) == \
        CHANCE_FLOOR
    assert cfg.calculate_breakthrough_chance("mortal", "late", 0.0, 20.0) == \
        CHANCE_CEILING
    assert cfg.calculate_breakthrough_chance("mortal", "late", 0.0, 1.0) > base
    assert cfg.calculate_breakthrough_chance("mortal", "late", 0.0, -1.0) < base

    # The parameter is positional-or-keyword and is passed by name at
    # ``main.py:2566`` and ``main.py:2791``, so the keyword form must keep
    # working.
    assert cfg.calculate_breakthrough_chance(
        "mortal", "late", legacy_bonus=1.0) == \
        cfg.calculate_breakthrough_chance("mortal", "late", 0.0, 1.0)

    # MEASURED: one legacy point is worth exactly HALF a luck point, not the
    # same amount -- a documented asymmetry, pinned so swapping the two
    # coefficients fails here rather than passing silently.
    #
    # Measured on (mortal, late), which has 0.10 of headroom under the ceiling
    # -- exactly one luck step, so the luck side SATURATES at 0.95 and cannot
    # be differenced. (dao_seeking/late is the rung with clearance on both
    # sides: 0.10 base, 0.20 with a luck point, 0.15 with a legacy one.) A
    # tolerance rather than == because these sums are inexact binary floats:
    # 0.20 - 0.10 is 0.09999999999999998, not 0.1.
    bottom = cfg.calculate_breakthrough_chance("dao_seeking", "late")
    luck_step = cfg.calculate_breakthrough_chance("dao_seeking", "late", 1.0, 0.0) - bottom
    legacy_step = cfg.calculate_breakthrough_chance("dao_seeking", "late", 0.0, 1.0) - bottom

    assert abs(luck_step - 0.10) < 1e-9, luck_step
    assert abs(legacy_step - 0.05) < 1e-9, legacy_step
    assert abs(luck_step - 2 * legacy_step) < 1e-9, \
        "one luck point must be worth exactly two legacy points, not one"


def test_legacy_bonus_of_is_TOTAL_and_never_raises_on_any_player_shape():
    """The one reader for the stored field, on every shape a row can hold.

    MEASURED, one row per case. A reader that raised would take the whole
    breakthrough surface down on one bad row, so totality is the contract:

        {} (no field) ........... 0.0     None ................... 0.0
        "not a dict" ............ 0.0     12 (int, not a dict) ... 0.0
        [] (list) ............... 0.0     {"legacy_bonus": None} .. 0.0
        {"legacy_bonus": "abc"} . 0.0     {"legacy_bonus": []} .... 0.0
        {"legacy_bonus": nan} ... 0.0     {"legacy_bonus": inf} ... 0.0
        {"legacy_bonus": 10**400} 0.0     {"legacy_bonus": True} .. 1.0
        {"legacy_bonus": "3.5"} . 3.5     {"legacy_bonus": 7.0} ... 7.0

    The last two rows matter as much as the refusals: a numeric STRING is a
    number (the writers store text) and a bool is an int, so both must survive
    the coercion rather than being refused as corrupt.

    This is the assertion the M_legacy_bonus_of_no_dict_guard mutant needed:
    deleting the ``isinstance(player, dict)`` check makes ``None.get`` raise
    ``AttributeError``, which nothing in this file previously detected.
    """
    refusals = (
        {}, None, "not a dict", 12, 3.5, ["mortal"], {"legacy_bonus": None},
        {"legacy_bonus": "abc"}, {"legacy_bonus": []}, {"legacy_bonus": {}},
        {"legacy_bonus": float("nan")}, {"legacy_bonus": float("inf")},
        {"legacy_bonus": float("-inf")}, {"legacy_bonus": 10 ** 400},
        {"other_field": 5.0},
    )
    for player in refusals:
        bonus = cfg.legacy_bonus_of(player)
        assert bonus == 0.0, (player, bonus)
        assert isinstance(bonus, float), (player, type(bonus))

    # The values that must SURVIVE, so the refusals above are the guard working
    # and not a reader that refuses everything.
    assert cfg.legacy_bonus_of({"legacy_bonus": 7.0}) == 7.0
    assert cfg.legacy_bonus_of({"legacy_bonus": "3.5"}) == 3.5, \
        "the writers store numbers as text; text that parses is a number"
    assert cfg.legacy_bonus_of({"legacy_bonus": True}) == 1.0, \
        "bool is an int subclass and is a legitimate 1-point bonus"
    assert cfg.legacy_bonus_of({"legacy_bonus": -5.0}) == -5.0, \
        "a negative bonus is passed through; the chance clamp is what floors it"


def test_a_corrupt_stored_legacy_bonus_costs_the_BONUS_and_never_the_roll():
    """The asymmetry the reader exists for, measured through the chance.

    ``_finite_float`` returns None for infinity, NaN and a 10**400 stored
    whole number, so ``legacy_bonus_of`` hands the caller 0.0 and the roll
    lands on the row's configured base. The alternative -- propagating the
    stored figure -- would put a non-finite number into an arithmetic total
    that the settlement loop persists.

    MEASURED on (mortal, late): a clean 0.0 bonus and a corrupt one both score
    exactly 0.85, while a genuine 2.0 scores 0.95. So the guard is
    distinguishable from BOTH neighbours: it is not ignoring the field, and it
    is not letting corruption through.
    """
    base = cfg.calculate_breakthrough_chance("mortal", "late")
    clean = cfg.calculate_breakthrough_chance(
        "mortal", "late", 0.0, cfg.legacy_bonus_of({"legacy_bonus": 0.0}))
    genuine = cfg.calculate_breakthrough_chance(
        "mortal", "late", 0.0, cfg.legacy_bonus_of({"legacy_bonus": 2.0}))

    assert clean == base == 0.85
    assert genuine > clean, "a genuine bonus must still move the roll"

    for dirty in ("text", None, float("nan"), float("inf"), float("-inf"), 10 ** 400, [1]):
        got = cfg.legacy_bonus_of({"legacy_bonus": dirty})
        assert got == 0.0, (dirty, got)
        rolled = cfg.calculate_breakthrough_chance("mortal", "late", 0.0, got)
        assert rolled == clean, (dirty, rolled)
        assert 0.0 < rolled <= CHANCE_CEILING, \
            "a corrupt bonus must never turn a priced rung into a refusal"


def test_a_corrupt_BASE_row_costs_the_roll_while_a_corrupt_stat_costs_the_bonus():
    """Two different corruptions, two different answers. Both fail closed.

    MEASURED, and the difference is the point:

      the TABLE ROW is non-finite  -> 0.0  (this function's own refusal: the
                                      stage cannot be priced at all)
      the PLAYER STAT is non-finite -> the FLOOR 0.05, because the row is
                                      real and only the modifier is corrupt
                                      -- and ``max(0.05, nan)`` is 0.05 in
                                      CPython, so the clamp lands there

    A row-corruption therefore reports a DIFFERENT number from a
    stat-corruption, so the two are not conflated. Both are refusals rather
    than payments: neither can produce a guaranteed breakthrough, because
    ``min(0.95, nan)`` would be the 0.95 CEILING if the clamp were ordered the
    other way round.
    """
    stage = cfg.REALM_MAP["mortal"].stages[0]
    original = stage.breakthrough_base_chance
    try:
        for bad in (float("inf"), float("-inf"), float("nan"), "text", None):
            stage.breakthrough_base_chance = bad
            got = cfg.calculate_breakthrough_chance("mortal", stage.name)
            assert got == 0.0, (bad, got)
    finally:
        stage.breakthrough_base_chance = original

    assert cfg.calculate_breakthrough_chance("mortal", stage.name) == 0.95, \
        "the row was not restored"

    # The stat side, on a row that is intact.
    for bad in (float("inf"), float("-inf"), float("nan")):
        got = cfg.calculate_breakthrough_chance(
            "mortal", "late", bad, cfg.legacy_bonus_of({"legacy_bonus": bad}))
        assert CHANCE_FLOOR <= got <= CHANCE_CEILING, (bad, got)


def test_a_non_finite_MODIFIER_argument_is_NOT_the_same_as_a_non_finite_row():
    """MEASURED LIMITATION, pinned on purpose: the two argument slots are
    unguarded, and that is asymmetric with the row.

    ``_finite_float`` guards the TABLE ROW, but ``player_luck`` and
    ``legacy_bonus`` are multiplied raw. MEASURED on (mortal, late):

        luck=nan        -> 0.05  (clamped, does not raise)
        luck=inf        -> 0.95  (clamped, does not raise)
        legacy=10**400  -> OverflowError: int too large to convert to float
        luck="abc"      -> TypeError: can't multiply sequence by non-int float
        luck=None       -> TypeError: unsupported operand type(s) for *

    So a caller that reads the stat with a bare ``float(player.get(...))`` --
    which is what four of the five writers do -- hands this function a value
    that can raise, where a NaN does not. That is why ``legacy_bonus_of``
    exists: it is the argument-producing boundary, and the engine's only
    caller (``main.py:2566``) routes the field through it.

    Reachability: MEASURED, both shipped callers pass a clean float or the
    output of ``legacy_bonus_of``, so no in-tree caller can hit these. The
    assertions are here so the limit is on the record -- if the arguments are
    ever hardened to refuse instead of raise, THIS TEST FAILS and names the
    change. Delete it in the same commit as that fix.

    The infinity rows are asserted alongside because they are the surprising
    half: they do NOT raise, they clamp, and ``inf`` lands on the CEILING.
    A corrupt stat is therefore a BETTER roll than a real one, which is why
    the reader that produces these arguments must not hand out infinity.
    """
    for bad in (float("inf"), float("-inf"), float("nan")):
        for luck, legacy in ((bad, 0.0), (0.0, bad), (bad, bad)):
            got = cfg.calculate_breakthrough_chance("mortal", "late", luck, legacy)
            assert CHANCE_FLOOR <= got <= CHANCE_CEILING, (luck, legacy, got)

    # The two slots that DO raise, pinned as raises rather than skipped.
    with pytest.raises(OverflowError):
        cfg.calculate_breakthrough_chance("mortal", "late", 0.0, 10 ** 400)
    with pytest.raises(TypeError):
        cfg.calculate_breakthrough_chance("mortal", "late", "abc", 0.0)
    with pytest.raises(TypeError):
        cfg.calculate_breakthrough_chance("mortal", "late", None, 0.0)

    # Why the reader matters: routing the same value through it produces a
    # clean argument, so the raise above is unreachable through the shipped
    # path.
    for dirty in (10 ** 400, "abc", None):
        argument = cfg.legacy_bonus_of({"legacy_bonus": dirty})
        assert isinstance(argument, float) and argument == 0.0, dirty
        cfg.calculate_breakthrough_chance("mortal", "late", 0.0, argument)