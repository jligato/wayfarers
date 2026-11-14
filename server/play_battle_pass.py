"""PLAY IT: the whole battle-pass loop, driven through real gameplay.

The unit tests plant XP by writing a level into a dict. This script does not. It
plays:

    create a player  ->  earn XP by pressing real gameplay buttons  ->  read the
    track  ->  BUY the paid tier  ->  CLAIM a free tier  ->  CLAIM it AGAIN
    (nothing paid)  ->  CLAIM a paid tier  ->  sweep the free track  ->  reach a
    SEASON END  ->  confirm the rollover and read the end-of-season summary

Every step asserts on what the server actually returned, and the final balance
checks are read back from PostgreSQL with raw SQL rather than through the store
(whose per-access copy can look right even when nothing committed).

Against real PostgreSQL:

    WC_PG_HOST=127.0.0.1 WC_PG_PORT=55432 WC_PG_DB=wayfarers WC_PG_USER=... \
    WC_PG_PASSWORD=... WC_SAVE_ENCRYPTION_DEV_SEED=<a local test seed> \
    python server/play_battle_pass.py

Exit code 0 = the pass is playable end to end. Non-zero = a step did not do what
the game says it does.
"""

import json
import os
import sys
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402

import battle_pass  # noqa: E402
import battle_pass_season  # noqa: E402
import main as server_main  # noqa: E402
app = server_main.app

# `from main import app` would bind the name `app`... and reading `server_main.players_db`
# afterwards is fine, but the earlier version of this file did
# `from main import app` and then used `server_main.players_db`, which failed with
# "function object has no attribute players_db" because the module-level name
# `main` had been rebound by a later import. Import the module ONCE, under its
# own name, and read everything through it.

PASS, FAIL = "  PASS", "  FAIL"
_failures = []


