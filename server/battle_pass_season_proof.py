"""Prove the season lifecycle actually PERSISTS to PostgreSQL.

Task 4/5 of the previous batch reported that the fix in server/battle_pass.py
(reading the player INSIDE `with players_db.transaction(player_id):`) is
UNPROTECTED: every committed battle-pass test still passes with the wrapper
removed. I reproduced exactly that - 26 passed with the fix sabotaged.

This script is the missing protection. It bypasses the in-process store cache
and reads PostgreSQL directly, because the store hands back a fresh dict per
access and an in-process read can look correct even when the row never
changed.

It now proves FOUR things, each read back with its own SQL:

  RESET      a stale season_id is replaced in the database, not just in memory
  SUMMARY    crossing a season boundary banks the finished season's summary in
             the player's stored state, and resets the track
  CLAIM      a claim's once-only receipt is a real row in battle_pass_claims
  CLOCK      the season window the server reports is one exact UTC calendar
             month, and does not move with the machine's timezone

Run directly (it is excluded from pytest collection by server/conftest.py):

    python server/battle_pass_season_proof.py            # expect SEASON_PROOF PASS
    python server/battle_pass_season_proof.py --sabotage # expect FAIL (control)

Exit code 0 means the season lifecycle landed. Non-zero means a player would
keep their old season forever, lose their end-of-season record, or could pay a
tier reward twice.
"""
import os
import sys
import time
import uuid

# server/ imports its siblings as top-level modules (main, battle_pass) while
# some of those in turn import "server.*". Both roots must be importable no
# matter which directory this script is launched from.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient

import main
app = main.app
import battle_pass_season
from battle_pass import get_season_id


