"""
MONTHLY EVENT ROUTES: THE HTTP CONTRACT, MEASURED.

WHAT THIS FILE IS FOR
--------------------
``server/monthly_event.py`` mounts four client-facing routes and nothing else
in this game reads the event clock. This file answers one question: what does a
client that has never read the Python actually send, and what does it get back?

Every assertion below was MEASURED by calling the routes, not read off the
return dictionaries in the module. Two places where the measured answer is not
what the module's comments imply are called out at the tests themselves.

THE FOUR ROUTES
---------------
``GET  /monthly-event/{player_id}``              -- read the banner
``POST /monthly-event/{player_id}/claim-daily``  -- claim today's reward
``POST /monthly-event/{player_id}/complete-quest`` -- finish a quest (``quest_day``)
``POST /monthly-event/{player_id}/buy-shop``     -- buy (``item_id``, ``quantity``)

There is no route to start or end the event: both are arithmetic on the clock.

TWO THINGS A CLIENT MUST KNOW
-----------------------------
1. THE MODULE ITSELF CARRIES NO AUTH. Mounted on a bare FastAPI app, all four
   routes answer an anonymous caller with 200. The authentication comes from a
   central shape-matching guard installed once in ``main.py``, and it is
   measured here on the live app: anonymous 401, another player's session 403,
   your own session passes through to the handler. Do not read a 200 from a
   bare app as "the route is open in the shipped game".
2. ``quest_day``, ``item_id`` and ``quantity`` are QUERY parameters, never a
   JSON body. Sending them in a body is not an error -- they are simply
   ignored, and the missing query parameter answers 422.

MEASURED SHAPES (2026-10-04)
---------------------------
state (all four reads agree on this key set):
  active, day_of_month, days_remaining, next_event_in, theme, theme_name,
  theme_description, visual, daily_rewards, quest_chain, shop_currency,
  shop_items
claim-daily 200: ok, day, reward, battle_pass
complete-quest 200: ok, quest, bonus_reward, battle_pass
buy-shop 200: ok, item, quantity, remaining_currency

REFUSALS, all measured
  401 no session                     403 session for another player
  404 unknown player                 400 event not active
  409 daily already claimed          409 quest already completed
  404 no reward on this day          404 no quest on this day
  400 quest day not unlocked yet     400 not enough currency
  404 item not on the shelf          400 already own that cosmetic
  422 quantity below 1 / above 99    422 missing query parameter

Run (memory mode -- no Postgres vars, so the store needs no migration):

  env -u WC_PG_DB <python> -m pytest server/tests/test_monthly_event_contract.py -q
"""
from __future__ import annotations

import os

# The routes never reach for a database connection; popping these first keeps an
# ambient value from dragging the store into a live-Postgres branch.
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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
import cosmetics
import main as main_module
import mission_board
import missions
import monthly_event as me
from monthly_event import (CelestialTheme, EVENT_DURATION_DAYS,
                           THEME_EVENT_COSMETIC, register_monthly_event_routes)
from player_store import PlayerStore

# Clearing the signup rate limit is needed by the live-app auth tests, which
# create real players. Routed through the shared helper because it clears BOTH
# identities of main.py; a direct attribute clear here would break the moment
# the counter is renamed.
from server.tests.wc_test_auth import clear_signup_rate_limit


# --------------------------------------------------------------------------
# Measured key sets. A changed key set is a broken client, not a tidy-up.
# --------------------------------------------------------------------------
ROUTE_TABLE = {
    "GET /monthly-event/{player_id}",
    "POST /monthly-event/{player_id}/claim-daily",
    "POST /monthly-event/{player_id}/complete-quest",
    "POST /monthly-event/{player_id}/buy-shop",
}

STATE_KEYS = {
    "active", "day_of_month", "days_remaining", "next_event_in", "theme",
    "theme_name", "theme_description", "visual", "daily_rewards",
    "quest_chain", "shop_currency", "shop_items",
}
CLAIM_KEYS = {"ok", "day", "reward", "battle_pass"}
QUEST_KEYS = {"ok", "quest", "bonus_reward", "battle_pass"}
SHOP_KEYS = {"ok", "item", "quantity", "remaining_currency"}
BATTLE_PASS_KEYS = {
    "ok", "source", "xp_gained", "new_level", "leveled_up", "total_xp",
}


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------
def _player(pid: str) -> dict:
    return {
        "id": pid, "name": pid, "cultivation_realm": "mortal",
        "cultivation_stage": "early", "progress": 0.0,
        "spirit_stones": 0, "hp": 120, "atk": 20, "def_": 10,
        "mp": 50, "spd": 10,
    }


def _active_start(day: int = 0) -> float:
    """An epoch whose day-of-month is ``day`` and where the event is LIVE.

    The wall clock is almost never inside a 14-day window, so every test that
    wants a live event has to place the clock itself rather than hope.
    """
    for cycle in range(1, 400):
        candidate = cycle * me.CYCLE_DAYS * me.DAY + day * me.DAY + 100
        if (me._utc_day(candidate) % me.CYCLE_DAYS == day
                and me.get_event_state(candidate)["active"]):
            return float(candidate)
    raise AssertionError("no live event day %d found" % day)


def _active_start_for(theme: CelestialTheme, day: int = 0) -> float:
    """An epoch inside a LIVE window carrying ``theme`` and day-of-month ``day``.

    ``_active_start`` returns the FIRST live window it finds, which is always
    ``north_mystic`` -- the themes alternate and that one comes first. So every
    test that reaches for it silently measures one theme. Anything that claims
    to hold "for the event" and means only North Mystic has to say so; use this
    helper to name the theme and cover both.

    The theme is re-checked at the requested day rather than only at day 0, so a
    theme that ever flipped mid-window would fail here instead of quietly
    handing the caller a day-14 epoch in the wrong theme.
    """
    for cycle in range(1, 400):
        candidate = cycle * me.CYCLE_DAYS * me.DAY + day * me.DAY + 100
        if (me._utc_day(candidate) % me.CYCLE_DAYS == day
                and me.get_event_state(candidate)["active"]
                and me.get_current_theme(candidate) is theme
                and me.get_current_theme(candidate + day * me.DAY) is theme):
            return float(candidate)
    raise AssertionError("no live %s window on day %d found" % (theme.value, day))


def _inactive_day(day: int = EVENT_DURATION_DAYS) -> float:
    """An epoch whose day-of-month is ``day`` and where the event is CLOSED."""
    for cycle in range(1, 400):
        candidate = cycle * me.CYCLE_DAYS * me.DAY + day * me.DAY + 100
        if (me._utc_day(candidate) % me.CYCLE_DAYS == day
                and not me.get_event_state(candidate)["active"]):
            return float(candidate)
    raise AssertionError("no closed event day %d found" % day)


def _seeded_app(*players: str):
    """A throwaway app carrying ONLY the monthly-event routes."""
    app = FastAPI()
    store = PlayerStore(main_module.InMemoryWalletStore())
    with store.transaction():
        for pid in players:
            store[pid] = _player(pid)
    register_monthly_event_routes(app, lambda: store)
    return app, store


def _grant_currency(store, pid: str, currency: str, amount: int) -> None:
    """Write event currency the way the store holds a row, not the way it reads one.

    MERGES into the existing block rather than replacing it. A caller that wants
    both themes' currencies seeded at once -- which the window-refusal test does
    -- needs both keys to survive; replacing the dict would leave whichever was
    written second as the only balance on the row.
    """
    with store.transaction(pid):
        row = dict(store[pid])
        block = dict(row.get("monthly_event", {}))
        block[currency] = amount
        row["monthly_event"] = block
        store[pid] = row
        store.persist(pid, row)


def _seed_quest_criteria(store, pid: str, theme: CelestialTheme,
                         quest_day: int) -> dict:
    """Record exactly what ``QUEST_CRITERIA`` asks for, and nothing more.

    Read from the shipped table rather than written out here, so this seed
    tracks a criteria change instead of silently disagreeing with it. The
    counter is read back and asserted to have landed, because a seed that did
    not take would turn every "the gate refuses" test below into a test of the
    seed rather than of the gate.

    Two sources are needed and they are seeded differently: ``qi_progress`` is a
    field on the player's own row, while ``combat_wins`` lives in the mission
    store's event ledger, which ``quest_criteria_met`` resolves per call out of
    the ``missions`` module.
    """
    row = me.QUEST_CRITERIA.get((theme.value, quest_day))
    assert row is not None, (
        "no criteria row for %s day %d, so this test would measure a 404"
        % (theme.value, quest_day))
    target = int(row["target"])
    source = row["source"]

    if source == "qi_progress":
        with store.transaction(pid):
            player = dict(store[pid])
            player["progress"] = float(target)
            store[pid] = player
            store.persist(pid, player)
    elif source == "combat_wins":
        missions.mission_store.record_event(pid, "combat", target)
    else:
        raise AssertionError(
            "no seed arm for source %r; add one rather than skipping, or this "
            "file stops covering that row" % source)

    met, detail = me.quest_criteria_met(store[pid], pid, theme, quest_day)
    assert met, "the seed did not land: %s" % detail
    return row


def _criteria_met_unseeded(store, pid: str, theme: CelestialTheme,
                           quest_day: int) -> None:
    """CONTROL arm for ``_seed_quest_criteria``: prove the seed is what opened
    the gate, by leaving it shut. A seed helper that silently always met the
    criteria would make every pay-path test pass for the wrong reason."""
    met, detail = me.quest_criteria_met(store[pid], pid, theme, quest_day)
    assert not met, (
        "%s day %d already reads as complete without a seed (%s), so the gate "
        "cannot be proven to bite in this file" % (theme.value, quest_day, detail))


def _held_currency(store, pid: str, currency: str):
    """The shop balance as the STORE holds it. Never the reply: the reply is
    built from the same dict the handler just mutated."""
    return store[pid].get("monthly_event", {}).get(currency, 0)


def _numeric_event_total(store, pid: str):
    """Every number sitting under ``monthly_event``, whatever it is keyed by."""
    return sum(v for v in store[pid].get("monthly_event", {}).values()
               if isinstance(v, (int, float)))


def _real_players(client, *names: str):
    """Create real players through the real create route, in order."""
    created = []
    for name in names:
        response = client.post("/player/create",
                               json={"name": name, "spirit_root": "metal"})
        assert response.status_code == 200, (
            "fixture setup failed: /player/create answered %d (%s)"
            % (response.status_code, response.text[:160]))
        created.append(response.json()["id"])
    return created


def _authed(pid: str) -> dict:
    return {"Authorization": "Bearer %s" % auth.issue_session(pid)}


@pytest.fixture(autouse=True)
def _fresh_signup_budget():
    """The live-app tests create players, and ``/player/create`` is rate limited
    to five per minute per address. Cleared around every test in this file so a
    later test cannot fail at the fixture instead of at its own assertion."""
    clear_signup_rate_limit()
    yield
    clear_signup_rate_limit()


@pytest.fixture
def live_client(monkeypatch):
    """The SHIPPED app with a clock pinned inside a live event window.

    ``main_module.app`` is the real thing: it is where the central auth guard
    and the real players_db live, and a bare re-mount of the routes cannot show
    either. The clock is pinned because the event is closed for 16 of every 30
    days, so an unpinned test would measure the window refusal instead of the
    payout.
    """
    clock = _PinnedClock(_active_start(0))
    monkeypatch.setattr(me.time, "time", clock)
    with TestClient(main_module.app) as client:
        yield client


class _PinnedClock:
    """Replaces the module's clock so a test can sit inside or outside the event."""

    def __init__(self, epoch: float):
        self.epoch = epoch

    def __call__(self):
        return self.epoch

    def set(self, epoch: float) -> None:
        self.epoch = epoch


@pytest.fixture
def live_event(monkeypatch):
    """Clock pinned to day 1 of a live event, restored afterwards."""
    epoch = _active_start(0)
    clock = _PinnedClock(epoch)
    monkeypatch.setattr(me.time, "time", clock)
    return clock


@pytest.fixture
def app_with_players():
    return _seeded_app("p1", "p2")


@pytest.fixture(autouse=True)
def _isolated_event_counters():
    """A quest counter belongs to ONE player's ledger, and that ledger is a
    process-wide singleton inside ``missions``.

    So a seed written for one test is still there for the next one, and a test
    that asserts "this player did nothing" silently inherits the previous
    test's combat wins. MEASURED while writing this file: three of the new
    quest tests went green for the wrong reason until this fixture existed,
    because an earlier test had already banked 3 wins against the shared id
    ``p2``.

    Restoring the saved contents in a ``finally`` is what keeps this honest on a
    shared tree: a test that raises mid-body cannot leave the global counter
    perturbed for every test that runs after it.

    The in-memory dict is read by name. If a future rename moves it, the
    ``getattr`` returns None and the fixture quietly stops isolating rather than
    raising AttributeError in every test -- a loud failure here would be a
    hundred confusing ones, and the quest tests below each assert their own
    precondition, so a leak shows up as a real assertion rather than as noise.
    """
    store = getattr(missions, "mission_store", None)
    counters = getattr(store, "_mem_events", None) if store is not None else None
    if counters is None:
        yield
        return
    saved = {pid: dict(counts) for pid, counts in counters.items()}
    try:
        yield
    finally:
        counters.clear()
        counters.update(saved)


# --------------------------------------------------------------------------
# A. The surface: four routes, registered once, no more
# --------------------------------------------------------------------------

def test_exactly_four_routes_are_mounted_and_they_are_the_documented_ones():
    app, _ = _seeded_app()
    mounted = {
        "%s %s" % (sorted(r.methods - {"HEAD", "OPTIONS"})[0], r.path)
        for r in app.routes
        if getattr(r, "methods", None) and r.path.startswith("/monthly-event")
    }
    assert mounted == ROUTE_TABLE, (
        "the client-facing event surface changed: got %s, contract says %s. "
        "There is deliberately no start and no end route -- both are clock "
        "arithmetic, not player actions." % (sorted(mounted), sorted(ROUTE_TABLE)))