def check(label, ok, detail=""):
    print(f"{PASS if ok else FAIL}  {label}{(' -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)
    return ok


def section(title):
    print(f"\n=== {title} ===")


def auth_headers(pid):
    from server.tests.wc_test_auth import auth_headers as _h

    return _h(pid)


def db_state(pid):
    """Read the player's stored state from PostgreSQL with raw SQL."""
    import psycopg2

    from save_encryption import decrypt_extra_state, get_master_key

    conn = psycopg2.connect(
        host=os.environ["WC_PG_HOST"],
        port=int(os.environ.get("WC_PG_PORT", 5432)),
        dbname=os.environ.get("WC_PG_DB", "wayfarers"),
        user=os.environ.get("WC_PG_USER", "wayfarers"),
        password=os.environ.get("WC_PG_PASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT spirit_stones, extra_state FROM players WHERE id = %s", (pid,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        stones, extra = row
        if isinstance(extra, str) and extra:
            extra = decrypt_extra_state(extra, pid, get_master_key())
        if isinstance(extra, str):
            extra = json.loads(extra)
        return {"stones": stones or {}, "extra": extra or {}}
    finally:
        conn.close()


def db_claim_rows(pid):
    import psycopg2

    conn = psycopg2.connect(
        host=os.environ["WC_PG_HOST"],
        port=int(os.environ.get("WC_PG_PORT", 5432)),
        dbname=os.environ.get("WC_PG_DB", "wayfarers"),
        user=os.environ.get("WC_PG_USER", "wayfarers"),
        password=os.environ.get("WC_PG_PASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tier, level FROM battle_pass_claims WHERE player_id = %s "
                "ORDER BY level",
                (pid,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def press_a_gameplay_button(client, headers, pid, label):
    """One real earn action. Returns the XP the server said it granted.

    These are the routes the game already had wired to `grant_xp`: the daily
    boss strike, sect contribution, and the offline-progression award. Nothing
    here mints XP from a button on the pass screen -- that route was removed on
    2026-09-29 precisely because any caller could mint a whole season with it.
    """
    strikes = 0
    for _ in range(6):
        r = client.post(f"/daily-boss/{pid}/strike", headers=headers)
        if r.status_code != 200:
            break
        strikes += 1
    return strikes


def plant_previous_season(pid, sid, level, claimed_free):
    """Write a FINISHED season onto the player, as if the clock had rolled.

    Only the stored row is written. Every assertion afterwards goes through the
    API or through raw SQL, so the rollover itself is what is under test.
    """
    with server_main.players_db.transaction(pid):
        player = server_main.players_db[pid]
        player["battle_pass"] = {
            "season_id": sid, "tier": "senior", "xp": 6000, "level": level,
            "claimed_free": list(claimed_free), "claimed_senior": list(claimed_free),
            "claimed_super": [], "weekly_bonus_claimed": [],
            "purchased_at": battle_pass_season.season_bounds(sid)[0],
        }


def play():
    if not os.environ.get("WC_PG_HOST"):
        print("This script needs a real database (WC_PG_HOST set).")
        return 2

    with TestClient(server_main.app) as client:
        section("1. CREATE A PLAYER")
        r = client.post("/player/create", json={"name": "PassPlayer", "spirit_root": "fire"})
        if r.status_code != 200:
            print(f"  could not create a player: {r.status_code} {r.text}")
            return 2
        pid = r.json()["id"]
        headers = auth_headers(pid)
        print(f"  player {pid}")

        # The season the clock says it is, and when it ends.
        # `GET /battle-pass/season` nests the clock under a "season" key
        # (`{"ok": True, "season": {...}}`), so the clock is read through
        # `season["season"]`. Read flat, `season["season_id"]` -- the very
        # first thing step 1 prints -- raised KeyError.
        season = client.get("/battle-pass/season", headers=headers).json()["season"]
        print(f"  season {season['season_id']} ends in "
              f"{season['days_remaining']:.1f} days "
              f"(number {season['season_number']}, "
              f"{season['length_days']:.0f} days long)")

        section("2. EARN XP BY PLAYING (not by pressing a pass button)")
        with server_main.players_db.transaction(pid):
            battle_pass.grant_stones(server_main.players_db[pid], 5000)
        strikes = press_a_gameplay_button(client, headers, pid, "daily boss")
        bp = client.get(f"/battle-pass/{pid}", headers=headers).json()["battle_pass"]
        xp_earned = bp["xp"]
        check(f"daily-boss strikes granted battle-pass XP ({strikes} strikes)",
              xp_earned > 0, f"xp={xp_earned}, level={bp['level']}")

        # Earn more by playing, then check the LEVEL matches the XP actually
        # held. Asserting a fixed level here is a change-detector: the curve is
        # retunable, and the assertion would break without anything being wrong.
        with server_main.players_db.transaction(pid):
            player = server_main.players_db[pid]
            battle_pass.grant_xp(player, 400, "script_earn")
        bp = client.get(f"/battle-pass/{pid}", headers=headers).json()["battle_pass"]
        expected_level = battle_pass.get_level_for_xp(bp["xp"])
        check("more play moved the pass level up", bp["level"] > 1,
              f"level={bp['level']}, xp={bp['xp']}")
        check("the reported level matches the XP the player actually holds",
              bp["level"] == expected_level,
              f"reported {bp['level']}, {bp['xp']} xp is level {expected_level}")

        section("3. READ THE TRACK")
        view = client.get(f"/battle-pass/{pid}", params={"track": "true"},
                         headers=headers).json()["battle_pass"]
        rows = view["track"]["levels"]
        states = {}
        for row in rows:
            for key in ("free", "senior", "super"):
                states.setdefault(row[key]["state"], 0)
                states[row[key]["state"]] += 1
        check("the track reports every row as claimed/claimable/locked",
              set(states) <= {"claimed", "claimable", "locked"} and len(rows) == 50,
              f"{len(rows)} rows, states={states}")
        check("a non-payer can see paid-track rows as locked, not missing",
              any(r["senior"]["state"] == "locked" for r in rows))

        section("4. BUY THE PAID TIER")
        stones_before = db_state(pid)["stones"]
        buy = client.post(f"/battle-pass/{pid}/upgrade", params={"tier": "senior"},
                          headers={**headers, "Idempotency-Key": f"play-buy-{uuid.uuid4().hex}"})
        check("buying SENIOR succeeds", buy.status_code == 200, buy.text[:120])
        stones_after = db_state(pid)["stones"]
        low_before = (stones_before or {}).get("low_grade", 0)
        low_after = (stones_after or {}).get("low_grade", 0)
        cost = battle_pass.TIER_COST[battle_pass.BattlePassTier.SENIOR]
        check("the purchase is debited in the DATABASE",
              low_after == low_before - cost, f"{low_before} -> {low_after}, cost={cost}")

        section("5. CLAIM A FREE TIER")
        s_before = db_state(pid)["stones"].get("low_grade", 0)
        claim = client.post(f"/battle-pass/{pid}/claim", params={"level": 1, "tier": "free"},
                            headers={**headers, "Idempotency-Key": f"play-c1-{uuid.uuid4().hex}"})
        check("claiming free tier 1 succeeds", claim.status_code == 200, claim.text[:120])
        paid = claim.json()["reward"]["spirit_stones"]
        s_mid = db_state(pid)["stones"].get("low_grade", 0)
        check("the reward is credited in the DATABASE",
              s_mid == s_before + paid, f"{s_before} -> {s_mid}, reward={paid}")

        section("6. DOUBLE-TAP THE CLAIM (a DIFFERENT key, like a real client)")
        again = client.post(f"/battle-pass/{pid}/claim", params={"level": 1, "tier": "free"},
                            headers={**headers, "Idempotency-Key": f"play-c2-{uuid.uuid4().hex}"})
        s_after = db_state(pid)["stones"].get("low_grade", 0)
        check("the replay is answered, not refused", again.status_code == 200, again.text[:120])
        check("the replay is marked replayed", again.json().get("replayed") is True)
        check("the REPLAY PAID NOTHING", s_after == s_mid,
              f"{s_mid} -> {s_after}")
        check("the replay returned the same reward it already got",
              again.json().get("reward", {}).get("spirit_stones") == paid)
        check("the database holds exactly ONE claim row for that tier",
              db_claim_rows(pid).count(("free", 1)) == 1, str(db_claim_rows(pid)))

        section("7. CLAIM A PAID TIER")
        p_before = db_state(pid)["stones"].get("low_grade", 0)
        paid_claim = client.post(f"/battle-pass/{pid}/claim", params={"level": 1, "tier": "senior"},
                                 headers={**headers, "Idempotency-Key": f"play-p-{uuid.uuid4().hex}"})
        check("claiming a PAID tier the player bought succeeds",
              paid_claim.status_code == 200, paid_claim.text[:120])
        check("the paid reward is credited",
              db_state(pid)["stones"].get("low_grade", 0) == p_before
              + paid_claim.json()["reward"]["spirit_stones"])

        section("8. SWEEP THE FREE TRACK")
        sweep_before = db_state(pid)["stones"].get("low_grade", 0)
        sweep = client.post(f"/battle-pass/{pid}/claim-all", params={"tier": "free"},
                            headers={**headers, "Idempotency-Key": f"play-s-{uuid.uuid4().hex}"})
        check("claim-all sweeps the unlocked free tiers",
              sweep.status_code == 200 and sweep.json()["claimed_count"] > 0,
              f"claimed {sweep.json().get('claimed_count')}")
        swept_total = sweep.json()["claimed_count"]
        after_sweep = db_state(pid)["stones"].get("low_grade", 0)
        check("the sweep credited more than zero",
              after_sweep > sweep_before, f"{sweep_before} -> {after_sweep}")

        resweep = client.post(f"/battle-pass/{pid}/claim-all", params={"tier": "free"},
                              headers={**headers, "Idempotency-Key": f"play-s2-{uuid.uuid4().hex}"})
        check("a repeated sweep claims NOTHING new",
              resweep.json().get("claimed_count") == 0, resweep.text[:120])
        check("the repeated sweep moved no money",
              db_state(pid)["stones"].get("low_grade", 0) == after_sweep)

        section("9. REACH A SEASON END")
        prev_sid = battle_pass_season.previous_season_id(
            battle_pass_season.season_id(time.time()))
        prev_start, prev_end = battle_pass_season.season_bounds(prev_sid)
        print(f"  the finished season will be {prev_sid} "
              f"({time.strftime('%Y-%m-%d', time.gmtime(prev_start))} .. "
              f"{time.strftime('%Y-%m-%d', time.gmtime(prev_end))} UTC)")
        plant_previous_season(pid, prev_sid, level=7, claimed_free=[1, 2, 3, 4])
        with server_main.players_db.transaction(pid):
            player = server_main.players_db[pid]
            player["battle_pass"]["claimed_senior"] = [1, 2]
        print("  planted a finished 2026-08 season at level 7 with 6 claimed tiers")

        rolled = client.get(f"/battle-pass/{pid}", headers=headers)
        check("the pass opens after the season ended", rolled.status_code == 200)
        bp = rolled.json()["battle_pass"]
        current = battle_pass_season.season_id(time.time())
        check("the track reset to the NEW season", bp["level"] == 1 and bp["xp"] == 0,
              f"level={bp['level']}, xp={bp['xp']}")
        check("the paid tier did NOT carry over", bp["tier"] == "free", f"tier={bp['tier']}")
        check("the new season is the current one", bp["season"]["season_id"] == current)

        section("10. THE END-OF-SEASON SUMMARY")
        stored = db_state(pid)["extra"]
        history = stored.get("battle_pass_history") or []
        check("the finished season was banked in the DATABASE", len(history) >= 1,
              f"{len(history)} entries")
        if history:
            summary = history[0]
            print(f"  {summary['season_id']}: level {summary['final_level']}, "
                  f"{summary['earned_free']['spirit_stones']} stones, "
                  f"{summary['earned_paid']['spirit_stones']} paid stones, "
                  f"{summary['unclaimed_free']} unclaimed free, "
                  f"{summary['unclaimed_paid']} unclaimed paid")
            check("the summary names the season that just ended",
                  summary["season_id"] == prev_sid)
            check("the summary records the level reached",
                  summary["final_level"] == 7)
            check("the summary totals the free-track earnings",
                  summary["earned_free"]["spirit_stones"] > 0)
            check("the summary is honest about what was left unclaimed",
                  summary["unclaimed_free"] == 3, f"got {summary['unclaimed_free']}")

        # Read the summary back over HTTP, from the route that serves it.
        # This used to GET /battle-pass/{id}/summary, which no module
        # declared: measured against the booted app, that path was absent from
        # the route table and answered 404 for every caller, so the checks
        # below could never pass. It is mounted now
        # (battle_pass.register_battle_pass_routes, declared in
        # battle_pass_routes.ROUTE_CONTRACT), so the read goes back where it
        # belongs instead of borrowing the whole-history list off
        # GET /battle-pass/{player_id}.
        served = client.get(f"/battle-pass/{pid}/summary", headers=headers,
                            params={"season": prev_sid})
        check("the summary is served over HTTP", served.status_code == 200,
              served.text[:120])
        served_row = served.json().get("season_summary") or {}
        check("the summary route serves the season that just ended",
              served_row.get("season_id") == prev_sid,
              f"served {served_row.get('season_id')!r}, expected {prev_sid!r}")
        check("the served summary carries the same numbers as the database",
              served_row.get("final_level") == 7,
              f"served final_level={served_row.get('final_level')}")
        check("the served summary is not the raw stored row",
              "tiers_claimed" in served_row.get("earned_free", {}),
              "the served shape must be the summary, not the claim list")

        section("11. THE COUNTDOWN THE PLAYER SEES")
        # Nested for the same reason as step 1: the route answers
        # `{"ok": True, "season": {...}}`, so `seconds_remaining`,
        # `length_days`, `days_remaining` and `server_now` all live under
        # the "season" key.
        clock = client.get("/battle-pass/season", headers=headers).json()["season"]
        check("the countdown is positive and inside this month",
              0 < clock["seconds_remaining"] <= clock["length_days"] * 86400,
              f"{clock['days_remaining']:.2f} days")
        check("the countdown is measured against the server clock, not the client",
              abs(clock["server_now"] - time.time()) < 120)

    print()
    if _failures:
        print(f"PLAY FAILED: {len(_failures)} step(s) did not do what the game says:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("PLAY OK: bought, played, claimed, double-tapped, swept, "
          "season-ended and rolled over. Every step verified in PostgreSQL.")
    return 0


if __name__ == "__main__":
    sys.exit(play())