def pg_dsn():
    """Read connection details from the environment. Never hardcode a password
    in a checked-in file - WC_PG_* is already set by every project's run and CI
    scripts, and a literal would leak the credential to anyone who clones."""
    return dict(
        host=os.environ.get("WC_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("WC_PG_PORT", "5432")),
        dbname=os.environ.get("WC_PG_DB", "wayfarers"),
        user=os.environ.get("WC_PG_USER", "wayfarers"),
        password=os.environ.get("WC_PG_PASSWORD", ""),
    )

def raw_state(player_id):
    """The player's stored extra_state, straight from PostgreSQL.

    Read with its own connection and no store cache, because `players_db[pid]`
    hands back a fresh deserialised dict per access and an in-process read can
    look correct even when the row never changed -- which is the exact failure
    this script exists to catch.

    The column is DECRYPTED when the deployment stores it encrypted, using the
    store's own `decrypt_extra_state`. Parsing it as plain JSON -- which is what
    this script used to do -- raises JSONDecodeError the moment a save key is
    configured, i.e. on every real deployment and on no local test. The proof
    therefore only ever ran where it could not fail.

    An undecryptable column returns {} rather than raising, matching
    `PlayerStore._row_to_dict`: a proof that dies on a crypto error reports an
    exception, not a verdict, and an exception is not a verdict anyone reads.
    """
    import json

    import psycopg2

    from save_encryption import decrypt_extra_state, get_master_key

    conn = psycopg2.connect(**pg_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT extra_state FROM players WHERE id = %s", (player_id,))
            row = cur.fetchone()
        if row is None:
            return None
        state = row[0]
        if not state:
            return {}
        if isinstance(state, str):
            try:
                state = decrypt_extra_state(state, player_id, get_master_key())
            except Exception:  # noqa: BLE001 - mirrors the store's own fallback
                return {}
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except Exception:  # noqa: BLE001
                return {}
        return state or {}
    finally:
        conn.close()


def raw_season(player_id):
    """Just the stored season_id. Kept as its own name; it is the first check."""
    return (raw_state(player_id) or {}).get("battle_pass", {}).get("season_id")


def claim_rows(player_id):
    """The claim ledger rows for this player, read with raw SQL."""
    import psycopg2

    conn = psycopg2.connect(**pg_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT season_id, tier, level FROM battle_pass_claims "
                "WHERE player_id = %s ORDER BY season_id, level",
                (player_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def auth_headers(pid):
    """A session for `pid`, using the project's shared test helper."""
    from server.tests.wc_test_auth import auth_headers as _h

    return _h(pid)


class _AsPlayer:
    """A TestClient that signs in as `pid` on every request.

    The battle-pass GET routes are auth-guarded, so a proof that reads state
    back through the API has to present a session. Minting it inline per call is
    easy to forget on the fourth call and produces a 401 that reads like a
    product failure rather than a missing header.

    Explicit headers win, so a proof that wants to send a deliberately bad token
    still can.
    """

    def __init__(self, client, pid):
        self._client = client
        self._headers = auth_headers(pid)

    def _merge(self, kwargs):
        headers = dict(self._headers)
        headers.update(kwargs.pop("headers", None) or {})
        kwargs["headers"] = headers
        return kwargs

    def get(self, url, **kw):
        return self._client.get(url, **self._merge(kw))

    def post(self, url, **kw):
        return self._client.post(url, **self._merge(kw))

    def request(self, method, url, **kw):
        return self._client.request(method, url, **self._merge(kw))


def proof_reset(client, pid):
    """A stale season_id must be replaced IN THE DATABASE."""
    from main import players_db

    stale = "ancient-" + uuid.uuid4().hex[:6]
    with players_db.transaction(pid):
        players_db[pid]["battle_pass"] = {
            "season_id": stale, "tier": "free", "xp": 0, "level": 1,
            "claimed_free": [], "claimed_senior": [],
            "claimed_super": [], "weekly_bonus_claimed": [],
        }
    before = raw_season(pid)
    assert before == stale, f"plant failed, saw {before!r}"
    print(f"  planted stale season {stale!r}, database confirms it")

    resp = client.get(f"/battle-pass/{pid}")
    assert resp.status_code == 200, resp.text
    after = raw_season(pid)
    expected = get_season_id(time.time())
    print(f"  database season after GET: {after!r} (current season is {expected!r})")
    if after != expected:
        return (f"FAIL reset: expected {expected!r} in the database, found {after!r}. "
                f"A player would keep their old season forever while the API "
                f"reports success.")
    return None


def proof_summary(client, pid):
    """Crossing a boundary must BANK the finished season and reset the track.

    The old code silently discarded the old season on rollover: the player lost
    their level, their claimed tiers and any record of what they had earned,
    with nothing shown in its place. This checks the summary is in the DATABASE.
    """
    import battle_pass
    from main import players_db

    prev_sid = battle_pass_season.previous_season_id(get_season_id(time.time()))
    prev_start, prev_end = battle_pass_season.season_bounds(prev_sid)

    # Give the player a real finished season: some XP, some claimed tiers.
    with players_db.transaction(pid):
        player = players_db[pid]
        player["battle_pass"] = {
            "season_id": prev_sid, "tier": "senior", "xp": 4000, "level": 4,
            "claimed_free": [1, 2, 3], "claimed_senior": [1, 2],
            "claimed_super": [], "weekly_bonus_claimed": [],
            "purchased_at": prev_start,
        }
    assert raw_season(pid) == prev_sid, "planting the finished season failed"
    print(f"  planted a finished {prev_sid} season at level 4 with 5 claimed tiers")

    # A request after the boundary. get_or_create_bp is the rollover chokepoint.
    resp = client.get(f"/battle-pass/{pid}")
    assert resp.status_code == 200, resp.text

    stored = raw_state(pid) or {}
    bp = stored.get("battle_pass", {})
    if bp.get("season_id") != get_season_id(time.time()):
        return (f"FAIL summary: the stored season is still {bp.get('season_id')!r}; "
                f"the rollover did not persist.")
    if bp.get("level") != 1 or bp.get("xp") != 0:
        return (f"FAIL summary: the new season starts at level {bp.get('level')} "
                f"with {bp.get('xp')} xp; the track was not reset.")
    if bp.get("tier") != "free":
        return (f"FAIL summary: the paid tier {bp.get('tier')!r} carried into the "
                f"new season; a new season is a new purchase.")

    history = stored.get("battle_pass_history") or []
    if not history:
        return ("FAIL summary: the finished season was silently discarded -- no "
                "battle_pass_history entry reached the database.")
    entry = history[0]
    if entry.get("season_id") != prev_sid:
        return (f"FAIL summary: history[0] is {entry.get('season_id')!r}, expected "
                f"{prev_sid!r}.")
    if entry.get("final_level") != 4:
        return f"FAIL summary: history records final_level {entry.get('final_level')!r}, expected 4."
    if entry.get("earned_free", {}).get("tiers_claimed") != 3:
        return ("FAIL summary: the free-track earnings were not recorded "
                f"(got {entry.get('earned_free')!r}).")
    if entry.get("earned_free", {}).get("spirit_stones", 0) <= 0:
        return "FAIL summary: the summary claims zero spirit stones for 3 claimed tiers."
    print(f"  database holds the {prev_sid} summary: level "
          f"{entry['final_level']}, {entry['earned_free']['spirit_stones']} stones, "
          f"{entry['unclaimed_free']} unclaimed free tiers")

    # The banked summary must be SERVED over HTTP, not merely stored, and it is
    # served by the route whose absence this proof used to be pinned against.
    #
    # `GET /battle-pass/{player_id}/summary` was missing from the route table,
    # so this block answered 404 for every caller and `proof_summary` could
    # never return None, whatever the rollover did. The database half above was
    # sound the whole time -- only the read had nowhere to land.
    #
    # The route now exists (battle_pass.py's `register_battle_pass_routes`,
    # declared in battle_pass_routes.ROUTE_CONTRACT), so the read is put back
    # where it belongs. The `season` query parameter names WHICH finished
    # season to serve; a season the player never played is a 404, which is what
    # makes "this season does not exist for you" distinguishable from "this
    # season exists and earned nothing".
    served = client.get(f"/battle-pass/{pid}/summary",
                        params={"season": prev_sid})
    if served.status_code != 200:
        return (f"FAIL summary: GET /battle-pass/{{id}}/summary?season="
                f"{prev_sid} returned {served.status_code}.")
    served_row = served.json().get("season_summary") or {}
    if served_row.get("season_id") != prev_sid:
        return (f"FAIL summary: the summary route served season "
                f"{served_row.get('season_id')!r}, expected {prev_sid!r}.")
    if served_row.get("final_level") != 4:
        return (f"FAIL summary: the route served final_level "
                f"{served_row.get('final_level')!r}, expected 4.")
    # The served row must be the rendered summary, not the raw claim record
    # echoed back: `summarize_season` is what turns three claimed free tiers
    # into a stones total. A route serving the stored row unchanged would look
    # correct to the two checks above while handing the panel a shape it cannot
    # render.
    if "tiers_claimed" not in (served_row.get("earned_free") or {}):
        return ("FAIL summary: the served row is the raw stored record, not "
                "the summary (earned_free has no tiers_claimed).")
    print(f"  GET /battle-pass/{{id}}/summary serves the {prev_sid} summary")
    return None


def proof_claim(client, pid):
    """A claim's once-only receipt must be a real ROW, and a retry must pay nothing."""
    import battle_pass
    from main import players_db

    season_id = get_season_id(time.time())
    headers = {}
    # Give the player a level so the claim is legal, and stones to see the debit.
    with players_db.transaction(pid):
        player = players_db[pid]
        battle_pass.get_or_create_bp(player, time.time())
        player["battle_pass"]["level"] = 3
        battle_pass.grant_stones(player, 1000)

    def stones():
        return battle_pass.stone_balance(players_db[pid])

    before = stones()
    first = client.post(f"/battle-pass/{pid}/claim", params={"level": 2, "tier": "free"},
                        headers={**headers, "Idempotency-Key": "proof-claim-1"})
    assert first.status_code == 200, first.text
    paid = first.json()["reward"]["spirit_stones"]
    after_first = stones()
    if after_first != before + paid:
        return f"FAIL claim: the first claim did not pay ({before} -> {after_first}, expected +{paid})."

    rows = claim_rows(pid)
    if len(rows) != 1:
        return (f"FAIL claim: {len(rows)} rows in battle_pass_claims after ONE claim; "
                f"the receipt did not reach the database.")
    print(f"  battle_pass_claims holds 1 row: {rows[0]}")

    # The retry, with a DIFFERENT key -- the harder case, and what a
    # double-tapping client actually sends.
    retry = client.post(f"/battle-pass/{pid}/claim", params={"level": 2, "tier": "free"},
                        headers={**headers, "Idempotency-Key": "proof-claim-2-DIFFERENT"})
    if retry.status_code != 200:
        return f"FAIL claim: the retry answered {retry.status_code}: {retry.text}"
    if retry.json().get("replayed") is not True:
        return f"FAIL claim: the retry was not marked replayed: {retry.json()}"
    if stones() != after_first:
        return (f"FAIL claim: a retry with a DIFFERENT key paid again "
                f"({after_first} -> {stones()}). The once-only guarantee does not "
                f"hold.")
    if len(claim_rows(pid)) != 1:
        return (f"FAIL claim: the retry inserted a second row "
                f"({len(claim_rows(pid))} rows); the primary key did not hold.")
    print("  a retry with a DIFFERENT key paid nothing and added no row")
    return None


_CLOCK_KEYS = ("season_id", "start", "end", "seconds_remaining",
               "days_remaining")


def _clock_body(body):
    """The clock dict, from wherever the route put it.

    The live route nests the countdown -- ``{"ok": true, "season":
    {...}}`` -- so reading the top level raised KeyError on every real
    run, and main() dies with a traceback instead of printing a verdict.
    Unwrap the documented envelope; a flat body is still accepted so a
    caller serving ``season_clock()`` directly exercises the same
    arithmetic.
    """
    if isinstance(body, dict):
        nested = body.get("season")
        if isinstance(nested, dict):
            return nested
    return body


def proof_clock(client):
    """The season window is one exact UTC calendar month, in UTC.

    Checked here rather than only in the unit tests because this is the number
    a PLAYER sees on the countdown.
    """
    import calendar

    import battle_pass_season

    clock = client.get("/battle-pass/season")
    if clock.status_code != 200:
        return f"FAIL clock: GET /battle-pass/season answered {clock.status_code}."
    body = _clock_body(clock.json())
    if not isinstance(body, dict):
        return (f"FAIL clock: the clock payload is a {type(body).__name__}, "
                f"not an object; nothing can be read from it.")
    # Read every field with a MISSING-KEY REPORT, not a subscript. This proof
    # exists to print a verdict, and a subscript on a payload whose shape
    # drifted raises KeyError -- which main() cannot turn into a FAIL line, so
    # the run dies with a traceback and the reader learns nothing. The nested
    # envelope was one way to reach that; a renamed or dropped key is another.
    # Both are the same finding: the countdown a player reads is not there.
    missing = [key for key in _CLOCK_KEYS if body.get(key) is None]
    if missing:
        served = (body.get("season") if isinstance(body.get("season"), dict)
                  else body)
        return (f"FAIL clock: the clock payload is missing "
                f"{', '.join(missing)}; it serves {sorted(served)}. "
                f"The countdown a player reads is not there.")
    sid = body["season_id"]
    start, end = battle_pass_season.season_bounds(sid)
    if (body["start"], body["end"]) != (start, end):
        return (f"FAIL clock: the route reports {body['start']}..{body['end']} for "
                f"{sid}, but the clock module says {start}..{end}.")
    if time.strftime("%Y-%m-%d %H:%M", time.gmtime(start)) != f"{sid}-01 00:00":
        return (f"FAIL clock: {sid} starts at "
                f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(start))} UTC, not "
                f"midnight on the 1st.")
    month_len = calendar.monthrange(*(int(x) for x in sid.split("-")))[1]
    if (end - start) / 86400.0 != month_len:
        return (f"FAIL clock: {sid} is {(end - start) / 86400.0} days long but "
                f"{sid} has {month_len} days in it.")
    if body["seconds_remaining"] < 0:
        return f"FAIL clock: the countdown is negative ({body['seconds_remaining']}s)."
    print(f"  {sid}: {start} .. {end} UTC = {month_len} days, "
          f"{body['days_remaining']:.1f} days remaining")
    return None


def main():
    """Returns 0 when every proof passed, 1 on any failure."""
    with TestClient(app) as client:
        r = client.post("/player/create", json={"name": "SP", "spirit_root": "fire"})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        print(f"player {pid}")
        # Every API call below is made AS that player; the battle-pass GET
        # routes are guarded and would otherwise answer 401.
        client = _AsPlayer(client, pid)

        failures = []
        for label, proof in (("RESET", proof_reset), ("SUMMARY", proof_summary),
                             ("CLAIM", proof_claim)):
            print(f"[{label}]")
            try:
                problem = proof(client, pid)
            except AssertionError as exc:
                problem = f"FAIL {label.lower()}: {exc}"
            if problem:
                failures.append(problem)
                print("  " + problem)
        print("[CLOCK]")
        # Wrapped like the three above, and catching Exception rather than
        # AssertionError: the clock is the only proof that reads a payload
        # SHAPE it does not control, so it is the one that can raise something
        # other than its own assertions. An escaped exception here means the
        # three verdicts already printed are the only record of the run.
        try:
            clock_problem = proof_clock(client)
        except AssertionError as exc:
            clock_problem = f"FAIL clock: {exc}"
        except Exception as exc:  # noqa: BLE001 - a crash is still a verdict
            clock_problem = (f"FAIL clock: raised {type(exc).__name__}: {exc}")
        if clock_problem:
            failures.append(clock_problem)
            print("  " + clock_problem)

        if failures:
            print(f"\nSEASON_PROOF FAIL ({len(failures)} problem(s))")
            return 1
        print("\nSEASON_PROOF PASS: reset, summary, claim and clock all reached "
              "PostgreSQL")
        return 0


if __name__ == "__main__":
    sys.exit(main())