# --------------------------------------------------------------------------
# B. The banner read: shape, and what it says about the clock
# --------------------------------------------------------------------------

def test_state_read_carries_the_twelve_documented_keys(live_event, app_with_players):
    app, _ = app_with_players
    response = TestClient(app).get("/monthly-event/p1")
    assert response.status_code == 200
    assert set(response.json()) == STATE_KEYS


def test_state_read_carries_no_player_field(app_with_players):
    """A banner is world state. It must never echo who asked."""
    app, _ = app_with_players
    response = TestClient(app).get("/monthly-event/p1")
    assert "player_id" not in response.text
    assert "p1" not in response.text


def test_unknown_player_is_404_on_every_route(live_event, app_with_players):
    """One rule, four routes: a player who does not exist is a 404, not a 200
    with an empty banner and not a 500."""
    app, _ = app_with_players
    client = TestClient(app)
    assert client.get("/monthly-event/ghost").status_code == 404
    assert client.post("/monthly-event/ghost/claim-daily").status_code == 404
    assert client.post("/monthly-event/ghost/complete-quest",
                       params={"quest_day": 1}).status_code == 404
    assert client.post("/monthly-event/ghost/buy-shop",
                       params={"item_id": "x"}).status_code == 404


# --------------------------------------------------------------------------
# C. Auth is CENTRAL, not per-route. Measured on the live app.
# --------------------------------------------------------------------------

def _live_client():
    return TestClient(main_module.app)


def test_every_event_route_is_closed_to_anonymous_callers_on_the_live_app():
    """The module has no auth of its own; ``main`` installs a guard that matches
    routes by shape. This asserts the shipped behaviour, on the shipped app."""
    client = _live_client()
    pid = "no-such-player-auth-probe"
    assert client.get("/monthly-event/%s" % pid).status_code == 401
    assert client.post("/monthly-event/%s/claim-daily" % pid).status_code == 401
    assert client.post("/monthly-event/%s/complete-quest" % pid,
                       params={"quest_day": 1}).status_code == 401
    assert client.post("/monthly-event/%s/buy-shop" % pid,
                       params={"item_id": "x"}).status_code == 401


def test_a_session_for_another_player_is_refused_with_403():
    """MEASURED 2026-10-05 against REAL players.

    The earlier version of this test minted a session for a player the store
    never had and expected 403. It now gets 401: ``auth`` refuses a session
    whose own player row is absent (``session_player_deleted``) before any
    ownership question is asked, which is the correct order -- a session for a
    deleted account must not be able to probe for live ones. So the probe needs
    two players who both exist, and only the first one's own session is refused.

    The row below is therefore created for real, which is also what makes this a
    test of ownership rather than of account existence.
    """
    client = _live_client()
    mine, theirs = _real_players(client, "own_403_mine", "own_403_theirs")
    assert client.get("/monthly-event/%s" % theirs).status_code == 401, (
        "an anonymous read must stay 401; this test is about the 403 below")
    response = client.get("/monthly-event/%s" % mine, headers=_authed(theirs))
    assert response.status_code == 403, (
        "a live session for another player answered %d, not 403"
        % response.status_code)
    assert "does not match" in response.json()["detail"], (
        "the refusal must name the mismatch: %r" % response.json()["detail"])


def test_own_session_gets_past_auth_and_reaches_the_handler():
    """The 403 above is about ownership, not a broken route: the same call with
    the caller's OWN session reaches the handler.

    MEASURED 2026-10-05: a real player with its own session is answered 200 with
    the event banner. The earlier version of this test used a player id the
    store never had and expected 404, which the ``session_player_deleted`` guard
    now answers 401 instead -- see the test above for why that order is correct.
    """
    client = _live_client()
    (mine,) = _real_players(client, "own_session_owner")
    response = client.get("/monthly-event/%s" % mine, headers=_authed(mine))
    assert response.status_code == 200, (
        "the caller's own session did not reach the handler: %d"
        % response.status_code)
    assert set(response.json()) == STATE_KEYS, (
        "own-session read answered a different shape than the anonymous banner")

    # CONTROL: the same call with somebody else's session is the 403 above. Both
    # halves are here so this cannot pass on a route that refuses everyone.


def test_the_bare_module_mount_is_deliberately_open_and_this_test_says_so():
    """CONTROL for the three tests above.

    The same four routes on an app with no guard installed answer an anonymous
    caller with 200. If the 401s ever stop being real, this is the shape that
    would slip through: the guard is what closes them, not the module.
    """
    app, _ = _seeded_app("p1")
    response = TestClient(app).get("/monthly-event/p1")
    assert response.status_code == 200, (
        "if this now answers 401, the module grew its own auth and the "
        "'auth is central' framing of this file is out of date")


# --------------------------------------------------------------------------
# D. claim-daily: shape, once-only, and the day the window has no reward
# --------------------------------------------------------------------------

@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_claim_daily_returns_the_four_documented_keys(theme, monkeypatch,
                                                     app_with_players):
    """MEASURED on both themes (2026-10-04): the reply carries the same four
    keys in either window, and ``reward`` is the row the RUNNING window's own
    table names -- "Ice Essence x5" under North Mystic, "Moon Essence x5"
    under Moon Palace. The key set alone would not discriminate: a route that
    served one theme's table under both would still answer 200 with four keys.
    The theme-sensitive half is the ``reward`` equality asserted here.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    response = TestClient(app).post("/monthly-event/p1/claim-daily")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == CLAIM_KEYS
    assert body["ok"] is True
    assert body["day"] == 1
    assert body["reward"] == me.get_daily_reward(theme, 1)
    assert set(body["battle_pass"]) == BATTLE_PASS_KEYS
    assert body["battle_pass"]["source"] == "monthly_event_daily"


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_claim_daily_credits_exactly_what_the_table_promises(theme, monkeypatch,
                                                             app_with_players):
    """The reply and the stored row must agree, and both must agree with the
    theme's reward table. A reward advertised but never credited is the defect
    this pins.

    MEASURED on both themes (2026-10-04): each day-1 claim credits its OWN
    item -- ``inventory["Ice Essence x5"]`` under North Mystic,
    ``inventory["Moon Essence x5"]`` under Moon Palace. This is the strongest
    theme discriminator in the claim block, because the two day-1 rows share
    qi 10 and legacy 5 and differ ONLY in the item name: a route reading one
    hard-coded table writes the other window's item into the inventory and
    fails the assert below while still paying the right qi. Before this the
    pair of tables was only ever checked against North Mystic.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    body = TestClient(app).post("/monthly-event/p1/claim-daily").json()
    promised = me.get_daily_reward(theme, body["day"])
    assert body["reward"] == promised

    row = store["p1"]
    assert row["progress"] == promised["qi"]
    assert row["legacy_bonus"] == float(promised["legacy"])
    for item in promised["items"]:
        assert row["inventory"][item] == 1
    # The mirror case: the other window's day-1 item must be absent, so a
    # row that carried both would fail rather than pass on the +1 above.
    other = CelestialTheme.MOON_PALACE if theme == CelestialTheme.NORTH_MYSTIC \
        else CelestialTheme.NORTH_MYSTIC
    for item in me.get_daily_reward(other, 1)["items"]:
        assert item not in row["inventory"], (
            "the %s window credited the %s day-1 item" % (theme.value, other.value))


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_claim_daily_is_once_per_day(theme, monkeypatch, app_with_players):
    """A second press the same day must not pay again. Checked on the balance,
    not on the status code alone.

    MEASURED on both themes (2026-10-04): the second press answers 409 and the
    balance sits at the first press's number in either window.
    HONEST LIMIT: this does NOT discriminate the theme. The once-only guard
    reads the player's own ``claimed_days`` marker and never consults the
    theme, so a route serving the wrong window's table still answers 409 on
    the replay. What the parameterisation adds is the guard holding in a
    window whose day-1 reward differs, so the "balance did not move" arm is
    measured against a second, different reward number rather than the same
    one twice.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    client = TestClient(app)
    assert client.post("/monthly-event/p1/claim-daily").status_code == 200
    paid = store["p1"]["progress"]
    assert paid == me.get_daily_reward(theme, 1)["qi"], (
        "the first press did not pay this window's own reward, so the replay "
        "below would prove nothing")
    assert client.post("/monthly-event/p1/claim-daily").status_code == 409
    assert store["p1"]["progress"] == paid, "the second press paid a second time"
    assert store["p1"]["monthly_event"]["claimed_days"].count(1) == 1


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_a_day_with_no_reward_is_404_and_pays_nothing(theme, monkeypatch,
                                                      app_with_players):
    """Days 2, 4, 5 and so on carry no reward. The claim must REFUSE, not pay
    zero and answer 200 -- otherwise a client would show an empty reward.

    MEASURED on both themes (2026-10-04): both tables run days 1, 3, 7 and 14
    and carry nothing on day 2, so the 404 is the missing-reward-row refusal
    in either window. The theme-varying half is the assert below, which proves
    the refusal is that row and not a wrong-theme lookup happening to 404 for
    some other reason.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    assert me.get_daily_reward(theme, 2) is None, (
        "the %s table now pays on day 2, so this would measure a different "
        "refusal" % theme.value)
    clock.set(clock.epoch + me.DAY)  # day 2: no reward row in either theme
    response = TestClient(app).post("/monthly-event/p1/claim-daily")
    assert response.status_code == 404
    assert store["p1"].get("progress", 0.0) == 0.0


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_claim_daily_is_refused_outside_the_event_window(theme, monkeypatch,
                                                         app_with_players):
    """MEASURED on both themes (2026-10-04): the clock is moved to the first
    day past the window and the claim answers 400 with nothing credited, from
    either window's start.

    HONEST LIMIT: the refusal is theme-invariant by construction. The
    ``active`` gate is checked before the handler reads the theme, so the 400
    is the same either way and a route serving the wrong window's table would
    leave this green -- which is correct for a refusal, not a gap. What the
    parameterisation proves is that the gate still fires after the clock has
    left a Moon Palace window, not only a North Mystic one.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    inactive = _inactive_day()  # first day past the window
    clock.set(inactive)
    response = TestClient(app).post("/monthly-event/p1/claim-daily")
    assert response.status_code == 400
    assert store["p1"].get("progress", 0.0) == 0.0


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_the_aura_is_not_awarded_before_the_day_that_pays_for_it(theme, monkeypatch,
                                                                app_with_players):
    """The day-14 aura must not turn up on a cheap early claim.

    MEASURED: the claim path reads ``THEME_EVENT_COSMETIC`` and asks whether the
    day being claimed is ``EVENT_AURA_DAY`` (14) before granting. Days 1, 3 and
    7 each carry their own reward but NOT the aura, and a claim on any of them
    must leave the player without it.

    MEASURED on both themes (2026-10-04): each window's three early claims pay
    their own rows -- the day-1, day-3 and day-7 items all differ between
    North Mystic and Moon Palace -- and none writes ``cosmetic_awarded``. The
    theme-sensitive half is the inventory check at the bottom, which would
    catch a claim that paid qi correctly while crediting the other window's
    items.

    This is a refusal-shaped test, so it is pinned on the STORED row, not on a
    status code: every one of these claims answers 200.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    client = TestClient(app)
    # Captured BEFORE the loop: the clock helper rewrites its own epoch, so
    # reading `clock.epoch` inside the loop would add each offset to the
    # PREVIOUS offset and walk to days that carry no reward row at all.
    base = clock.epoch
    for day_offset in (0, 2, 6):  # event days 1, 3 and 7 -- the reward rows
        clock.set(base + day_offset * me.DAY)
        response = client.post("/monthly-event/p1/claim-daily")
        assert response.status_code == 200, (
            "day offset %d did not pay its own reward; this test would then be "
            "measuring a broken claim path, not the aura gate" % day_offset)

    event_state = store["p1"]["monthly_event"]
    assert "cosmetic_awarded" not in event_state, (
        "the player was handed the day-%d aura on an early claim"
        % me.EVENT_AURA_DAY)
    assert not store["p1"].get("cosmetics"), (
        "the player owns a cosmetic it never earned")
    # The three claims above DID pay their own rewards, so this is a gate on
    # the aura and not a claim path that silently did nothing. Pinning the
    # item names is what makes the arm theme-sensitive: the two windows hand
    # out three differently-named items each.
    inventory = store["p1"]["inventory"]
    for day in (1, 3, 7):
        for item in me.get_daily_reward(theme, day)["items"]:
            assert inventory.get(item) == 1, (
                "the %s window did not credit its own day-%d item %r"
                % (theme.value, day, item))
    assert store["p1"]["progress"] > 0.0
    assert event_state["claimed_days"] == [1, 3, 7]


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_the_aura_is_awarded_on_the_day_that_pays_for_it_under_a_catalogue_id(theme, monkeypatch):
    """Event day 14 grants the aura, and the stored id must be one the cosmetics
    catalogue can resolve.

    A row written under a display label resolves in no catalogue lookup and
    appears in no player-facing list: a reward the player can never see or wear.
    That is what this pins, alongside the day the grant belongs to.

    MEASURED on both themes (2026-10-04): each window's day-14 claim writes the
    id its OWN theme names -- ``aura_north_mystic`` and ``aura_moon_palace``
    respectively. Parametrised because the grant is read out of
    ``THEME_EVENT_COSMETIC[theme]`` and that lookup is exactly where a
    half-fix would hide: a Moon Palace month that resolved the wrong entry, or
    granted nothing at all, still leaves the North Mystic case green.
    """
    app, store = _seeded_app("p1")
    base = _active_start_for(theme, 0)
    clock = _PinnedClock(base)
    # monkeypatch, not a bare assignment: this module's clock is process-wide
    # state, so a test that left it pinned would hand the NEXT test a fake day.
    monkeypatch.setattr(me.time, "time", clock)
    # Offset 13 is event day 14: the last day the window is open.
    clock.set(base + (me.EVENT_AURA_DAY - 1) * me.DAY)

    response = TestClient(app).post("/monthly-event/p1/claim-daily")
    assert response.status_code == 200
    assert response.json()["day"] == me.EVENT_AURA_DAY

    # The clock is now ON the aura day, so the theme is read here, not from the
    # day-0 epoch the loop above moved away from.
    assert me.get_current_theme(clock.epoch) is theme
    row = store["p1"]
    awarded = row["monthly_event"]["cosmetic_awarded"]
    expected = THEME_EVENT_COSMETIC[theme.value]
    assert awarded == expected, (
        "the %s window granted %r instead of its own %r"
        % (theme.value, awarded, expected))
    assert cosmetics.get_cosmetic(awarded) is not None, (
        "the event cosmetic was granted under an id the catalogue does not know")
    assert row["cosmetics"][awarded]["owned"] is True


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_the_aura_is_not_awarded_on_day_one_in_any_theme(theme, monkeypatch):
    """The day-1 claim must write NO ``cosmetic_awarded``, in either theme.

    This is the specific shape of the bug that shipped: the grant ran on every
    claim, so the cheapest day of the window handed out the headline cosmetic
    thirteen days early and then recorded that day 14 had happened.

    MEASURED on both themes (2026-10-04): the day-1 claim answers 200, credits
    its own reward, and leaves ``cosmetic_awarded`` absent and the cosmetics
    table empty. Pinned on the stored row because the status code is 200 either
    way -- the grant failing open is invisible from the reply.
    """
    app, store = _seeded_app("p1")
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)

    response = TestClient(app).post("/monthly-event/p1/claim-daily")
    assert response.status_code == 200
    assert response.json()["day"] == 1

    row = store["p1"]
    assert "cosmetic_awarded" not in row["monthly_event"], (
        "the %s window awarded the day-%d aura on a day-1 claim"
        % (theme.value, me.EVENT_AURA_DAY))
    assert not row.get("cosmetics"), (
        "the player owns a cosmetic it never earned")
    # The claim did its real job, so this is a gate on the aura and not a claim
    # path that silently did nothing.
    assert row["progress"] > 0.0
    assert row["monthly_event"]["claimed_days"] == [1]


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_the_day_gate_is_what_withholds_the_aura_and_not_something_else(theme,
                                                                       monkeypatch):
    """CONTROL for the day-14 and day-1 tests above (both themes).

    Moving the named day the grant is keyed on to 1 -- and changing nothing
    else -- makes an early claim award the aura again, in either theme, and
    under the ID that window itself names. So the withholding is genuinely
    that day comparison, not a side effect of something else in the claim
    path (a refused grant, a catalogue miss, an expired trial).

    Parametrised for the same reason as the tests it controls: a control that
    only ever fired under North Mystic would not catch a day gate that had
    quietly become theme-conditional.

    This replaces the mutation on the shared source file with a rebinding of
    the module's own constant, so the tree another worker is editing is never
    written to.
    """
    app, store = _seeded_app("p1")
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    monkeypatch.setattr(me, "EVENT_AURA_DAY", 1)

    assert TestClient(app).post("/monthly-event/p1/claim-daily").status_code == 200
    assert "cosmetic_awarded" in store["p1"]["monthly_event"], (
        "the day gate did not bite: moving EVENT_AURA_DAY to the claimed day "
        "did not produce an early aura, so those two tests prove nothing")
    assert store["p1"]["monthly_event"]["cosmetic_awarded"] == \
        THEME_EVENT_COSMETIC[theme.value], (
        "the day gate opened, but under the wrong theme's aura id")


