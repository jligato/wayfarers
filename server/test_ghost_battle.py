"""Ghost-battle unit and route tests.

These are in-process tests over FastAPI's TestClient with the in-memory
PlayerStore. PostgreSQL durability is covered separately by
``check_ghost_battle_persistence.py`` and the integration runner.

The stat line is the whole contract
-------------------------------
``POST /pvp/match`` does NOT fight the ``enemy_stats`` from the request body. It
reads the opponent's own saved row (``main._pvp_enemy_stat_line``) and ignores
the body's line, so a caller cannot hand in a 1-HP opponent. That hardening
landed in ``main.py`` AFTER this file was written.

``POST /ghost/record`` re-runs that same battle and refuses with 409 unless the
re-derived turn log equals the one in ``pvp_history`` -- so the line it
re-simulates must be the DERIVED one. While the two sides disagreed (the route
re-simulated the caller's ``ENEMY``, the match had fought the rival's stored
row) the logs could never match and 26 tests here died with
``KeyError: 'ghost_id'`` on a ``{"detail": ...}`` body.

So ``ENEMY`` below is only the line carried on the wire; the line that decides a
capture is ``_opponent_line()``, which asks the SERVER's own normaliser what it
fought. That delegation is deliberate -- a second hand-written copy of the rule
is exactly what drifted before.

Authentication
--------------
Every ``/ghost/*`` and ``/pvp/match`` call below now presents a session minted
for the specific player it acts as, through ``server/test_auth_helpers.py``.
That is per-call on purpose: a blanket autouse session would make this file
structurally unable to observe the unauthenticated case, which is what the
security tests exist to check.

``_bearer`` is kept as the local spelling but now delegates to that shared
helper, so there is exactly one place that knows how a session is minted.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

from fastapi.testclient import TestClient  # noqa: E402

from server import auth  # noqa: E402
from server.test_auth_helpers import auth_headers  # noqa: E402
from server.main import app, players_db  # noqa: E402
from server.ghost_battle import (  # noqa: E402
    GHOST_FIGHTS_PER_PLAYER,
    GHOST_RECEIPTS_PER_PLAYER,
    GHOSTS_PER_PLAYER,
    GHOST_LOSS_REWARD_STONES,
    GHOST_WIN_REWARD_STONES,
    freeze_stats,
    ghost_seed,
    source_match_seed,
)

# Registered here because main.py is owned by another agent; the production wiring
# snippet is documented in docs/top-priorities.md.
from server.ghost_battle import register_ghost_battle_routes  # noqa: E402

if not any(getattr(r, "path", "") == "/ghost/record" for r in app.routes):
    register_ghost_battle_routes(app, lambda: players_db)

client = TestClient(app)

# The line carried on the wire to /pvp/match. The server parses it and then
# ignores it -- it fights the opponent's stored row instead. Kept explicit so
# no reader mistakes it for the line that decides a capture.
ENEMY = {"realm": "mortal", "stage": "early", "element": "water",
         "hp": 800, "max_hp": 800, "atk": 50, "def": 25}


def _bearer(player_id):
    """Authorization header carrying a live session for ``player_id``.

    Delegates to the shared helper so there is one place that knows how a
    session is minted. ``signing_key`` is retained in the helper's signature
    for call-site compatibility and is deliberately unused: this game has no
    player passwords.
    """
    return auth_headers(player_id, "")


def _opponent_line(opponent_id):
    """The stat line /pvp/match ACTUALLY fought, read back from the server.

    Delegated to ``main._pvp_enemy_stat_line`` -- the same helper the route
    calls -- rather than spelled out here. An earlier version hand-wrote the
    eight keys and sent them to both calls; that copy drifted from the server's
    rule and every capture 409'd. Taking no arguments made the drift invisible,
    so this one REQUIRES the opponent's id and cannot be filled in with a guess.
    """
    import server.main as main_module

    return dict(main_module._pvp_enemy_stat_line(
        type("Req", (), {"opponent_id": opponent_id})()))


def _new_player(name, root="fire"):
    res = client.post("/player/create", json={"name": name, "spirit_root": root})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _play_one_match(player_id, opponent_id, opponent_rating=1000, enemy=None):
    """Play a real match against ``opponent_id`` and return the response.

    ``/pvp/match`` simulates with ``enemy`` (defaulting to ``ENEMY``) and stores
    the resulting turn log in the player's ``pvp_history``. That same line is
    what a later ``/ghost/record`` must echo back, which is why the default here
    and in ``_opponent_line`` are the same constant.
    """
    return client.post("/pvp/match", json={
        "player_id": player_id, "opponent_id": opponent_id,
        "opponent_rating": opponent_rating, "enemy_stats": enemy or dict(ENEMY),
    }, headers=_bearer(player_id))


# === Seed determinism ===


def test_ghost_seed_is_stable_across_calls():
    assert ghost_seed("p1", "k") == ghost_seed("p1", "k")
    assert ghost_seed("p1", "k") != ghost_seed("p2", "k")


def test_freeze_stats_snapshots_the_combat_line():
    player = {"cultivation_realm": "core", "cultivation_stage": "late",
              "spirit_root": "wind", "atk": 77, "def_": 33, "hp": 900, "mp": 100, "spd": 120}
    frozen = freeze_stats(player)
    assert frozen["realm"] == "core" and frozen["stage"] == "late" and frozen["element"] == "wind"
    assert frozen["def"] == 33
    # Mutating the live player must not change the frozen ghost copy.
    player["atk"] = 9999
    assert frozen["atk"] == 77


# === Recording ===


def test_record_requires_a_finished_battle():
    owner = _new_player("GhostNoBattle")
    res = client.post("/ghost/record", json={"player_id": owner, "opponent_id": "nobody",
                                             "opponent_rating": 1000,
                                             "opponent_stats": ENEMY}, headers=_bearer(owner))
    assert res.status_code == 404


def test_record_captures_a_finished_battle():
    owner = _new_player("GhostOwner")
    rival = _new_player("GhostRival", "water")
    _play_one_match(owner, rival)
    res = client.post("/ghost/record", json={"player_id": owner, "opponent_id": rival,
                                             "opponent_rating": 1000,
                                             "opponent_stats": _opponent_line(rival)}, headers=_bearer(owner))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["replayed"] is False
    assert body["ghost_id"].startswith("ghost_")
    assert body["source_winner"] in ("player", "enemy")


def test_record_rejects_a_forged_stat_line():
    """A client cannot invent a ghost for a fight that never happened."""
    owner = _new_player("GhostForger")
    rival = _new_player("GhostHonest", "water")
    _play_one_match(owner, rival)
    forged = dict(ENEMY)
    forged["hp"] = 999999  # enemy that would obviously not match the stored log
    res = client.post("/ghost/record", json={"player_id": owner, "opponent_id": rival,
                                             "opponent_rating": 1000, "opponent_stats": forged}, headers=_bearer(owner))
    assert res.status_code == 409, res.text


def test_rating_is_not_part_of_the_capture_identity():
    """A rating the match did not use is accepted, and yields a separate ghost.

    Measured against the live route: ``source_match_seed`` hashes only
    ``"pvp:{player_id}:{opponent_id}"``, so the rating never reaches the
    simulation and never decides whether the capture verifies.

    The rating is now read from the OPPONENT'S OWN SAVED ROW
    (``server_opponent_rating``), so it is not part of the capture identity
    either -- which is what this test's name has always claimed. It used to be
    pasted straight from the request body into the dedupe key
    (``log_key = "{opponent_id}:{rating}:{new_rating}"``), and because the
    per-day payout rule is keyed on ghost id, a caller could replay one real
    battle under N claimed ratings to mint N pay-paying ghosts: measured 210
    stones from a single challenger on a single match. The rating is now
    server-owned, matching ``main._pvp_rating_for``, which closed the same
    caller-claimed-rating hole on ``/pvp/match``.

    This test was previously named ``..._rejects_a_forged_rating`` while
    asserting a 200. The rating is still not a rejection input; the stat line
    is (``test_record_rejects_a_forged_stat_line``).
    """
    owner = _new_player("GhostRatingForger")
    rival = _new_player("GhostRatingRival", "water")
    _play_one_match(owner, rival, opponent_rating=1000)
    honest = client.post("/ghost/record", json={"player_id": owner, "opponent_id": rival,
                                               "opponent_rating": 1000,
                                               "opponent_stats": _opponent_line(rival)}, headers=_bearer(owner))
    assert honest.status_code == 200, honest.text

    # Same battle, different claimed rating: still a valid capture (the rating
    # is not a rejection input), but the SAME ghost. The rating is read from the
    # opponent's row, so a claimed one cannot mint extra pay-paying ghosts.
    other = client.post("/ghost/record", json={"player_id": owner, "opponent_id": rival,
                                              "opponent_rating": 1234,
                                              "opponent_stats": _opponent_line(rival)}, headers=_bearer(owner))
    assert other.status_code == 200, other.text
    assert other.json()["ghost_id"] == honest.json()["ghost_id"], (
        "a caller-chosen opponent_rating changed the capture identity, so one "
        "real battle mints a fresh pay-paying ghost per claim: %s vs %s"
        % (other.json()["ghost_id"], honest.json()["ghost_id"]))
    assert other.json()["replayed"] is True, other.json()


def test_record_is_idempotent_for_the_same_battle():
    owner = _new_player("GhostIdem")
    rival = _new_player("GhostIdemRival", "water")
    _play_one_match(owner, rival)
    first = client.post("/ghost/record", json={"player_id": owner, "opponent_id": rival,
                                               "opponent_rating": 1000,
                                             "opponent_stats": _opponent_line(rival)}, headers=_bearer(owner))
    second = client.post("/ghost/record", json={"player_id": owner, "opponent_id": rival,
                                                "opponent_rating": 1000,
                                             "opponent_stats": _opponent_line(rival)}, headers=_bearer(owner))
    assert first.json()["ghost_id"] == second.json()["ghost_id"]
    assert second.json()["replayed"] is True
    assert second.json()["ghosts"] == 1


# === Listing ===


def test_list_excludes_the_viewers_own_ghosts():
    owner = _new_player("GhostLister")
    rival = _new_player("GhostListerRival", "water")
    _play_one_match(owner, rival)
    client.post("/ghost/record", json={"player_id": owner, "opponent_id": rival,
                                       "opponent_rating": 1000,
                                             "opponent_stats": _opponent_line(rival)}, headers=_bearer(owner))
    res = client.post("/ghost/list", json={"player_id": owner, "limit": 20}, headers=_bearer(owner))
    assert res.status_code == 200
    assert all(g["owner_id"] != owner for g in res.json()["ghosts"])


def test_list_shows_other_players_ghosts():
    owner = _new_player("GhostShown")
    rival = _new_player("GhostShownRival", "water")
    viewer = _new_player("GhostViewer")
    _play_one_match(owner, rival)
    client.post("/ghost/record", json={"player_id": owner, "opponent_id": rival,
                                       "opponent_rating": 1000,
                                             "opponent_stats": _opponent_line(rival)}, headers=_bearer(owner))
    res = client.post("/ghost/list", json={"player_id": viewer, "limit": 50}, headers=_bearer(viewer))
    assert res.status_code == 200
    owners = {g["owner_id"] for g in res.json()["ghosts"]}
    assert owner in owners


# === Fighting ===


def _setup_pair():
    owner = _new_player("GhostFightOwner")
    rival = _new_player("GhostFightRival", "water")
    challenger = _new_player("GhostChallenger", "wind")
    _play_one_match(owner, rival)
    ghost = client.post("/ghost/record", json={"player_id": owner, "opponent_id": rival,
                                               "opponent_rating": 1000,
                                               "opponent_stats": _opponent_line(rival)}, headers=_bearer(owner)).json()["ghost_id"]
    return owner, challenger, ghost


def test_fight_a_ghost_returns_a_full_log():
    owner, challenger, ghost = _setup_pair()
    res = client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost}, headers=_bearer(challenger))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["winner"] in ("player", "enemy")
    assert isinstance(body["log"], list) and body["log"]
    assert body["reward"] in (GHOST_WIN_REWARD_STONES, GHOST_LOSS_REWARD_STONES)


def test_fight_is_deterministic():
    owner, challenger, ghost = _setup_pair()
    first = client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost}, headers=_bearer(challenger)).json()
    second = client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost}, headers=_bearer(challenger)).json()
    assert first["log"] == second["log"]
    assert first["winner"] == second["winner"] and first["turns"] == second["turns"]


def test_cannot_fight_your_own_ghost():
    owner, _challenger, ghost = _setup_pair()
    res = client.post("/ghost/fight", json={"player_id": owner, "ghost_id": ghost}, headers=_bearer(owner))
    assert res.status_code == 400


def test_fighting_an_unknown_ghost_is_404():
    challenger = _new_player("GhostUnknownFighter")
    res = client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": "ghost_deadbeef"}, headers=_bearer(challenger))
    assert res.status_code == 404


# === Idempotency receipts ===


def test_fight_with_idempotency_key_awards_once():
    owner, challenger, ghost = _setup_pair()
    before = _stones(challenger)
    headers = dict(_bearer(challenger), **{"Idempotency-Key": "key-alpha"})
    first = client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost},
                        headers=headers)
    second = client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost},
                         headers=headers)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["log"] == first.json()["log"]
    # Exactly one reward, not two.
    assert _stones(challenger) - before == first.json()["reward"]


def test_reusing_a_key_for_a_different_ghost_is_409():
    owner, challenger, ghost = _setup_pair()
    rival2 = _new_player("GhostOtherRival", "water")
    other_owner = _new_player("GhostOtherOwner")
    _play_one_match(other_owner, rival2)
    other_ghost = client.post("/ghost/record", json={"player_id": other_owner, "opponent_id": rival2,
                                                    "opponent_rating": 1000,
                                                    "opponent_stats": _opponent_line(rival2)}, headers=_bearer(other_owner)).json()["ghost_id"]
    client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost},
                headers=dict(_bearer(challenger),
                            **{"Idempotency-Key": "shared-key"}))
    res = client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": other_ghost},
                      headers=dict(_bearer(challenger),
                                   **{"Idempotency-Key": "shared-key"}))
    assert res.status_code == 409, res.text


def test_receipt_table_is_bounded():
    owner, challenger, ghost = _setup_pair()
    for i in range(GHOST_RECEIPTS_PER_PLAYER + 10):
        client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost},
                    headers=dict(_bearer(challenger),
                                 **{"Idempotency-Key": f"k{i}"}))
    assert len(players_db[challenger].get("ghost_receipts", {})) <= GHOST_RECEIPTS_PER_PLAYER


# === Bounded collections ===


def test_ghost_list_per_player_is_bounded():
    owner = _new_player("GhostBounded")
    rival = _new_player("GhostBoundedRival", "water")
    for i in range(GHOSTS_PER_PLAYER + 5):
        _play_one_match(owner, rival, opponent_rating=1000 + i)
        client.post("/ghost/record", json={"player_id": owner, "opponent_id": rival,
                                           "opponent_rating": 1000 + i,
                                           "opponent_stats": _opponent_line(rival)}, headers=_bearer(owner))
    assert len(players_db[owner].get("ghosts", [])) == GHOSTS_PER_PLAYER


def test_fight_history_is_bounded():
    owner, challenger, ghost = _setup_pair()
    for _ in range(GHOST_FIGHTS_PER_PLAYER + 5):
        client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost}, headers=_bearer(challenger))
    assert len(players_db[challenger].get("ghost_fights", [])) == GHOST_FIGHTS_PER_PLAYER


# === Ladder isolation ===


def test_ghost_fights_do_not_move_the_pvp_ladder():
    owner, challenger, ghost = _setup_pair()
    before = players_db[challenger].get("pvp_rating", 1000)
    client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost}, headers=_bearer(challenger))
    assert players_db[challenger].get("pvp_rating", 1000) == before


def test_ghost_fight_history_route_returns_the_scroll():
    owner, challenger, ghost = _setup_pair()
    client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost}, headers=_bearer(challenger))
    res = client.post("/ghost/fights", json={"player_id": challenger, "limit": 20}, headers=_bearer(challenger))
    assert res.status_code == 200
    assert len(res.json()["fights"]) == 1
    assert res.json()["fights"][0]["ghost_id"] == ghost


# === Preview determinism ===


def test_preview_reproduces_the_source_battle():
    owner, challenger, ghost = _setup_pair()
    first = client.post("/ghost/preview", json={"player_id": challenger, "ghost_id": ghost},
                        headers=_bearer(challenger)).json()
    second = client.post("/ghost/preview", json={"player_id": challenger, "ghost_id": ghost},
                         headers=_bearer(challenger)).json()
    assert first["log"] == second["log"]
    assert first["winner"] == second["winner"]
    assert first["source_winner"] == first["winner"]


# === Preview auth (the measured information leak) ===


def test_preview_refuses_an_anonymous_caller():
    """The route hands back another player's frozen stat line and full log.

    Measured before the fix: this exact request answered 200 with a 35-entry
    turn log. It is now refused before the handler runs.
    """
    owner, challenger, ghost = _setup_pair()
    res = client.post("/ghost/preview", json={"player_id": challenger, "ghost_id": ghost})
    assert res.status_code == 401, res.text
    assert "log" not in res.text


def test_preview_refuses_a_caller_acting_as_someone_else():
    """A real session for a different player is 403, not 200.

    Pairs with the refusal above: a guard that refused EVERY signed-in caller
    would satisfy it, and would also break the feature.
    """
    owner, challenger, ghost = _setup_pair()
    intruder = _new_player("GhostPreviewIntruder")
    res = client.post("/ghost/preview", json={"player_id": challenger, "ghost_id": ghost},
                      headers=_bearer(intruder))
    assert res.status_code == 403, res.text


def test_preview_serves_the_signed_in_viewer_a_real_battle():
    """The companion to the two refusals: the route still works for its owner.

    Asserts the fields the handler actually produces, not just a 200 -- a
    refusal cannot satisfy this.
    """
    owner, challenger, ghost = _setup_pair()
    res = client.post("/ghost/preview", json={"player_id": challenger, "ghost_id": ghost},
                      headers=_bearer(challenger))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["winner"] in ("player", "enemy")
    assert isinstance(body["turns"], int) and body["turns"] > 0
    assert isinstance(body["log"], list) and body["log"]
    # The ghost's real recorded outcome, not a fresh roll.
    stored = next(g for g in players_db[owner]["ghosts"] if g["ghost_id"] == ghost)
    assert body["source_winner"] == stored["source_winner"]


def test_preview_needs_a_player_id_in_the_body():
    """A missing identity is a 422, not a silent anonymous read.

    Without this the route could be called with no player_id at all and the guard
    would have nothing to match -- the exact shape that made the leak possible.
    """
    owner, challenger, ghost = _setup_pair()
    res = client.post("/ghost/preview", json={"ghost_id": ghost}, headers=_bearer(challenger))
    assert res.status_code == 422, res.text


# === Per-day payout: the repeat-fight farm ===


def _fight(challenger, ghost, key=None):
    headers = dict(_bearer(challenger))
    if key:
        headers["Idempotency-Key"] = key
    return client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost},
                      headers=headers)


def test_repeat_fights_pay_once_per_ghost_per_day():
    """The measured farm, closed.

    Before the fix: five calls against one ghost using four DISTINCT
    Idempotency-Keys paid 100 stones (balance 500 -> 600). The key is caller-
    chosen, so the receipt table never saw those as the same fight.

    After: exactly one of them pays. The other four still run the battle and
    still return their full log -- the practice loop is preserved -- but their
    reward is 0 and the response says why.
    """
    owner, challenger, ghost = _setup_pair()
    before = _stones(challenger)
    paid = 0
    for i in range(5):
        res = _fight(challenger, ghost, key=f"distinct-{i}")
        assert res.status_code == 200, res.text
        body = res.json()
        paid += int(body["reward"])
        # The battle itself always runs, so the log is never empty.
        assert body["log"]
    assert paid == GHOST_WIN_REWARD_STONES or paid == GHOST_LOSS_REWARD_STONES, paid
    assert _stones(challenger) - before == paid


def test_a_repeat_fight_says_it_paid_nothing():
    """The client needs to distinguish 'you lost' from 'the day is spent'.

    Without the flag the panel would render 0 stones and read like a bug, or --
    worse -- a client that computes its own reward would show 25 anyway.
    """
    owner, challenger, ghost = _setup_pair()
    first = _fight(challenger, ghost, key="k-first").json()
    second = _fight(challenger, ghost, key="k-second").json()
    assert first["already_paid_today"] is False
    assert first["reward"] > 0
    assert second["already_paid_today"] is True
    assert second["reward"] == 0
    # Same battle, still fully described: this is a practice run, not a refusal.
    assert second["log"] == first["log"]
    assert second["winner"] == first["winner"]


def test_the_daily_allowance_resets_the_next_day():
    """One per day, not one ever.

    Advances the ledger's day by hand rather than waiting 24 hours, then fires a
    real request -- so the reset is proved on the route, not on the helper.

    The hand-edit has to go through ``players_db.persist``. This file edits a
    row straight out of ``players_db[...]`` elsewhere, which works for READING
    and silently does nothing for WRITING: the store is copy-on-read, so that
    expression hands back a detached snapshot and the day never moves. Measured
    on the isolated store: two reads are different objects, an edit through one
    left the stored day unchanged, and the route then correctly refused to
    reset. The route's rollover was never broken -- the fixture was.
    """
    owner, challenger, ghost = _setup_pair()
    first = _fight(challenger, ghost, key="day-1").json()
    assert first["reward"] > 0

    # Move the stored ledger back a day: this is what a real rollover does.
    # PERSISTED, because the read above is a copy-on-read snapshot.
    row = players_db[challenger]
    row["ghost_daily_paid"]["day"] = int(row["ghost_daily_paid"]["day"]) - 1
    players_db.persist(challenger, row)
    assert players_db[challenger]["ghost_daily_paid"]["day"] == int(
        row["ghost_daily_paid"]["day"]), "the day edit did not reach the store"

    second = _fight(challenger, ghost, key="day-2").json()
    assert second["already_paid_today"] is False
    assert second["reward"] > 0


def test_a_replay_of_a_paid_key_still_replays():
    """The receipt must keep working after the daily rule.

    The receipt check runs BEFORE the daily ledger, so a genuine retry of a paid
    key returns the stored result and re-adds the ghost to nothing.
    """
    owner, challenger, ghost = _setup_pair()
    before = _stones(challenger)
    headers = dict(_bearer(challenger), **{"Idempotency-Key": "same-key"})
    first = client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost},
                        headers=headers).json()
    second = client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost},
                         headers=headers).json()
    assert second["replayed"] is True
    assert second["reward"] == first["reward"]
    assert _stones(challenger) - before == first["reward"]


def test_the_daily_ledger_is_bounded():
    """A player cannot grow their own row by fighting every ghost in the world."""
    from server.ghost_battle import GHOST_DAILY_PAID_ENTRIES
    owner, challenger, ghost = _setup_pair()
    paid = players_db[challenger].setdefault("ghost_daily_paid", {"day": -1, "ghosts": []})
    paid["ghosts"] = [f"ghost_bulk_{i}" for i in range(GHOST_DAILY_PAID_ENTRIES + 25)]
    _fight(challenger, ghost, key="bulk-1")
    assert len(players_db[challenger]["ghost_daily_paid"]["ghosts"]) <= GHOST_DAILY_PAID_ENTRIES


def test_concurrent_distinct_key_fights_pay_once():
    """The farm, attempted in parallel rather than in sequence.

    Twenty-four threads, each with its OWN Idempotency-Key, so the receipt table
    treats every one of them as a new fight. Exactly one may pay.

    This deliberately does NOT stub the ledger read to force the race: stubbing
    the read the guard depends on would delete the guard and measure the stub
    instead. The real read runs and the real write runs; the assertion is on the
    balance.
    """
    import threading

    owner, challenger, ghost = _setup_pair()
    before = _stones(challenger)
    results = []
    lock = threading.Lock()
    start = threading.Barrier(12)

    def _one(i):
        start.wait()
        res = _fight(challenger, ghost, key=f"race-{i}")
        with lock:
            results.append(res.status_code)

    threads = [threading.Thread(target=_one, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 12, results
    assert all(code == 200 for code in results), results
    delta = _stones(challenger) - before
    assert delta in (GHOST_WIN_REWARD_STONES, GHOST_LOSS_REWARD_STONES), (
        "12 concurrent distinct-key fights paid %s stones; exactly one payout expected" % delta
    )


# === Defeat counter ===


def test_beating_a_ghost_increments_its_defeated_count():
    owner, challenger, ghost = _setup_pair()
    before = next(g for g in players_db[owner]["ghosts"] if g["ghost_id"] == ghost)["defeated_by"]
    res = client.post("/ghost/fight", json={"player_id": challenger, "ghost_id": ghost}, headers=_bearer(challenger)).json()
    after = next(g for g in players_db[owner]["ghosts"] if g["ghost_id"] == ghost)["defeated_by"]
    if res["winner"] == "player":
        assert after == before + 1
    else:
        assert after == before


def _stones(player_id):
    stones = players_db[player_id].get("spirit_stones", 0)
    if isinstance(stones, dict):
        return int(stones.get("low_grade", 0))
    return int(stones or 0)