# --------------------------------------------------------------------------
# E. complete-quest: unlock order, once-only, half pay
# --------------------------------------------------------------------------

@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_complete_quest_returns_the_four_documented_keys(theme, monkeypatch,
                                                         app_with_players):
    """MEASURED on both themes (2026-10-04): the reply's ``quest`` is the row
    the RUNNING window's own chain names -- "Enter the Frozen Gate" under
    North Mystic, "Moonlit Arrival" under Moon Palace. A handler that read one
    hard-coded chain would answer 200 under both themes and still be wrong in
    one of them, so the theme is named rather than inherited from a fixture.

    The player is SEEDED first (2026-10-05). This route pays only once the quest
    criteria are met, so an unseeded player is refused 400 and this test was
    measuring nothing about the reply shape. That refusal is itself pinned, by
    ``test_an_unseeded_player_is_refused_the_payout`` further down.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    _seed_quest_criteria(store, "p2", theme, 1)
    response = TestClient(app).post("/monthly-event/p2/complete-quest",
                                    params={"quest_day": 1})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == QUEST_KEYS
    assert body["ok"] is True
    assert body["quest"] == me.get_quest_for_day(theme, 1)
    assert set(body["battle_pass"]) == BATTLE_PASS_KEYS
    assert body["battle_pass"]["source"] == "monthly_event_quest"


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_complete_quest_pays_half_the_daily_reward(theme, monkeypatch,
                                                   app_with_players):
    """The route pays the day's reward at one half. Pinned as a relationship
    (half of the table), not as a literal, so a table change does not need a
    test edit but a pay change does.

    MEASURED on both themes (2026-10-04): each pays half of ITS OWN day-1 row.
    HONEST LIMIT: this one does NOT discriminate the theme. Sabotaging the
    route to serve the North Mystic table while this test expects Moon Palace
    leaves it green, because both day-1 rows carry qi 10 and legacy 5 and only
    the item name differs -- and the quest route pays qi and legacy only, never
    the items. So this pins "half, in whichever window is running", not "half
    of the running window's row". The theme-sensitive half of the quest path
    is carried by ``test_complete_quest_returns_the_four_documented_keys``.

    The pay is measured as a DELTA, not an absolute (2026-10-05). Seeding the
    criteria is not free: the Moon Palace day-1 quest is measured against
    ``qi_progress``, so the seed writes 100 onto the very field this route pays.
    An absolute assert would then read 105 and blame the payout for the fixture.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    _seed_quest_criteria(store, "p2", theme, 1)
    before_progress = float(store["p2"]["progress"])
    before_legacy = float(store["p2"].get("legacy_bonus", 0.0))

    assert TestClient(app).post("/monthly-event/p2/complete-quest",
                                params={"quest_day": 1}).status_code == 200
    day_reward = me.get_daily_reward(theme, 1)
    assert day_reward is not None, "day 1 must carry a reward in %s" % theme.value
    assert float(store["p2"]["progress"]) - before_progress == \
        pytest.approx(day_reward["qi"] * 0.5)
    assert float(store["p2"]["legacy_bonus"]) - before_legacy == \
        pytest.approx(day_reward["legacy"] * 0.5)


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_a_future_quest_day_is_400_and_pays_nothing(theme, monkeypatch,
                                                    app_with_players):
    """A quest the player has not reached yet is refused. On day 1 the day-3
    quest exists in the table and must still be refused -- the check is the
    current day, not the existence of the quest.

    MEASURED on both themes (2026-10-04): both chains carry a day-3 quest, so
    the 400 is the unlock gate in either window. This covers the REFUSAL, and
    a refusal is theme-invariant by construction -- sabotaging the route to
    serve the other theme leaves it green, which is the correct outcome here,
    not a gap. What it does pin per theme is that day 3 exists in both chains
    (the assert below), so the 400 is the unlock gate and not a 404.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    assert me.get_quest_for_day(theme, 3) is not None, (
        "the %s chain has no day-3 quest, so this would measure a 404" % theme.value)
    response = TestClient(app).post("/monthly-event/p2/complete-quest",
                                    params={"quest_day": 3})
    assert response.status_code == 400
    assert store["p2"].get("progress", 0.0) == 0.0


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_a_day_with_no_quest_is_404(theme, monkeypatch, app_with_players):
    """MEASURED on both themes (2026-10-04): day 2 carries no quest in either
    chain (both run 1, 3, 7, 14), so the route answers 404 rather than paying
    an empty reward. The theme-varying half is the assert below -- the 404
    itself is theme-invariant, and this file's theme control confirms it stays
    green under a wrong-theme route, which is correct for a refusal.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, _ = app_with_players
    assert me.get_quest_for_day(theme, 2) is None
    response = TestClient(app).post("/monthly-event/p2/complete-quest",
                                    params={"quest_day": 2})
    assert response.status_code == 404


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_complete_quest_is_once_only(theme, monkeypatch, app_with_players):
    """Checked on the balance: the marker is appended and the credit happen in
    one transaction, so a replay must move neither. MEASURED on both themes
    (2026-10-04). The once-only guard reads the player's own marker, never the
    theme, so it is theme-invariant by construction and this file's theme
    control confirms it stays green under a wrong-theme route. What the
    parameterisation adds is the guard holding in a window whose tables
    differ, not a new discriminator.

    Seeded (2026-10-05) so the FIRST press reaches the payout at all; the
    409-under-test is the second one.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    _seed_quest_criteria(store, "p2", theme, 1)
    client = TestClient(app)
    assert client.post("/monthly-event/p2/complete-quest",
                       params={"quest_day": 1}).status_code == 200
    paid = float(store["p2"]["progress"])
    assert paid > 0.0, "the first press paid nothing, so the replay proves nothing"
    assert client.post("/monthly-event/p2/complete-quest",
                       params={"quest_day": 1}).status_code == 409
    assert float(store["p2"]["progress"]) == paid, "the replay paid a second time"
    assert store["p2"]["monthly_event"]["claimed_quests"].count(1) == 1


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_quest_day_is_a_query_parameter_and_omitting_it_is_422(theme, monkeypatch,
                                                               app_with_players):
    """There is no request body on this route. A client that sends
    ``{"quest_day": 1}`` as JSON gets a 422, because nothing was sent in the
    query string. The request validator runs before the handler reads the
    theme, so this answers the same in both windows -- which is the point, and
    why the theme control leaves it green rather than treating it as a gap.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, _ = app_with_players
    assert TestClient(app).post("/monthly-event/p1/complete-quest").status_code == 422
    assert TestClient(app).post("/monthly-event/p1/complete-quest",
                                json={"quest_day": 1}).status_code == 422


# --------------------------------------------------------------------------
# F. buy-shop: the debit, the shelf, and every way it refuses
# --------------------------------------------------------------------------

def _shop_item(kind: str, theme: CelestialTheme | None = None) -> str:
    """A shelf label of the given ``type`` for ``theme``.

    The default reads the theme off the FIRST live window, which is always
    North Mystic. Inside a pinned Moon Palace window that silently hands back
    a North Mystic label, so every themed test passes its own ``theme``.
    """
    config = me.THEME_CONFIG[theme or me.get_current_theme(_active_start(0))]
    return next(name for name, spec in config["shop_items"].items()
                if spec["type"] == kind)


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_buy_shop_returns_the_four_documented_keys(theme, monkeypatch,
                                                    app_with_players):
    """MEASURED on both themes (2026-10-04): the two shelves are disjoint and
    denominated in different currencies, so a reply built from the wrong
    theme's config still has the same four keys and is still 200. The keys are
    not what makes this theme-sensitive -- the label reaching the route is.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    currency = me.get_event_state(clock.epoch)["shop_currency"]
    _grant_currency(store, "p1", currency, 10_000)
    response = TestClient(app).post("/monthly-event/p1/buy-shop",
                                    params={"item_id": _shop_item("material", theme),
                                            "quantity": 2})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == SHOP_KEYS
    assert body["ok"] is True
    assert body["quantity"] == 2


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_buy_shop_charges_cost_times_quantity_and_says_the_remainder(theme, monkeypatch,
                                                                    app_with_players):
    """The charge is the relationship: unit price times quantity, and the reply's
    remainder is what the stored balance holds. Both are checked, so a route
    that charged but mis-reported is caught too.

    MEASURED on both themes (2026-10-04): North Star Dust against Ice Essence
    (50 each) and Moon Essence against Moon Essence x10 (50 each). The two
    costs are coincidentally equal today; the currency NAMES are not, and the
    stored balance is read under this theme's own key.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    config = me.THEME_CONFIG[theme]
    currency = config["shop_currency"]
    start = 10_000
    _grant_currency(store, "p1", currency, start)

    item_id = _shop_item("material", theme)
    unit = config["shop_items"][item_id]["cost"]
    quantity = 3
    response = TestClient(app).post("/monthly-event/p1/buy-shop",
                                    params={"item_id": item_id, "quantity": quantity})
    assert response.status_code == 200
    expected = start - unit * quantity
    assert response.json()["remaining_currency"] == expected
    assert store["p1"]["monthly_event"][currency] == expected
    assert store["p1"]["inventory"][item_id] == quantity


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_buy_shop_quantity_is_bounded_to_one_through_ninety_nine(theme, monkeypatch,
                                                                 app_with_players):
    """The bound is enforced by the request validator, so the refusal is a 422
    and arrives BEFORE the handler -- including before the player is charged.
    MEASURED on both themes (2026-10-04). The bound is a literal on the
    request, not a table lookup, so it is theme-invariant: the theme control
    leaves it green, correctly. The parameterisation is what proves the
    pre-handler ordering holds in a Moon Palace window too, where the shelf
    and currency keys differ.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    currency = me.get_event_state(clock.epoch)["shop_currency"]
    _grant_currency(store, "p1", currency, 10_000)
    client = TestClient(app)
    item_id = _shop_item("material", theme)
    assert client.post("/monthly-event/p1/buy-shop",
                       params={"item_id": item_id, "quantity": 0}).status_code == 422
    assert client.post("/monthly-event/p1/buy-shop",
                       params={"item_id": item_id, "quantity": 100}).status_code == 422
    assert store["p1"]["monthly_event"][currency] == 10_000, (
        "a rejected quantity still moved the player's currency")


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_buy_shop_without_currency_is_400_and_leaves_the_balance_alone(theme, monkeypatch,
                                                                      app_with_players):
    """A refused purchase must not cost the player anything: the balance before
    and after has to be the same number. MEASURED on both themes (2026-10-04):
    both shelves are empty of balance at zero, so the refusal is the
    insufficiency check and not a missing shelf."""
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    currency = me.get_event_state(clock.epoch)["shop_currency"]
    _grant_currency(store, "p2", currency, 0)
    response = TestClient(app).post("/monthly-event/p2/buy-shop",
                                    params={"item_id": _shop_item("material", theme)})
    assert response.status_code == 400
    assert store["p2"]["monthly_event"][currency] == 0


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_buy_shop_refuses_an_item_that_is_not_on_this_theme_shelf(theme, monkeypatch,
                                                                  app_with_players):
    """Each theme has its own shelf with its own currency. The other theme's
    item must not be sellable.

    MEASURED on both themes (2026-10-04): each window answers 404 for the
    foreign label. This was the single most North-Mystic-bound test in the
    file -- with the fixture clock it only ever asked "can a North Mystic
    month sell a Moon Palace item", so the mirror case (a Moon Palace month
    selling "Ice Essence (x10)") had never been asked.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, _ = app_with_players
    assert me.get_current_theme(clock.epoch) is theme, (
        "the pinned clock is not in the %s window" % theme.value)
    other = (CelestialTheme.NORTH_MYSTIC if theme == CelestialTheme.MOON_PALACE
             else CelestialTheme.MOON_PALACE)
    foreign = _shop_item("material", theme=other)
    assert foreign not in me.THEME_CONFIG[theme]["shop_items"], (
        "the two themes share the label %r, so this cannot discriminate" % foreign)
    response = TestClient(app).post("/monthly-event/p1/buy-shop",
                                    params={"item_id": foreign})
    assert response.status_code == 404


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_buy_shop_missing_item_id_is_422(theme, monkeypatch, app_with_players):
    """No request body here either; ``item_id`` is a query parameter.
    MEASURED on both themes (2026-10-04): the validator answers before the
    handler reads the theme, so this is theme-invariant and the theme control
    correctly leaves it green. What the parameterisation buys is the 422
    proven in the Moon Palace window rather than only the North Mystic one.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, _ = app_with_players
    assert TestClient(app).post("/monthly-event/p1/buy-shop").status_code == 422


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_a_cosmetic_purchase_grants_a_catalogue_id_not_a_display_label(theme, monkeypatch,
                                                                      app_with_players):
    """The bug this route was patched for: it once charged 500 units and wrote
    a row keyed by the shelf's display name, which no catalogue lookup could
    resolve -- a purchase the player could never see or wear. Pinned on the
    stored key, not on the status code.

    MEASURED on both themes (2026-10-04): "North Mystic Aura" resolves to
    ``aura_north_mystic`` and "Moon Palace Aura" to ``aura_moon_palace``, each
    charged under its own currency. The 500 is read from the theme's own shelf
    rather than written as a literal, so a re-priced aura does not need a test
    edit.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    config = me.THEME_CONFIG[theme]
    currency = me.get_event_state(clock.epoch)["shop_currency"]
    _grant_currency(store, "p1", currency, 10_000)
    aura_label = _shop_item("cosmetic", theme)
    cost = config["shop_items"][aura_label]["cost"]

    response = TestClient(app).post("/monthly-event/p1/buy-shop",
                                    params={"item_id": aura_label})
    assert response.status_code == 200
    expected_id = me.SHOP_COSMETIC_ID[aura_label]
    owned = store["p1"]["cosmetics"]
    assert expected_id in owned, (
        "the cosmetic was bought under a label instead of a catalogue id")
    assert cosmetics.get_cosmetic(expected_id) is not None
    assert aura_label not in owned, "a display label reached the cosmetics table"
    assert response.json()["remaining_currency"] == 10_000 - cost


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_buying_a_cosmetic_twice_is_refused_and_does_not_charge_twice(theme, monkeypatch,
                                                                      app_with_players):
    """MEASURED on both themes (2026-10-04): the owns-check resolves the
    shelf label to the catalogue id before refusing, so the second press is a
    400 in either window and the balance sits at the post-first-purchase
    number."""
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    currency = me.get_event_state(clock.epoch)["shop_currency"]
    _grant_currency(store, "p1", currency, 10_000)
    client = TestClient(app)
    aura = _shop_item("cosmetic", theme)
    assert client.post("/monthly-event/p1/buy-shop",
                       params={"item_id": aura}).status_code == 200
    after_first = store["p1"]["monthly_event"][currency]
    assert after_first < 10_000, "the first purchase was free, so a refusal proves little"
    assert client.post("/monthly-event/p1/buy-shop",
                       params={"item_id": aura}).status_code == 400
    assert store["p1"]["monthly_event"][currency] == after_first, (
        "the refused second purchase still charged the player")


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_buy_shop_is_refused_outside_the_event_window(theme, monkeypatch,
                                                      app_with_players):
    """The window gate is checked before the shelf, so the refusal is the same
    400 in either theme. MEASURED on both themes (2026-10-04)."""
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    inactive = _inactive_day()
    clock.set(inactive)
    response = TestClient(app).post("/monthly-event/p1/buy-shop",
                                    params={"item_id": _shop_item("material", theme)})
    assert response.status_code == 400
    assert "monthly_event" not in store["p1"] or not store["p1"]["monthly_event"], (
        "a refused purchase outside the window still wrote event state")


# --------------------------------------------------------------------------
# H. grant_event_currency: the only way event currency comes into existence
# --------------------------------------------------------------------------

def test_grant_event_currency_mints_the_currencies_the_shop_spends():
    """Every currency the three POST routes can debit has to be mintable, or the
    shop is unreachable in practice: nothing else in the game credits it.

    Pinned as a set relationship against the themes' own ``shop_currency``
    values, so a third theme with a third currency fails here rather than
    selling an empty shelf.
    """
    theme = CelestialTheme.NORTH_MYSTIC
    player = _player("p1")

    assert "monthly_event" not in player, "the fixture should start without it"
    me.grant_event_currency(player, theme, 40)
    assert player["monthly_event"] == {"North Star Dust": 40}

    # Minting is cumulative, not a replacement of the whole event block.
    me.grant_event_currency(player, theme, 2)
    assert player["monthly_event"] == {"North Star Dust": 42}

    me.grant_event_currency(player, CelestialTheme.MOON_PALACE, 7)
    assert player["monthly_event"]["Moon Essence"] == 7
    assert player["monthly_event"]["North Star Dust"] == 42


def test_every_shop_currency_is_mintable_and_readable_by_its_own_theme():
    """The minted key must be the SAME key the buying route debits.

    A mismatch is a silent failure in both directions: the mint writes
    ``North Star Dust`` into a row and the till reads ``Moon Essence``, so the
    balance the player was shown is not the balance that is charged.
    """
    player = _player("p1")
    for theme, config in me.THEME_CONFIG.items():
        currency = config["shop_currency"]
        me.grant_event_currency(player, theme, 5)
        assert player["monthly_event"].get(currency) == 5, (
            "%s minted a currency the %s shop cannot read" % (theme.value, theme.value))
        for shelf_item in config["shop_items"].values():
            assert shelf_item["currency"] == currency, (
                "%s sells an item denominated in %s while its till reads %s"
                % (theme.value, shelf_item["currency"], currency))


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_currency_minted_through_the_helper_is_what_the_till_debits(theme, monkeypatch,
                                                                   app_with_players):
    """End to end across the module boundary: mint with the helper, then spend
    with the route. This is the link the other two tests only assert in
    isolation, and it is the one a rename would break quietly.

    MEASURED on both themes (2026-10-04): minting 300 Moon Essence and buying
    "Moon Essence (x10" leaves 250 under the Moon Essence key; minting 300
    North Star Dust and buying Ice Essence leaves 250 under North Star Dust.
    Before this the whole path ran only under North Mystic, so a helper that
    minted the wrong currency name for the running theme would have gone
    unnoticed.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    currency = me.THEME_CONFIG[theme]["shop_currency"]

    # Seed the stored row the way the helper writes one, then persist.
    seed = _player("p1")
    me.grant_event_currency(seed, theme, 300)
    with store.transaction("p1"):
        store["p1"] = seed
        store.persist("p1", seed)

    item_id = _shop_item("material", theme=theme)
    unit = me.THEME_CONFIG[theme]["shop_items"][item_id]["cost"]
    response = TestClient(app).post("/monthly-event/p1/buy-shop",
                                    params={"item_id": item_id})
    assert response.status_code == 200
    assert response.json()["remaining_currency"] == 300 - unit
    assert store["p1"]["monthly_event"][currency] == 300 - unit


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_a_refused_purchase_never_debits_currency_the_helper_minted(theme, monkeypatch,
                                                                    app_with_players):
    """The balance minted above has to survive a refusal untouched. Checked on
    the stored number, because the reply alone would look the same either way.

    MEASURED on both themes (2026-10-04): one unit of each currency is less
    than the cheapest shelf item in both windows, so the refusal is the
    insufficiency check and the single unit survives.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    currency = me.THEME_CONFIG[theme]["shop_currency"]
    seed = _player("p1")
    me.grant_event_currency(seed, theme, 1)  # cannot afford anything
    with store.transaction("p1"):
        store["p1"] = seed
        store.persist("p1", seed)

    item_id = _shop_item("material", theme=theme)
    assert me.THEME_CONFIG[theme]["shop_items"][item_id]["cost"] > 1, (
        "one unit of %s would afford %s, so this would not be a refusal"
        % (currency, item_id))
    response = TestClient(app).post("/monthly-event/p1/buy-shop",
                                    params={"item_id": item_id})
    assert response.status_code == 400
    assert store["p1"]["monthly_event"][currency] == 1, (
        "a refused purchase debited currency the player still holds")


# --------------------------------------------------------------------------
# G. The clock itself: the theme must not flip mid-event
# --------------------------------------------------------------------------

def test_the_theme_is_constant_across_the_whole_window():
    """CONTROL for the arithmetic this module depends on.

    Both the theme and the event's own length are derived from the day number,
    so the theme is checked hourly across a full 30-day cycle and must be one
    value inside the window. The regression this catches is a divisor that
    silently counts seconds, which re-picks the theme every few minutes and
    swaps the player's shop currency under them.
    """
    # The stride is 3601 seconds, which shares no factor with the 360-second
    # divisor the old buggy arithmetic used. A "nice" stride such as one
    # quarter-day is an exact multiple of 360, so it samples the same phase of
    # the bug every time and the control stays green -- the theme would flip
    # between samples and still look constant.
    stride = 3601
    start = _active_start(0)
    themes = {me.get_current_theme(start + offset)
              for offset in range(0, me.CYCLE_DAYS * me.DAY, stride)}
    assert len(themes) == 1, (
        "the theme changed %d times inside one %d-day cycle" % (
            len(themes), me.CYCLE_DAYS))


def test_the_two_themes_alternate_month_by_month():
    """A relationship, not a snapshot: consecutive cycles carry different
    themes, and the whole set is exactly the two declared ones."""
    start = _active_start(0)
    cycle = me.CYCLE_DAYS * me.DAY
    seen = [me.get_current_theme(start + n * cycle) for n in range(4)]
    assert len(set(seen)) == 2, seen
    assert set(seen) <= set(me.THEME_CONFIG), seen


def test_every_event_cosmetic_id_resolves_in_the_catalogue():
    """Both mapping tables must name ids the catalogue knows. A rename on either
    side should fail here rather than quietly grant something invisible."""
    for cosmetic_id in list(THEME_EVENT_COSMETIC.values()) + \
            list(me.SHOP_COSMETIC_ID.values()):
        assert cosmetics.get_cosmetic(cosmetic_id) is not None, cosmetic_id


def test_the_state_read_describes_the_window_the_clock_is_actually_in():
    """day_of_month, days_remaining and next_event_in must agree with each other
    and with `active`, at both ends of the window."""
    for offset, expect_active in ((0, True), (EVENT_DURATION_DAYS - 1, True),
                                  (EVENT_DURATION_DAYS, False)):
        state = me.get_event_state(_active_start(0) + offset * me.DAY)
        assert state["active"] is expect_active
        if expect_active:
            assert state["next_event_in"] == 0
            assert state["days_remaining"] == EVENT_DURATION_DAYS - state["day_of_month"] + 1
        else:
            assert state["days_remaining"] == 0
            assert state["next_event_in"] > 0


# --------------------------------------------------------------------------
# I. THE FAUCET: the daily claim is what funds the shop
#
# Before this block, `grant_event_currency` had ZERO callers in the whole
# tree. It is the only writer of `player["monthly_event"][<currency>]`, and
# nothing in the game called it, so every player arrived at the shop holding
# 0 and every purchase was answered 400 "Insufficient currency". The till was
# correct and unfunded.
#
# These tests are written to FAIL on the pre-fix file. That was measured, not
# assumed: see the A/B in the run notes.
# --------------------------------------------------------------------------

def test_claiming_a_day_funds_the_shop_currency_the_till_debits(monkeypatch,
                                                                app_with_players):
    """THE regression. One claim, then the balance the till actually reads.

    Asserted on the STORE, not on the reply: `claim-daily` answers 200 and
    reports a reward either way, so a reply-only assertion would stay green on
    the broken file. That is the whole failure being fixed.
    """
    clock = _PinnedClock(_active_start_for(CelestialTheme.NORTH_MYSTIC, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    currency = me.THEME_CONFIG[CelestialTheme.NORTH_MYSTIC]["shop_currency"]

    assert TestClient(app).post("/monthly-event/p1/claim-daily").status_code == 200
    held = store["p1"].get("monthly_event", {}).get(currency, 0)
    assert held == me.EVENT_CURRENCY_REWARD[1], (
        "the daily claim paid %r of %s; nothing funds the shop, so every "
        "purchase is refused for insufficient currency" % (held, currency))


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_day_one_alone_can_afford_the_cheapest_item_on_the_shelf(theme, monkeypatch,
                                                                 app_with_players):
    """The FIRST claim has to be worth something, or the fix is cosmetic.

    A faucet that only pays on day 14 leaves the player staring at a shelf they
    cannot touch for two weeks, which is the same dead shop wearing a hat.
    Cheapest item across BOTH shelves is 50, so day 1 has to clear it.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players

    assert TestClient(app).post("/monthly-event/p1/claim-daily").status_code == 200
    currency = me.THEME_CONFIG[theme]["shop_currency"]
    cheapest = min(item["cost"] for item in me.THEME_CONFIG[theme]["shop_items"].values())
    held = store["p1"]["monthly_event"][currency]
    assert held >= cheapest, (
        "day 1 paid %d %s and the cheapest %s item costs %d, so the player "
        "cannot buy anything on the first claim"
        % (held, currency, theme.value, cheapest))


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_currency_earned_by_claiming_is_spendable_end_to_end(theme, monkeypatch,
                                                            app_with_players):
    """Claim, then buy, with no hand-seeding anywhere.

    This is the join the helper tests above cannot make: they mint by calling
    the helper directly, so they stay green on a file where the CLAIM does not
    call it. Only the route proves the wiring.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    currency = me.THEME_CONFIG[theme]["shop_currency"]
    item_id = _shop_item("material", theme=theme)
    unit = me.THEME_CONFIG[theme]["shop_items"][item_id]["cost"]

    assert TestClient(app).post("/monthly-event/p1/claim-daily").status_code == 200
    earned = store["p1"]["monthly_event"][currency]
    assert earned >= unit, (
        "day 1 paid %d %s and %s costs %d, so this buys nothing and proves "
        "nothing about the wiring" % (earned, currency, item_id, unit))

    bought = TestClient(app).post("/monthly-event/p1/buy-shop",
                                  params={"item_id": item_id})
    assert bought.status_code == 200, bought.text
    assert bought.json()["remaining_currency"] == earned - unit
    assert store["p1"]["monthly_event"][currency] == earned - unit
    assert store["p1"]["inventory"][item_id] == 1


def test_a_replayed_daily_claim_mints_a_second_time(monkeypatch, app_with_players):
    """The faucet must ride the ONCE-ONLY guard, not bypass it.

    This is the security half of the change. A faucet added next to the reward
    grants is worthless if the marker can be bypassed: the second press must
    refuse AND leave the balance exactly where the first press left it.

    MEASURED 2026-10-05 by mutation, and the distinction matters. Two mutants
    were run and they behave DIFFERENTLY:

      * SINGLE-MINT ABOVE THE GUARD -- move the one faucet call to before the
        ``if day in claimed`` refusal. This stays GREEN, and that is a real
        property of the code rather than a weak test. ``PlayerStore`` is
        COPY-ON-READ (measured: a write to the dict handed back by ``store[pid]``
        is discarded unless ``persist`` runs), so a mint written above the guard
        lands on a snapshot the refusal then throws away. The guard and the
        store's copy-on-read are TWO independent defences, and this test pins
        the one a client can observe.
      * DOUBLE-MINT ABOVE THE GUARD -- the same move but LEAVING the original
        call in place. This goes RED here and in three other tests, because the
        surviving second call still sits inside the persisted region and pays
        twice on the first press.

    So the single-mint mutant is equivalent and needs no ratchet; the double
    mint is the failure worth pinning, and it is caught.
    """
    clock = _PinnedClock(_active_start_for(CelestialTheme.NORTH_MYSTIC, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    currency = me.THEME_CONFIG[CelestialTheme.NORTH_MYSTIC]["shop_currency"]
    client = TestClient(app)

    assert client.post("/monthly-event/p1/claim-daily").status_code == 200
    after_first = store["p1"]["monthly_event"][currency]

    assert client.post("/monthly-event/p1/claim-daily").status_code == 409
    assert store["p1"]["monthly_event"][currency] == after_first, (
        "the replayed claim minted a second time: %d -> %d"
        % (after_first, store["p1"]["monthly_event"][currency]))


def test_a_day_with_no_reward_pays_no_currency(monkeypatch, app_with_players):
    """Day 2 has no row in `daily_rewards`, so the claim is a 404 and must not
    mint. A faucet applied BEFORE the 404 would pay every day of the month."""
    clock = _PinnedClock(_active_start_for(CelestialTheme.NORTH_MYSTIC, 1))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    assert me.get_daily_reward(me.get_current_theme(clock()), 2) is None, (
        "day 2 now pays a reward, so this test measures nothing")
    currency = me.THEME_CONFIG[CelestialTheme.NORTH_MYSTIC]["shop_currency"]

    assert TestClient(app).post("/monthly-event/p1/claim-daily").status_code == 404
    assert not store["p1"].get("monthly_event", {}).get(currency), (
        "a 404 claim still minted shop currency")


def test_every_paid_day_names_a_day_both_reward_tables_actually_pay():
    """The two tables must stay in step, or a reward day silently pays nothing.

    A subset relation, not equality: a day may pay qi but no currency (the
    lookup floors that at 0). What must never happen is the reverse -- a day
    the rewards table pays while the currency table has never heard of it.
    """
    for theme, config in me.THEME_CONFIG.items():
        reward_days = set(config["daily_rewards"])
        missing = reward_days - set(me.EVENT_CURRENCY_REWARD)
        assert not missing, (
            "%s pays a reward on day(s) %s but EVENT_CURRENCY_REWARD has no "
            "amount for them, so those claims fund the shop with nothing"
            % (theme.value, sorted(missing)))
        extra = set(me.EVENT_CURRENCY_REWARD) - reward_days
        assert not extra, (
            "EVENT_CURRENCY_REWARD pays day(s) %s that %s's rewards table does "
            "not, so those amounts can never be claimed"
            % (sorted(extra), theme.value))


def test_the_faucet_never_subtracts_from_a_balance():
    """A grant that can go negative is a way to take a player's shop money.

    `amount` comes from a config table, so this guards a mistyped row rather
    than a hostile caller -- but the failure it prevents (a negative balance
    that later reads as "affordable") is the same either way.
    """
    for bad in (0, -1, -10_000, float("nan"), float("inf"), None, True, "10"):
        player = _player("p1")
        me.grant_event_currency(player, CelestialTheme.NORTH_MYSTIC, bad)
        held = player.get("monthly_event", {}).get("North Star Dust", 0)
        assert held in (0, None), (
            "grant_event_currency(player, %r) left a balance of %r" % (bad, held))


def test_a_corrupt_stored_balance_is_replaced_not_concatenated():
    """An older save can hold junk under the currency key. Reading it as a
    string used to be a TypeError on the very next claim; a float used to leave
    44.99999999999999 on the row."""
    for junk in ("North Star Dust", None, float("nan")):
        player = _player("p1")
        player["monthly_event"] = {"North Star Dust": junk}
        me.grant_event_currency(player, CelestialTheme.NORTH_MYSTIC, 10)
        held = player["monthly_event"]["North Star Dust"]
        assert held == 10, (
            "a stored balance of %r produced %r after a grant of 10"
            % (junk, held))


# ===========================================================================
# THE FOUR REQUIRED GUARANTEES, RE-MEASURED 2026-10-05
#
# Everything above was written against this module before its quest-criteria
# gate and its shop-currency faucet existed. These four sections pin the four
# guarantees that gate, the faucet, the window and the number handling actually
# make, and each one carries a control that has to go RED if the guarantee is
# removed -- a contract assertion that cannot fail documents nothing.
#
# Every figure below was produced by CALLING the code, not read off the source.
# ===========================================================================


# --------------------------------------------------------------------------
# 1. THE EVENT CURRENCY GRANT REQUIRES AUTH
#
# `grant_event_currency` is the only writer of the shop balance, so if a
# stranger can reach a claim they can mint shop money at will. It is measured
# on the SHIPPED app (`main.app`) with REAL players, because the module itself
# carries no auth: the closure lives in a shape-matching guard installed once in
# main.py, which a bare re-mount of these routes cannot show at all.
# --------------------------------------------------------------------------

def test_the_currency_faucet_is_closed_to_an_anonymous_caller(live_client):
    """MEASURED 2026-10-05: 401, and the balance stays absent from the row.

    Pinned on the STORE, not the status. A claim route that answered 401 and
    minted anyway would pass a status-only assert; ``monthly_event`` is simply
    not created on the row.
    """
    (pid,) = _real_players(live_client, "faucet_anon")
    assert not _numeric_event_total(main_module.players_db, pid), (
        "the fixture already holds event currency; this measures nothing")

    response = live_client.post("/monthly-event/%s/claim-daily" % pid)
    assert response.status_code == 401, (
        "an anonymous claim answered %d, not 401" % response.status_code)
    assert _numeric_event_total(main_module.players_db, pid) == 0, (
        "an anonymous claim minted event currency")


def test_the_currency_faucet_is_closed_to_another_players_session(live_client):
    """MEASURED 2026-10-05: 403 on all three spending routes, and the victim
    pays nothing.

    The second player really exists, so this is the ownership check and not the
    deleted-account check its sibling above measures.
    """
    victim, stranger = _real_players(live_client, "faucet_victim",
                                     "faucet_stranger")
    headers = _authed(stranger)
    # (method, path, query) -- the banner is a GET, so posting to it would
    # answer 405 and measure the verb rather than the ownership check.
    calls = [
        ("get", "/monthly-event/%s" % victim, None),
        ("post", "/monthly-event/%s/claim-daily" % victim, None),
        ("post", "/monthly-event/%s/complete-quest" % victim, {"quest_day": 1}),
        ("post", "/monthly-event/%s/buy-shop" % victim,
         {"item_id": "Ice Essence (x10)"}),
    ]
    for verb, path, params in calls:
        response = getattr(live_client, verb)(path, params=params, headers=headers)
        assert response.status_code == 403, (
            "%s %s answered %d for a session belonging to somebody else"
            % (verb.upper(), path, response.status_code))
    assert _numeric_event_total(main_module.players_db, victim) == 0, (
        "another player's session minted currency for the victim")
    assert _numeric_event_total(main_module.players_db, stranger) == 0, (
        "another player's session minted currency for itself")


def test_the_owners_own_session_is_the_one_that_funds_the_shop(live_client):
    """CONTROL for the two refusals above, and the reason they are not vacuous.

    MEASURED 2026-10-05: the very same call that answered 401 above and 403 for
    the stranger answers 200 here and writes exactly the day-1 currency amount.
    If this ever fails, the refusals are a route refusing everyone.
    """
    (pid,) = _real_players(live_client, "faucet_owner")
    currency = me.THEME_CONFIG[me.get_current_theme(_active_start(0))]["shop_currency"]

    response = live_client.post("/monthly-event/%s/claim-daily" % pid,
                                headers=_authed(pid))
    assert response.status_code == 200, (
        "the owner's own claim answered %d" % response.status_code)
    held = _held_currency(main_module.players_db, pid, currency)
    assert held == me.EVENT_CURRENCY_REWARD[1], (
        "the authenticated claim paid %r of %s, expected %r"
        % (held, currency, me.EVENT_CURRENCY_REWARD[1]))


def test_the_bare_module_would_have_minted_for_anyone(live_client):
    """CONTROL that names WHY the auth assertions above are necessary.

    The same route on an app with no guard installed answers an anonymous
    caller 200 and mints. That is the shape that would slip through if the
    central guard were ever uninstalled from these paths, which is why the file
    measures auth on the live app and not here.
    """
    app, store = _seeded_app("p1")
    response = TestClient(app).post("/monthly-event/p1/claim-daily")
    assert response.status_code == 200, (
        "the bare mount now answers %d; if the module grew its own auth this "
        "file's central-guard framing is out of date" % response.status_code)
    currency = me.get_event_state(_active_start(0))["shop_currency"]
    assert _held_currency(store, "p1", currency) == me.EVENT_CURRENCY_REWARD[1], (
        "the unguarded mount did not mint, so the 401s above would be "
        "measuring a route that never pays")


# --------------------------------------------------------------------------
# 2. QUEST CRITERIA VERIFICATION PREVENTS INVALID CLAIMS
#
# `quest_criteria_met` is the only thing between a caller who did nothing and a
# free half-reward. It FAILS CLOSED: no row, an unknown source, a reader that
# raises, and a reader that returns a non-number all refuse. Every refusal arm
# is pinned here, because "could not evaluate it" quietly becoming "yes" is the
# failure mode that pays.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_an_unseeded_player_is_refused_the_quest_payout(theme, monkeypatch,
                                                        app_with_players):
    """MEASURED 2026-10-05 on both themes: 400, the balance does not move, and
    no ``claimed_quests`` marker is written.

    The refusal is theme-invariant by construction -- the gate reads the
    criteria table, not the prose -- so what the parameterisation adds is the
    gate proven in a window whose two tables differ, and the refusal proved in
    each window rather than only in North Mystic.

    CONTROL on the marker: it is asserted ABSENT, not merely empty. A gate
    placed after the append would burn the player's one attempt at a quest they
    have not finished, and the next press would answer 409 instead of paying.
    That is what the retry test below proves cannot happen.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    _criteria_met_unseeded(store, "p2", theme, 1)

    response = TestClient(app).post("/monthly-event/p2/complete-quest",
                                    params={"quest_day": 1})
    assert response.status_code == 400, (
        "a player who did the quest nothing was paid anyway: %d"
        % response.status_code)
    assert float(store["p2"].get("progress", 0.0)) == 0.0
    assert "claimed_quests" not in store["p2"].get("monthly_event", {}), (
        "a refused claim still burned the player's one attempt")


def test_a_refused_quest_can_be_claimed_once_the_criteria_are_met(monkeypatch,
                                                                 app_with_players):
    """CONTROL for the refusal above, and the reason the marker is not burned.

    MEASURED 2026-10-05: refuse, seed, retry -> 200 and exactly one payout. A
    gate that marked the quest on refusal would answer 409 here and strand the
    player, so this test fails if the ordering in the handler ever moves.
    """
    theme = CelestialTheme.NORTH_MYSTIC
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    client = TestClient(app)

    assert client.post("/monthly-event/p2/complete-quest",
                       params={"quest_day": 1}).status_code == 400
    _seed_quest_criteria(store, "p2", theme, 1)
    assert client.post("/monthly-event/p2/complete-quest",
                       params={"quest_day": 1}).status_code == 200, (
        "the quest was marked on the refusal, so the player cannot retry it")
    assert store["p2"]["monthly_event"]["claimed_quests"] == [1]
    # And exactly one payout: the retry is not a second claim on top of a first.
    reward = me.get_daily_reward(theme, 1)
    assert float(store["p2"]["progress"]) == pytest.approx(reward["qi"] * 0.5)


def test_the_criteria_reader_is_the_server_side_counter_not_the_request(
        monkeypatch, app_with_players):
    """A request body cannot talk its way past the gate.

    ``quest_criteria_met`` reads the player's OWN stored row and the request
    context; it never reads the body. MEASURED 2026-10-05: naming the source,
    the counter and the target in the JSON body changes nothing -- every call is
    the same 400 refusal, because nothing was seeded.

    CONTROL: with the criteria seeded, the identical request shape pays. So the
    400s above are the criteria gate and not the validator refusing a body.
    """
    theme = CelestialTheme.NORTH_MYSTIC
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = app_with_players
    client = TestClient(app)
    smuggle = {"combat_wins": 999, "progress": 999, "source": "combat_wins",
               "target": 1, "quest_day": 1}

    for _ in range(3):
        response = client.post("/monthly-event/p2/complete-quest",
                               params={"quest_day": 1}, json=smuggle)
        assert response.status_code == 400, (
            "a body carrying %r bought a quest" % sorted(smuggle))
    assert float(store["p2"].get("progress", 0.0)) == 0.0

    _seed_quest_criteria(store, "p2", theme, 1)
    assert client.post("/monthly-event/p2/complete-quest",
                       params={"quest_day": 1}, json=smuggle).status_code == 200, (
        "the identical request shape was refused even with the criteria met, so "
        "the 400s above are not a body validator")


def test_a_quest_with_no_criteria_row_refuses_rather_than_paying_free(
        monkeypatch, app_with_players):
    """FAIL CLOSED on a missing row.

    MEASURED 2026-10-05: a quest day outside the table answers
    ``(False, "Quest has no completion criteria")``. Paying here would be the
    whole failure the table exists to prevent -- a quest nobody wrote a counter
    for must never be a free reward.

    CONTROL: every quest that IS in the chain has a row (asserted below), so
    this arm is about the refusal path and not about a table that is simply
    empty.
    """
    theme = CelestialTheme.NORTH_MYSTIC
    met, detail = me.quest_criteria_met(_player("p2"), "p2", theme, 999)
    assert met is False
    assert "no completion criteria" in detail.lower()

    # Every real quest is covered, which is what makes the refusal above an
    # answer about an UNKNOWN day rather than a hole in the table.
    for other, config in me.THEME_CONFIG.items():
        for quest in config["quest_chain"]:
            assert (other.value, quest["day"]) in me.QUEST_CRITERIA, (
                "%s day %d pays but names no criteria"
                % (other.value, quest["day"]))


def test_every_criteria_row_names_a_reader_the_mission_board_actually_has(
        monkeypatch, app_with_players):
    """A row naming a source no reader answers to must REFUSE, not pay.

    ``mission_board.SOURCES`` is the set of counters the mission board and the
    achievements already derive from; a criteria row naming anything outside it
    is unreachable, and the only safe answer is a refusal.

    MEASURED 2026-10-05: a row naming ``no_such_source`` returns
    ``(False, "Quest criteria cannot be evaluated")``. The row is written into
    a copy of the table and the module's own dict is restored afterwards, so the
    shared tree is never touched and a failure here cannot leak into another
    test.
    """
    theme = CelestialTheme.NORTH_MYSTIC
    saved = dict(me.QUEST_CRITERIA)
    try:
        me.QUEST_CRITERIA[(theme.value, 1)] = {"source": "no_such_source",
                                               "target": 1}
        met, detail = me.quest_criteria_met(_player("p2"), "p2", theme, 1)
        assert met is False, "a criteria row naming an unknown source paid out"
        assert "cannot be evaluated" in detail.lower()
    finally:
        me.QUEST_CRITERIA.clear()
        me.QUEST_CRITERIA.update(saved)
    # And the restore is exact -- the table is back to the shipped rows.
    assert me.QUEST_CRITERIA == saved


def test_a_criteria_reader_that_raises_or_returns_junk_refuses(monkeypatch):
    """The two ways a real counter goes wrong, both of which must REFUSE.

    MEASURED 2026-10-05, all three arms returning
    ``(False, "Quest criteria cannot be evaluated")``:
      * a reader that raises -- a dependency died;
      * a reader returning a string -- ``int()`` raised;
      * a reader returning ``nan`` -- ``int()`` raised, and without the guard
        ``nan < target`` is False, so a naive comparison would have PAID.

    That last arm is the one worth keeping: a non-finite counter compared
    directly is not obviously wrong, it is silently permissive.
    """
    theme = CelestialTheme.NORTH_MYSTIC
    saved_criteria = dict(me.QUEST_CRITERIA)
    saved_sources = dict(mission_board.SOURCES)
    try:
        def raiser(_player_row, _ctx):
            raise RuntimeError("the counter's dependency is down")

        mission_board.SOURCES["probe_raises"] = raiser
        mission_board.SOURCES["probe_string"] = lambda p, c: "not a number"
        mission_board.SOURCES["probe_nan"] = lambda p, c: float("nan")
        mission_board.SOURCES["probe_inf"] = lambda p, c: float("inf")

        for name in ("probe_raises", "probe_string", "probe_nan", "probe_inf"):
            me.QUEST_CRITERIA[(theme.value, 1)] = {"source": name, "target": 1}
            met, detail = me.quest_criteria_met(_player("p2"), "p2", theme, 1)
            assert met is False, (
                "a %s counter paid out the quest" % name)
            assert "cannot be evaluated" in detail.lower(), detail
    finally:
        me.QUEST_CRITERIA.clear()
        me.QUEST_CRITERIA.update(saved_criteria)
        mission_board.SOURCES.clear()
        mission_board.SOURCES.update(saved_sources)


def test_the_criteria_boundary_is_exact_and_counts_come_from_the_ledger(
        monkeypatch):
    """One short of the target refuses; exactly the target pays.

    MEASURED 2026-10-05 against the North Mystic day-1 quest (3 combat wins):
    a ledger of 2 reads ``(False, "...(2/3)")`` and a ledger of 3 reads
    ``(True, ...)``. Pinned as a boundary rather than as a literal count, so a
    retuned target does not need a test edit but an off-by-one does.

    CONTROL: the count is read from the player's own ledger, so seeding a
    DIFFERENT player changes nothing here. A gate reading a global counter
    would let one player's farming open another player's quest.
    """
    theme = CelestialTheme.NORTH_MYSTIC
    row = me.QUEST_CRITERIA[(theme.value, 1)]
    target = int(row["target"])
    assert row["source"] == "combat_wins", (
        "the day-1 %s quest is no longer measured on combat wins; this test "
        "measures the wrong reader" % theme.value)

    ledger = missions.mission_store
    pid = "criteria_boundary_probe"
    short = target - 1
    ledger.record_event(pid, "combat", short)
    met, detail = me.quest_criteria_met(_player(pid), pid, theme, 1)
    assert met is False, (
        "%d of %d was accepted as complete" % (short, target))
    assert "%d/%d" % (short, target) in detail, (
        "the refusal must show the shortfall: %r" % detail)

    ledger.record_event(pid, "combat", 1)
    met, detail = me.quest_criteria_met(_player(pid), pid, theme, 1)
    assert met is True, "exactly %d was refused: %s" % (target, detail)

    # CONTROL: the count belongs to THIS player. A different id is untouched.
    other_met, _ = me.quest_criteria_met(_player("someone_else"), "someone_else",
                                         theme, 1)
    assert other_met is False, (
        "the criteria counter is shared between players; one player's farming "
        "opens another player's quest")


def test_every_criteria_row_reads_a_source_the_board_actually_exports():
    """Table integrity, as a relationship rather than a snapshot.

    Each row's ``source`` must be a key of ``mission_board.SOURCES`` -- the
    readers the mission board and the achievements already use. A rename on
    either side fails here rather than quietly turning every row into an
    unreachable, always-refused quest.
    """
    sources = {row["source"] for row in me.QUEST_CRITERIA.values()}
    unknown = sources - set(mission_board.SOURCES)
    assert not unknown, (
        "QUEST_CRITERIA names source(s) %s that mission_board.SOURCES does not "
        "export; those quests refuse for every player" % sorted(unknown))
    # Every row also carries a target a quest can actually be measured against.
    for (theme_value, day), row in me.QUEST_CRITERIA.items():
        assert int(row["target"]) > 0, (
            "%s day %d has a non-positive target %r, so the quest is either "
            "impossible or free" % (theme_value, day, row["target"]))


# --------------------------------------------------------------------------
# 3. EVENT BOUNDARIES ARE ENFORCED
#
# The window is 14 days of every 30. Everything that pays checks the clock
# first, so the boundary is a gate on all three POST routes -- not just on the
# claim. The state read stays open, which is what lets a client show a closed
# event instead of an error.
# --------------------------------------------------------------------------

def test_the_window_edge_is_the_last_day_that_pays_and_the_first_that_does_not(
        monkeypatch):
    """MEASURED 2026-10-05, both sides of the edge.

    ``day_of_month`` is 1-indexed in the reply, so the last live day reads 14
    and the first closed day reads 15. At day 14 the event is active, one day
    remains, and the claim answers 200; at day 15 it is inactive, zero days
    remain, and the same claim answers 400.

    CONTROL: the day-14 claim is asserted to have PAID on the stored row, so the
    400 at day 15 is the boundary and not a route that never pays.
    """
    last_live = _active_start(0) + (EVENT_DURATION_DAYS - 1) * me.DAY
    first_dead = _active_start(0) + EVENT_DURATION_DAYS * me.DAY

    last_state = me.get_event_state(last_live)
    assert last_state["active"] is True
    assert last_state["day_of_month"] == EVENT_DURATION_DAYS
    assert last_state["days_remaining"] == 1

    app, store = _seeded_app("p1")
    clock = _PinnedClock(last_live)
    monkeypatch.setattr(me.time, "time", clock)
    assert TestClient(app).post("/monthly-event/p1/claim-daily").status_code == 200
    assert float(store["p1"]["progress"]) > 0.0, (
        "the last live day did not pay, so the 400 below proves nothing")

    dead_state = me.get_event_state(first_dead)
    assert dead_state["active"] is False
    assert dead_state["day_of_month"] == EVENT_DURATION_DAYS + 1
    assert dead_state["days_remaining"] == 0
    clock.set(first_dead)
    assert TestClient(app).post("/monthly-event/p1/claim-daily").status_code == 400


def test_all_three_paying_routes_close_together_on_the_window(monkeypatch):
    """MEASURED 2026-10-05: outside the window all three POST routes answer 400
    and none of them writes to the player's row.

    The window gate is checked BEFORE the shelf lookup and BEFORE the quest
    table, which is what makes a single refusal cover three different routes:
    MEASURED, an item that does not exist and a quest day that does not exist
    both answer 400 outside the window and 404 inside it. A client therefore
    cannot use "does it exist?" as a probe for "is the event running?", and
    cannot learn the shelf while it is shut.

    CONTROL: the state READ stays open outside the window and reports
    ``active: false``, which is the one thing a client legitimately needs.
    """
    dead = _active_start(0) + EVENT_DURATION_DAYS * me.DAY
    app, store = _seeded_app("p1")
    clock = _PinnedClock(dead)
    monkeypatch.setattr(me.time, "time", clock)
    client = TestClient(app)

    assert client.post("/monthly-event/p1/claim-daily").status_code == 400
    assert client.post("/monthly-event/p1/buy-shop",
                       params={"item_id": "an item that does not exist"}
                       ).status_code == 400
    assert client.post("/monthly-event/p1/complete-quest",
                       params={"quest_day": 99}).status_code == 400
    assert not store["p1"].get("monthly_event"), (
        "a closed event still wrote event state onto the player row")

    banner = client.get("/monthly-event/p1")
    assert banner.status_code == 200, (
        "the state read must stay open outside the window so a client can show "
        "the event as closed")
    assert banner.json()["active"] is False
    assert banner.json()["days_remaining"] == 0
    assert banner.json()["next_event_in"] > 0


def test_the_window_refusal_costs_the_player_nothing(monkeypatch):
    """MEASURED 2026-10-05: a claim refused on the clock moves no balance.

    Pinned on the stored row for all three currencies that could plausibly be
    touched, because a refusal that debits is a worse bug than a refusal that
    pays.
    """
    dead = _active_start(0) + EVENT_DURATION_DAYS * me.DAY
    app, store = _seeded_app("p1")
    for theme, config in me.THEME_CONFIG.items():
        _grant_currency(store, "p1", config["shop_currency"], 500)

    clock = _PinnedClock(dead)
    monkeypatch.setattr(me.time, "time", clock)
    client = TestClient(app)
    assert client.post("/monthly-event/p1/claim-daily").status_code == 400
    assert client.post("/monthly-event/p1/complete-quest",
                       params={"quest_day": 1}).status_code == 400

    assert float(store["p1"].get("progress", 0.0)) == 0.0
    assert float(store["p1"].get("legacy_bonus", 0.0)) == 0.0
    for theme, config in me.THEME_CONFIG.items():
        assert _held_currency(store, "p1", config["shop_currency"]) == 500, (
            "a claim refused on the window moved the %s balance"
            % config["shop_currency"])
    assert not store["p1"].get("inventory"), "a refused claim still granted items"


def test_a_quest_day_is_unlocked_by_the_clock_and_not_by_the_table(monkeypatch):
    """A quest that EXISTS but is not yet reached is a 400, not a 404 and not a
    payout.

    MEASURED 2026-10-05: on event day 1 the day-3 quest exists in both chains
    and still answers 400 "Quest not unlocked yet". The distinction a client
    needs: 404 means "no such quest in this event", 400 means "not yet".

    CONTROL: the day-1 quest, which IS reached, passes the unlock gate and goes
    on to be refused by the criteria gate instead -- a different 400 with a
    different message. Both are 400 and a client cannot confuse the two reasons
    by status, so the messages are asserted to differ.
    """
    clock = _PinnedClock(_active_start(0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = _seeded_app("p1")
    client = TestClient(app)
    assert me.get_quest_for_day(me.get_current_theme(clock.epoch), 3) is not None

    locked = client.post("/monthly-event/p1/complete-quest", params={"quest_day": 3})
    assert locked.status_code == 400
    assert "unlocked" in locked.json()["detail"].lower()

    not_done = client.post("/monthly-event/p1/complete-quest", params={"quest_day": 1})
    assert not_done.status_code == 400
    assert "unlocked" not in not_done.json()["detail"].lower(), (
        "the criteria refusal reused the unlock message; a client cannot tell "
        "the player which one they hit")
    assert "quest" in not_done.json()["detail"].lower()


def test_the_aura_day_and_the_window_length_are_two_separate_facts():
    """The aura is gated on ``EVENT_AURA_DAY``, not on the window's length.

    They agree at 14 today, which is exactly why a test should pin the
    RELATIONSHIP rather than the coincidence: a change to the window length must
    not silently move or hide the day the headline cosmetic is earned.

    MEASURED 2026-10-05: the aura day is a key of BOTH themes' reward tables
    (so the day is claimed at all) and the currency table pays it too (so the
    day is worth claiming).
    """
    aura_day = me.EVENT_AURA_DAY
    assert 1 <= aura_day <= EVENT_DURATION_DAYS, (
        "the aura day %d falls outside the %d-day window, so it can never be "
        "reached" % (aura_day, EVENT_DURATION_DAYS))
    for theme, config in me.THEME_CONFIG.items():
        assert aura_day in config["daily_rewards"], (
            "%s pays no reward on the day the aura is awarded" % theme.value)
        assert aura_day in me.EVENT_CURRENCY_REWARD, (
            "the aura day pays no shop currency in %s, so the final claim of "
            "the window is worth the least" % theme.value)


def test_one_full_window_cannot_clear_the_whole_shelf_but_day_one_can_buy(
        monkeypatch):
    """The faucet is sized against the shelf, measured rather than asserted.

    MEASURED 2026-10-05 from the shipped tables: the window pays 340 in total
    while the non-aura items cost 350 between them, so a player who claims every
    paying day still cannot buy everything and has to choose. The cheapest item
    costs 50 and day 1 pays 60, so the FIRST claim is worth something -- a
    faucet that only paid on day 14 would leave the player unable to touch the
    shop for two weeks, which is the same dead shelf with a hat on.

    Asserted as inequalities against the tables, not as the literals 340/350/50,
    so a re-priced shelf does not need a test edit but a dead faucet does.
    """
    window_pay = sum(me.EVENT_CURRENCY_REWARD.values())
    for theme, config in me.THEME_CONFIG.items():
        costs = {name: spec["cost"] for name, spec in config["shop_items"].items()}
        non_aura = [c for name, c in costs.items()
                    if config["shop_items"][name]["type"] != "cosmetic"]
        cheapest = min(costs.values())
        day_one = me.EVENT_CURRENCY_REWARD[1]
        assert window_pay < sum(non_aura), (
            "a full window in %s (%d) now buys the entire non-aura shelf (%d); "
            "the event stops being a choice"
            % (theme.value, window_pay, sum(non_aura)))
        assert day_one >= cheapest, (
            "day 1 pays %d %s and the cheapest item costs %d, so the first "
            "claim buys nothing" % (day_one, config["shop_currency"], cheapest))


# --------------------------------------------------------------------------
# 4. NON-FINITE VALUES ARE REJECTED
#
# `float()` is not a guard. `float(float("inf"))` returns `inf` and raises
# nothing, so an unreadable balance becomes `inf + 5` written straight back
# onto the row. The other direction raises `OverflowError`, which is neither
# `TypeError` nor `ValueError`, so a guard catching only those two lets a
# 401-digit saved number 500 the claim mid-transaction.
#
# MEASURED 2026-10-05: one such defect is still live and is pinned as a strict
# xfail below rather than left to be rediscovered.
# --------------------------------------------------------------------------

def test_the_finite_helper_refuses_every_unreadable_number():
    """``_finite`` is the module's number gate. Every unreadable value must come
    back as the default, never as itself.

    MEASURED 2026-10-05:
      inf, -inf, nan, 10**400, "junk", None  -> 0.0 (the default)
      0 -> 0.0, -5 -> -5.0, 12.5 -> 12.5     (real numbers pass through)

    The ``10**400`` row is the one that separates this from a two-exception
    guard: converting it raises ``OverflowError``, which a handler catching
    only ``TypeError`` and ``ValueError`` lets straight through.
    """
    for junk in (float("inf"), float("-inf"), float("nan"), 10 ** 400,
                 "junk", None, object()):
        assert me._finite(junk) == 0.0, (
            "_finite(%r) returned %r instead of the default"
            % (junk, me._finite(junk)))
    # Real numbers pass through untouched, including negatives and fractions.
    for real in (0, -5, 12.5, 10 ** 308):
        assert me._finite(real) == float(real), real


def test_a_non_finite_grant_amount_is_refused_rather_than_written(
        monkeypatch, app_with_players):
    """The faucet refuses a non-finite amount and writes nothing at all.

    MEASURED 2026-10-05, for both themes: ``grant_event_currency`` called with
    ``inf``, ``-inf`` or ``nan`` leaves the row with no currency under either
    theme's key -- the function returns before touching the dict, so a stale
    balance is not even overwritten.

    The player's existing balance is asserted UNCHANGED rather than merely
    positive: a guard that wrote ``max(0, held) + 0`` would leave a zeroed
    balance behind, which is a different bug from the one this pins.
    """
    for theme, config in me.THEME_CONFIG.items():
        currency = config["shop_currency"]
        for junk in (float("inf"), float("-inf"), float("nan")):
            player = _player("p1")
            player["monthly_event"] = {currency: 250}
            me.grant_event_currency(player, theme, junk)
            held = player["monthly_event"].get(currency)
            assert held == 250, (
                "granting %r to a %s row holding 250 left %r"
                % (junk, theme.value, held))


def test_the_non_finite_amount_guard_is_a_guard_and_not_an_absence_of_one():
    """CONTROL for the refusal above: the same helper DOES mint a real amount.

    Without this arm the three refusals pass on a function that had been
    rewritten to do nothing at all, which is the failure a "never writes junk"
    assertion cannot see on its own.
    """
    player = _player("p1")
    me.grant_event_currency(player, CelestialTheme.NORTH_MYSTIC, 10)
    assert player["monthly_event"]["North Star Dust"] == 10
    # And the accepted amounts are the INTEGER ones the till compares against:
    # a float 10.7 must not leave 44.99999999999999 on the row.
    fractional = _player("p1")
    me.grant_event_currency(fractional, CelestialTheme.NORTH_MYSTIC, 10.7)
    held = fractional["monthly_event"]["North Star Dust"]
    assert held == 10 and isinstance(held, int), (
        "a fractional amount left %r of type %s on the row"
        % (held, type(held).__name__))


def test_a_non_finite_stored_legacy_bonus_costs_one_term_not_the_whole_payout(
        monkeypatch, app_with_players):
    """The claim route's own number guard, measured end to end.

    MEASURED 2026-10-05: a player row carrying ``legacy_bonus = inf`` still
    gets a 200 claim, and the stored ``legacy_bonus`` afterwards is the finite
    ``5.0`` -- the unreadable term is dropped and the reward still lands. That
    is the contract the module states: an unreadable balance is junk, so it
    costs the player this term of a sum rather than the whole payout.

    CONTROL: the same claim on a healthy row also ends at 5.0, so the repair is
    invisible from the outside -- which is the point. A guard that refused the
    whole claim instead would be a worse outcome, and the balance assertion
    below is what would catch it.
    """
    theme = CelestialTheme.NORTH_MYSTIC
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = _seeded_app("p1")
    client = TestClient(app)

    with store.transaction("p1"):
        row = dict(store["p1"])
        row["legacy_bonus"] = float("inf")
        store["p1"] = row
        store.persist("p1", row)

    response = client.post("/monthly-event/p1/claim-daily")
    assert response.status_code == 200, (
        "an unreadable legacy_bonus took the whole claim down with it: %d"
        % response.status_code)
    stored = float(store["p1"]["legacy_bonus"])
    assert stored == me.get_daily_reward(theme, 1)["legacy"], (
        "the stored legacy_bonus is %r; the unreadable term should have been "
        "dropped and the reward kept" % stored)
    assert float(store["p1"]["progress"]) > 0.0, "the rest of the payout was lost"


def test_a_non_finite_stored_shop_balance_is_repaired_by_a_later_grant(
        monkeypatch):
    """``nan`` and a string are repaired. MEASURED 2026-10-05: a grant of 10
    over either leaves exactly 10, because the reader floors an unreadable
    balance at zero and the till compares against an int.

    ``inf`` is NOT repaired and is the live defect in the next test.
    """
    currency = "North Star Dust"
    for junk in (float("nan"), "North Star Dust", None, [1, 2]):
        player = _player("p1")
        player["monthly_event"] = {currency: junk}
        me.grant_event_currency(player, CelestialTheme.NORTH_MYSTIC, 10)
        held = player["monthly_event"][currency]
        assert held == 10, (
            "a stored balance of %r produced %r after a grant of 10"
            % (junk, held))


@pytest.mark.xfail(strict=True, reason=(
    "LIVE DEFECT, measured 2026-10-05: grant_event_currency reads the stored "
    "balance with int(...) inside a try that catches only (TypeError, "
    "ValueError). int(float('inf')) raises OverflowError, which is neither, so "
    "a row saved with an infinite shop balance escapes the guard and 500s the "
    "claim route. int(float('nan')) raises ValueError and IS caught, which is "
    "why this survived a nan-only test. Sibling fix: the module's own _finite() "
    "helper already catches OverflowError, so the repair is to read the stored "
    "balance through the same kind of guard. xfail(strict) so a fix turns "
    "this into a reported failure instead of a silent pass."))
def test_an_infinite_stored_shop_balance_is_repaired_rather_than_500ing(
        monkeypatch, app_with_players):
    """The contract this SHOULD have: an unreadable saved balance costs the
    player this term of a sum, not the whole claim.

    Currently an ``inf`` under the currency key raises ``OverflowError`` out of
    ``grant_event_currency``, and because the faucet is called from inside the
    claim handler the player sees a 500 with no reward at all.

    Three arms, all measured:
      * the helper raises OverflowError on ``inf``;
      * ``int(float('nan'))`` raises ValueError and is caught, which is why the
        sibling test above passes and this one does not;
      * over HTTP a row holding ``inf`` answers 500 while ``nan`` answers 200.

    The CONTROL arm is what proves this is the stored-balance read and not the
    amount: the identical call with a plain ``inf`` AMOUNT is refused cleanly
    and mints nothing, and the 500 disappears when the stored junk is removed.
    """
    currency = me.THEME_CONFIG[CelestialTheme.NORTH_MYSTIC]["shop_currency"]

    # 1. The helper itself.
    player = _player("p1")
    player["monthly_event"] = {currency: float("inf")}
    me.grant_event_currency(player, CelestialTheme.NORTH_MYSTIC, 10)
    assert player["monthly_event"][currency] == 10, (
        "an infinite stored balance was not repaired")

    # 2. The exception class that escapes the guard -- the reason for the xfail.
    with pytest.raises(OverflowError):
        int(float("inf"))

    # 3. End to end over HTTP.
    theme = CelestialTheme.NORTH_MYSTIC
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = _seeded_app("p1")
    with store.transaction("p1"):
        row = dict(store["p1"])
        row["monthly_event"] = {currency: float("inf")}
        store["p1"] = row
        store.persist("p1", row)
    broken = TestClient(app, raise_server_exceptions=False)
    assert broken.post("/monthly-event/p1/claim-daily").status_code == 200, (
        "a row with an infinite shop balance answered a server error; an "
        "unreadable saved number must cost the player one term, not the claim")

    # CONTROL: an inf AMOUNT is refused cleanly (no stored junk involved), which
    # is why the 500 above is the stored-balance read and not the amount guard.
    clean = _player("p1")
    me.grant_event_currency(clean, CelestialTheme.NORTH_MYSTIC, float("inf"))
    assert not clean.get("monthly_event"), (
        "an infinite AMOUNT was written; the amount guard is separate")


def test_a_non_finite_amount_cannot_be_smuggled_through_the_config_table(
        monkeypatch, app_with_players):
    """The amount the faucet uses comes from ``EVENT_CURRENCY_REWARD``. A
    non-finite row there must mint nothing, not ``inf`` units.

    MEASURED 2026-10-05: with the day-1 amount patched to ``inf``, the claim
    still answers 200 (the payout itself is fine) and the shop balance is
    absent -- the guard refused the amount instead of writing it.

    CONTROL: restoring the real amount pays it, so the 200 above is a claim
    that paid its reward and refused only the corrupt currency row.
    """
    theme = CelestialTheme.NORTH_MYSTIC
    currency = me.THEME_CONFIG[theme]["shop_currency"]
    saved = dict(me.EVENT_CURRENCY_REWARD)
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    try:
        me.EVENT_CURRENCY_REWARD[1] = float("inf")
        app, store = _seeded_app("p1")
        response = TestClient(app).post("/monthly-event/p1/claim-daily")
        assert response.status_code == 200, (
            "a corrupt currency row took the whole claim down: %d"
            % response.status_code)
        assert _held_currency(store, "p1", currency) in (0, None), (
            "an infinite currency amount was minted: %r"
            % _held_currency(store, "p1", currency))
        assert float(store["p1"]["progress"]) > 0.0, (
            "the rest of the payout was lost to a bad currency row")
    finally:
        me.EVENT_CURRENCY_REWARD.clear()
        me.EVENT_CURRENCY_REWARD.update(saved)

    app, store = _seeded_app("p1")
    assert TestClient(app).post("/monthly-event/p1/claim-daily").status_code == 200
    assert _held_currency(store, "p1", currency) == me.EVENT_CURRENCY_REWARD[1], (
        "restoring the table did not restore the payout; the control above is "
        "measuring a broken fixture")


def test_a_refused_daily_claim_leaves_every_number_on_the_row_finite(
        monkeypatch, app_with_players):
    """Nothing the claim writes may be non-finite, whatever the row held before.

    Walks the stored row after a REFUSED claim rather than a paid one, so the
    assertion covers the shape of everything under the two fields the claim
    touches (``progress`` and ``legacy_bonus``) plus anything already sitting
    under ``monthly_event``.
    """
    clock = _PinnedClock(_inactive_day(EVENT_DURATION_DAYS))
    monkeypatch.setattr(me.time, "time", clock)
    app, store = _seeded_app("p1")
    client = TestClient(app)
    assert client.post("/monthly-event/p1/claim-daily").status_code == 400

    row = store["p1"]
    for field in ("progress", "legacy_bonus"):
        value = row.get(field, 0.0)
        assert isinstance(value, (int, float)) and value == value \
            and value not in (float("inf"), float("-inf")), (
        "a refused claim left a non-finite %s on the row: %r" % (field, value))
    for key, value in row.get("monthly_event", {}).items():
        if isinstance(value, (int, float)):
            assert value == value and value not in (float("inf"), float("-inf")), \
                "a refused claim left a non-finite %s: %r" % (key, value)

# --------------------------------------------------------------------------
# 5. THE FAUCET IS WIRED AT HTTP LEVEL, AND THE WHOLE WINDOW IS THE PROOF
#
# Block I above proves ONE claim funds the till, and section 1 proves the route
# is closed to strangers. Neither walks the window, so neither can tell a
# correct per-day table from one that pays day 1 and forgets the rest: a faucet
# keyed only on day 1 passes every test in this file so far while handing over
# 60 of the 340 a full window is supposed to pay.
#
# The assertion is on the STORED TOTAL, read before and after each claim. The
# reply is useless here -- `claim-daily` answers 200 and names a reward whether
# or not the currency ever lands, so a reply-shaped assertion stays green on the
# broken file. `PlayerStore` is COPY-ON-READ, so every number below is read back
# out of the store after the handler has returned, never off a dict the handler
# happened to be holding.
# --------------------------------------------------------------------------

def test_every_paying_day_raises_the_stored_currency_total_by_its_own_amount(
        monkeypatch):
    """THE missing regression: the total RISES, four times, by four amounts.

    MEASURED 2026-10-05 against the shipped `EVENT_CURRENCY_REWARD`: claiming
    event days 1, 3, 7 and 14 moves the stored balance 0 -> 60 -> 130 -> 230 ->
    340, each step up by that day's own row. Day 1 -> 60 is the step the older
    tests already cover; the three after it are the ones nothing covered.

    Walked over HTTP on a bare mount with the clock advanced a day at a time, so
    every step is a real request through the real handler. The claimed-days
    marker forces that walk -- claiming the same day twice is a 409 -- so the
    loop cannot quietly re-measure day 1 four times and call it a pass.
    """
    theme = CelestialTheme.NORTH_MYSTIC
    app, store = _seeded_app("p1")
    client = TestClient(app)
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    base = clock.epoch
    currency = me.THEME_CONFIG[theme]["shop_currency"]

    running = 0
    for day in sorted(me.EVENT_CURRENCY_REWARD):
        clock.set(base + (day - 1) * me.DAY)  # day-of-month 1 is offset 0
        before = _numeric_event_total(store, "p1")
        step = me.EVENT_CURRENCY_REWARD[day]

        response = client.post("/monthly-event/p1/claim-daily")
        assert response.status_code == 200, (
            "event day %d did not pay at all, so this walk would be measuring "
            "a broken claim path rather than the faucet" % day)
        assert response.json()["day"] == day, (
            "the clock is on event day %d but the route answered day %r"
            % (day, response.json()["day"]))

        running += step
        after = _numeric_event_total(store, "p1")
        assert after == running, (
            "claiming event day %d moved the stored total from %d to %d; this "
            "day pays %r, so expected a rise of %d to %d. The faucet is either "
            "skipping a paying day or paying the wrong amount."
            % (day, before, after, step, step, running))
        assert _held_currency(store, "p1", currency) == running, (
            "the rise landed somewhere other than the %s the till debits: %r"
            % (currency, store["p1"].get("monthly_event")))

    # The walk has to have covered every paying day, or the total it reached is
    # a prefix of the window and proves nothing about the rest.
    assert running == sum(me.EVENT_CURRENCY_REWARD.values()), (
        "the walk did not cover every paying day, so this test measured a "
        "prefix of the window")


@pytest.mark.parametrize("theme", list(CelestialTheme),
                         ids=[t.value for t in CelestialTheme])
def test_the_faucet_walks_a_real_window_over_the_mounted_app(theme, monkeypatch):
    """THE SAME WALK, ON THE SHIPPED APP, WITH A REAL SESSION.

    The walk above uses a bare mount, which cannot show the route is reachable
    in the game a player actually plays: the module carries no auth of its own,
    so on a bare app every claim is a 200 whether or not anything guards it.
    This arm says the faucet is reachable END TO END -- real app, real player
    created through the real create route, real session, real store.

    MEASURED 2026-10-05: the total rises on every paying day and lands on the
    table's sum, on both themes.
    """
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    base = clock.epoch
    currency = me.THEME_CONFIG[theme]["shop_currency"]

    with TestClient(main_module.app) as client:
        (pid,) = _real_players(client, "window_walk_%s" % theme.value)
        headers = _authed(pid)

        running = 0
        for day in sorted(me.EVENT_CURRENCY_REWARD):
            clock.set(base + (day - 1) * me.DAY)
            before = _numeric_event_total(main_module.players_db, pid)
            response = client.post("/monthly-event/%s/claim-daily" % pid,
                                   headers=headers)
            assert response.status_code == 200, (
                "event day %d answered %d (%s) on the mounted app"
                % (day, response.status_code, response.text[:160]))
            step = me.EVENT_CURRENCY_REWARD[day]
            running += step
            after = _numeric_event_total(main_module.players_db, pid)
            assert after - before == step, (
                "event day %d moved the mounted store's total from %d to %d -- a "
                "rise of %d rather than the %r this day pays"
                % (day, before, after, after - before, step))
            assert _held_currency(main_module.players_db, pid, currency) == running

        assert running == sum(me.EVENT_CURRENCY_REWARD.values())


def test_a_day_the_faucet_does_not_pay_leaves_the_total_where_it_was(monkeypatch):
    """CONTROL for the walk above: not every day pays, and a day paying no
    currency must move the total by NOTHING.

    The day picked here carries no reward row at all, so the route refuses it
    404. This is the shape where a faucet is wired but a claim still quietly
    moves money it should not: pinned on the stored total, because the status
    code alone would not say which.
    """
    theme = CelestialTheme.NORTH_MYSTIC
    app, store = _seeded_app("p1")
    client = TestClient(app)
    clock = _PinnedClock(_active_start_for(theme, 0))
    monkeypatch.setattr(me.time, "time", clock)
    base = clock.epoch
    currency = me.THEME_CONFIG[theme]["shop_currency"]

    quiet = next(day for day in (2, 4, 5, 6)
                 if day not in me.EVENT_CURRENCY_REWARD
                 and day <= me.EVENT_DURATION_DAYS)

    # Day 1 first, so the quiet day is measured against a NON-EMPTY total rather
    # than against zero. A zero-to-zero comparison passes on a faucet that never
    # pays anything at all.
    clock.set(base)
    assert client.post("/monthly-event/p1/claim-daily").status_code == 200
    before = _numeric_event_total(store, "p1")
    assert before > 0, "the day-1 seed claim paid nothing; this measures nothing"

    clock.set(base + (quiet - 1) * me.DAY)
    response = client.post("/monthly-event/p1/claim-daily")
    assert response.status_code == 404, (
        "event day %d answered %d, but it carries no reward row, so this "
        "fixture is measuring something other than a quiet day"
        % (quiet, response.status_code))
    assert _numeric_event_total(store, "p1") == before, (
        "a refused claim moved the currency total from %d to %d"
        % (before, _numeric_event_total(store, "p1")))
    assert _held_currency(store, "p1", currency) == before


# --------------------------------------------------------------------------
# 6. THE COSMETICS CATALOGUE PRICES THE EVENT AURA ON A BALANCE THE FAUCET
#    NEVER FUNDS -- MEASURED, NOT INFERRED
#
# `THEME_CONFIG[theme]["shop_currency"]` is the DISPLAY name "North Star Dust" /
# "Moon Essence", and that is what `grant_event_currency` writes and what
# `buy-shop` debits. The cosmetics catalogue spells the same two currencies
# SNAKE_CASE (`north_star_dust` / `moon_essence`) and `cosmetics._row_balance`
# reads THOSE. So the catalogue's two 500-cost auras are priced in a balance no
# code path in the game ever creates.
#
# MEASURED 2026-10-05 by CALLING the catalogue, not by reading it: with 5000
# under `north_star_dust` the purchase succeeds; with 5000 under the display
# name "North Star Dust" -- the key the faucet actually writes -- the SAME
# purchase is refused.
#
# This is a real defect and it is NOT fixed here: `cosmetics.py` belongs to
# another owner, and both in-tree repairs are worse than the bug. Renaming the
# key the faucet writes breaks the shop's own till; writing BOTH spellings from
# one payout mints two balances per claim and opens the 500 aura to a double
# purchase. So it is pinned as a measured FACT, not as a wish.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("theme,cosmetic_id,snake,display", [
    (CelestialTheme.NORTH_MYSTIC, "aura_north_mystic", "north_star_dust",
     "North Star Dust"),
    (CelestialTheme.MOON_PALACE, "aura_moon_palace", "moon_essence",
     "Moon Essence"),
])
def test_the_catalogue_aura_is_priced_in_a_key_the_faucet_never_writes(
        theme, cosmetic_id, snake, display, monkeypatch):
    """MEASURED 2026-10-05, both themes. Recorded as a fact about the tree.

    Four reads, and the last is the one that matters:

      1. the FAUCET writes the display name, read from the module's own config
         so a rename in either direction shows up here;
      2. the CATALOGUE charges the snake_case name;
      3. this window's event is LIVE, so the till's own gate is not what
         refuses;
      4. money under the display name CANNOT buy the catalogue aura, while money
         under the snake_case name can.

    Arm 3 exists because of what it caught. The first version of this test left
    the clock on the wall clock, and the MOON_PALACE arm came back refusing for
    the wrong reason: `purchase_cosmetic` gates every event cosmetic on its own
    window being live (`cosmetics.event_is_running`), and out-of-window it
    refuses before it ever looks at a balance. A "the purchase was refused" arm
    with no live-window control measures the calendar, not the currency.

    Arm 4 is the control that keeps the finding honest. Without it the test only
    proves two spellings exist; with it, the spellings sit on OPPOSITE SIDES OF
    A REAL PURCHASE -- which is what makes the faucet a dead end for the
    headline cosmetic.

    `cosmetics` imports its own `time`, so the clock is pinned in BOTH modules.
    Pinning only `monthly_event` would leave the till reading the wall clock.
    """
    assert me.THEME_CONFIG[theme]["shop_currency"] == display, (
        "the faucet now writes %r, not the display name this test measured; "
        "re-measure before trusting the arms below"
        % me.THEME_CONFIG[theme]["shop_currency"])

    entry = cosmetics.get_cosmetic(cosmetic_id)
    assert entry is not None, "the catalogue does not carry %s" % cosmetic_id
    assert entry["purchase_currency"] == snake, (
        "the catalogue now charges %r for %s, not the snake_case name this test "
        "measured; re-measure before trusting the arms below"
        % (entry["purchase_currency"], cosmetic_id))
    assert entry.get("event_source") == theme.value, (
        "%s is no longer an event cosmetic of the %s window, so the till's "
        "live-window gate does not apply to it and arm 3 is measuring the wrong "
        "thing" % (cosmetic_id, theme.value))

    # Arm 3: the window is live, for this theme, in the module the till reads.
    epoch = _active_start_for(theme, 0)
    monkeypatch.setattr(me.time, "time", _PinnedClock(epoch))
    monkeypatch.setattr(cosmetics.time, "time", _PinnedClock(epoch))
    assert cosmetics.event_is_running(theme.value), (
        "the %s window is not live at the epoch this test pinned, so the "
        "purchase below is refused by the calendar rather than by the balance"
        % theme.value)

    # Arm 3, both directions, on throwaway dicts -- no store, so neither arm can
    # pass by reading a balance some earlier test left behind.
    funded_display = _player("display_funded")
    funded_display["monthly_event"] = {display: 5000}
    funded_snake = _player("snake_funded")
    funded_snake["monthly_event"] = {snake: 5000}

    bought_with_snake = cosmetics.purchase_cosmetic(funded_snake, cosmetic_id)
    bought_with_display = cosmetics.purchase_cosmetic(funded_display, cosmetic_id)

    assert bought_with_snake, (
        "the catalogue refused %s with 5000 under its own %r key, so the "
        "two-spelling mismatch is not why it is unreachable and this test is "
        "measuring something else" % (cosmetic_id, snake))
    assert not bought_with_display, (
        "5000 of %s -- the exact key the faucet writes -- now buys %s from the "
        "catalogue. The mismatch is FIXED, so this measured fact has gone stale "
        "and the 'deliberately NOT fixed here' note in monthly_event.py and "
        "grant_event_currency's docstring both need updating."
        % (display, cosmetic_id))

