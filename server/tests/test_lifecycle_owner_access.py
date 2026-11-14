"""Do the 9 ``/lifecycle/{pid}/*`` routes refuse the LEGITIMATE player, or only strangers?

Why this file exists
--------------------
A suite of refusals proves nothing on its own. Anonymous 401 + stranger 401 +
victim 401 is the classic vacuous result: it only shows that some token was
rejected, and a guard that refuses *everyone* looks identical. The only honest
test carries a **positive control**: the real owner, holding a real session,
must SUCCEED. If the owner is refused too, the routes are broken for everyone
and the refusal tests are measuring an outage, not security.

The working session (measured, not assumed)
-------------------------------------------
``auth.issue_session`` mints a token into a session registry the running app
does not resolve, so a token from there authenticates as
``unknown_or_expired_token`` and every arm reads 401. The path that actually
works is the one the shipped Godot client uses
(``client/scripts/network/APIClient.gd:26`` -> ``LOGIN_PATH := "/auth/login"``):

  1. ``POST /player/create``   -- returns ``signing_key`` (32 bytes, hex)
  2. ``POST /auth/login``      -- ``{"player_id": ..., "signing_key": ...}``
  3. reuse ``token`` as ``Authorization: Bearer <token>``

That three-call path is the fixture below.

Two measurement traps this file had to get past
-----------------------------------------------
1. **Fixture scope.** The repository conftest installs an autouse teardown that
   empties the shared player store after EVERY test. A module-scoped player is
   gone by the second test, and the routes answer 400 "player_id required" --
   which looks like an outage but is a fixture bug.
2. **Lifecycle priming.** ``POST /player/create`` deliberately leaves
   ``lifecycle`` unset; the first lifecycle READ starts the clock
   (``main.py:1266``, "starts on first check-in/gameplay per policy"). Every
   mutating route answers 400 "lifecycle not started" until that read happens.
   The game always does it, so the fixture does it too.

What the sweep found (all three arms measured in one run, fresh player each)
-----------------------------------------------------------------------------
* The legitimate owner is NOT broadly refused. This is not a blanket outage.
* ``POST /lifecycle/{pid}/receipt`` returns **500 to the legitimate owner** --
  a real, route-specific outage. ``TypeError: ReturnReceipt.__init__() got an
  unexpected keyword argument 'id'`` at ``lifecycle.py:681``.
  ``ReturnReceipt.__init__`` takes ``(player, now)`` (``return_receipt.py:16``);
  the route calls it with nine keyword arguments.
* Strangers and anonymous callers are correctly refused 401/403 on 8 of 9
  routes while the owner is alive.

The ninth is a genuine pre-auth information leak, isolated in its own test
below: the custom route class ``LifecycleRoute.handle`` (``lifecycle.py:313``)
runs BEFORE FastAPI resolves dependencies, so its 410 is produced without
anyone proving who they are, and the body is the full ``view(db[pid])``.

Run:  python -m pytest server/tests/test_lifecycle_owner_access.py -v
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from fastapi.testclient import TestClient

from server.main import app

RUN_TAG = uuid.uuid4().hex[:8]
_counter = [0]

# raise_server_exceptions=False so a 500 is REPORTED as a 500 rather than
# re-raised as a traceback. We are measuring status codes, and an unhandled
# TypeError is exactly one of the findings.
client = TestClient(app, raise_server_exceptions=False)

#: The 9 lifecycle routes, as (method, path-suffix-after-{pid}).
LIFECYCLE_ROUTES = (
    ("GET", ""),
    ("POST", "/check-in"),
    ("POST", "/claim-offline"),
    ("GET", "/offline-preview"),
    ("POST", "/advance"),
    ("POST", "/rebirth"),
    ("POST", "/receipts"),
    ("POST", "/receipt"),
    ("POST", "/view_recap"),
)

assert len(LIFECYCLE_ROUTES) == 9, "the sweep must cover all 9 lifecycle routes"

#: Routes measured to break for the legitimate owner (500). Kept as data so a
#: fix is a one-line change here, and the sweep then DEMANDS it be removed --
#: so this file can never quietly become a stale allowlist hiding a live outage.
#:
#: EMPTY as of the P0-3 mint-authority pass. It held ("POST", "/receipt"), whose
#: handler called ``ReturnReceipt(id=..., category=..., ...)`` against a class
#: whose signature is ``__init__(self, player, now)`` -- TypeError, so the route
#: answered 500 to its own legitimate owner on every call while all eight
#: siblings answered 200. lifecycle.py now builds the receipt as the plain dict
#: the stored lifecycle rows already hold, and this set is empty because the
#: measured sweep is owner=200 on all nine.
OWNER_500_ROUTES: set = set()


def _new_label(prefix: str) -> str:
    _counter[0] += 1
    return f"{prefix}-{RUN_TAG}-{_counter[0]}"


def _create_player(label: str) -> tuple[str, str]:
    """Create a real account; return (player_id, signing_key_hex).

    ``POST /player/create`` is the only place a signing key is issued, and it
    returns it in the response body. That is the credential the client stores
    and re-presents at every boot.
    """
    resp = client.post("/player/create", json={"name": label, "spirit_root": "fire"})
    assert resp.status_code == 200, f"player create failed: {resp.status_code} {resp.text[:200]}"
    body = resp.json()
    return body["id"], body["signing_key"]


def _login(player_id: str, signing_key: str) -> str:
    """Exchange a signing key for a session token, the way the client does."""
    resp = client.post("/auth/login", json={"player_id": player_id, "signing_key": signing_key})
    assert resp.status_code == 200, f"login failed: {resp.status_code} {resp.text[:200]}"
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _primed_player(prefix: str) -> dict:
    """A real, logged-in, lifecycle-primed player -- the post-boot game state.

    The priming GET is not optional: ``create_player`` leaves ``lifecycle``
    unset by design, so every mutating route would answer 400 "lifecycle not
    started" to a player the game has created but not yet displayed.
    """
    pid, key = _create_player(_new_label(prefix))
    token = _login(pid, key)
    primed = client.get(f"/lifecycle/{pid}", headers=_auth(token))
    assert primed.status_code == 200, f"priming read failed: {primed.status_code} {primed.text[:200]}"
    return {"player_id": pid, "token": token}


@pytest.fixture
def owner():
    return _primed_player("lc_owner")


@pytest.fixture
def stranger():
    return _primed_player("lc_stranger")


def _call(method: str, pid: str, suffix: str, headers=None):
    return client.request(method, f"/lifecycle/{pid}{suffix}", headers=headers or {})


# ---------------------------------------------------------------------------
# Positive control. If this fails, every refusal below is meaningless.
# ---------------------------------------------------------------------------


def test_owner_session_actually_works(owner):
    """The fixture must prove a WORKING session, not merely a minted token.

    This is the arm a wrong session-minting method fails: a token from the
    wrong registry returns 401 here, and the rest of the file would then be a
    vacuous all-refusal suite.
    """
    resp = _call("GET", owner["player_id"], "", _auth(owner["token"]))
    assert resp.status_code == 200, (
        f"the legitimate owner was refused with {resp.status_code}: {resp.text[:200]}. "
        "Every refusal test in this file is untrustworthy until this passes."
    )
    assert resp.json().get("status") in {"active", "dead", "memorial", "reborn"}


def test_owner_can_read_own_lifecycle(owner):
    resp = _call("GET", owner["player_id"], "", _auth(owner["token"]))
    assert resp.status_code == 200
    assert "born_at" in resp.json() and "dies_at" in resp.json()


def test_stranger_is_refused_own_lifecycle(owner, stranger):
    _call("GET", owner["player_id"], "", _auth(owner["token"]))  # prove owner serves
    resp = _call("GET", owner["player_id"], "", _auth(stranger["token"]))
    assert resp.status_code in (401, 403), (
        f"stranger read another player's lifecycle: {resp.status_code} {resp.text[:200]}"
    )


def test_anonymous_is_refused_own_lifecycle(owner):
    _call("GET", owner["player_id"], "", _auth(owner["token"]))  # prove owner serves
    resp = _call("GET", owner["player_id"], "")
    assert resp.status_code in (401, 403), f"anonymous read a lifecycle: {resp.status_code}"


# ---------------------------------------------------------------------------
# The sweep: a FRESH primed player per route, owner / stranger / anonymous.
# ---------------------------------------------------------------------------


def test_every_lifecycle_route_serves_owner_and_refuses_stranger():
    """All 9 routes, each against its own player, all three arms in one run.

    ORDER WITHIN A ROUTE IS LOAD-BEARING: stranger and anonymous are called
    FIRST, the owner LAST.

    The reason is a measured artifact, not a style choice. ``POST
    /lifecycle/{pid}/advance`` is how a player ends their own life, so when the
    owner is called first the lifecycle is ``dead`` for every subsequent call in
    that iteration -- and the pre-auth 410 (see the leak test below) then
    answers the stranger and the anonymous caller with 410 plus the owner's
    record. Calling the untrusted arms first measures refusal against a LIVE
    lifecycle, which is the honest question: is a stranger refused while the
    owner is playing?

    Both orderings are worth knowing. Owner-first yields a stranger 410 on
    ``/advance``; stranger-first yields 403. The status a stranger gets depends
    on the owner's game state, and that is itself a finding.
    """
    failures = []
    measured = []
    for method, suffix in LIFECYCLE_ROUTES:
        owner = _primed_player("lc_sweep_owner")
        stranger = _primed_player("lc_sweep_stranger")
        pid = owner["player_id"]
        path = f"/lifecycle/{pid}{suffix}"

        # Untrusted arms FIRST, on a live lifecycle. See the docstring.
        stranger_resp = client.request(method, path, headers=_auth(stranger["token"]))
        anon_resp = client.request(method, path)
        owner_resp = client.request(method, path, headers=_auth(owner["token"]))

        label = f"{method} /lifecycle/{{pid}}{suffix or '/'}"
        measured.append(
            f"{label} -> owner={owner_resp.status_code} "
            f"stranger={stranger_resp.status_code} anon={anon_resp.status_code}"
        )

        if (method, suffix) in OWNER_500_ROUTES:
            # Assert the defect is STILL PRESENT, so a fix forces an edit here
            # instead of leaving a stale allowlist that hides a live outage.
            if owner_resp.status_code >= 500:
                failures.append(
                    f"{label}: OWNER OUTAGE {owner_resp.status_code} -- known defect, "
                    "ReturnReceipt.__init__ takes (player, now) but the route passes id="
                )
            else:
                failures.append(
                    f"{label}: expected the known 500, got {owner_resp.status_code}; "
                    "the fix landed, so drop it from OWNER_500_ROUTES"
                )
            continue

        if not (200 <= owner_resp.status_code < 300 or owner_resp.status_code in (409, 410)):
            failures.append(
                f"{label}: OWNER got {owner_resp.status_code} -> {owner_resp.text[:160]}"
            )

        # A 410/409 from these routes is a GAME-RULE answer raised by the route
        # class BEFORE authentication ran, and its body carries the owner's
        # lifecycle. It is not a refusal, so it must not be accepted as one.
        if stranger_resp.status_code not in (401, 403):
            failures.append(
                f"{label}: STRANGER got {stranger_resp.status_code} -> {stranger_resp.text[:160]}"
            )
        if anon_resp.status_code not in (401, 403):
            failures.append(
                f"{label}: ANON got {anon_resp.status_code} -> {anon_resp.text[:160]}"
            )

    assert measured, "the sweep collected no rows -- the route list is wrong"
    print("\nMEASURED:\n  " + "\n  ".join(measured))
    assert not failures, "lifecycle route access is not what we think:\n  " + "\n  ".join(failures)


# ---------------------------------------------------------------------------
# The pre-auth leak, isolated.
# ---------------------------------------------------------------------------


def test_anonymous_cannot_read_a_dead_players_lifecycle_via_410_body():
    """A 410 raised before authentication must not carry the owner's record.

    The route class runs ahead of FastAPI's dependencies, so its 410 is
    produced without anyone proving who they are. The body it returns is the
    full ``view(db[pid])`` -- born_at, dies_at, receipts. Measured: an
    anonymous POST gets 410 with ~1.8 KB including the owner's ``born_at``.
    """
    owner = _primed_player("lc_dead_owner")
    pid = owner["player_id"]

    # The owner legitimately ends their own life -- a route the game offers.
    died = client.post(f"/lifecycle/{pid}/advance", headers=_auth(owner["token"]))
    assert died.status_code == 200, f"owner could not end their lifecycle: {died.status_code}"

    owner_view = client.get(f"/lifecycle/{pid}", headers=_auth(owner["token"])).json()
    born_at = owner_view.get("born_at")
    assert born_at, "owner view carried no born_at to compare against"

    # No Authorization header at all.
    anon = client.post(f"/lifecycle/{pid}/rebirth")

    # The name of this test is the contract: the record must NOT be readable.
    # It used to assert the OPPOSITE -- `assert "born_at" in anon.text` --
    # which made the defect permanent and hid it behind a green suite. That pin
    # is removed: the route class now authenticates before it refuses
    # (server/lifecycle.py, `route_class`), so the anonymous caller is turned
    # away before any lifecycle row is read.
    assert anon.status_code == 401, (
        f"anonymous caller was not refused: {anon.status_code} {anon.text[:300]}"
    )
    assert "born_at" not in anon.text, (
        f"anonymous caller received the dead player's record: "
        f"{anon.status_code} {anon.text[:300]}"
    )
    assert str(born_at) not in anon.text, (
        f"the owner's exact born_at leaked to an anonymous caller: {anon.text[:300]}"
    )


# ---------------------------------------------------------------------------
# Negative control: prove the refusals above can actually fail.
# ---------------------------------------------------------------------------


def test_negative_control_a_stranger_would_succeed_without_ownership_enforcement():
    """A guard that refuses everyone would make every other test here green.

    Strip authentication entirely and show the same stranger call that gets 403
    under the guard gets 200 when nothing checks it. That is the proof the 403
    in the sweep is the guard, not a coincidence.
    """
    from fastapi import FastAPI

    from server import lifecycle as lifecycle_mod

    owner = _primed_player("lc_nc_owner")
    stranger = _primed_player("lc_nc_stranger")
    path = f"/lifecycle/{owner['player_id']}"

    guarded = client.get(path, headers=_auth(stranger["token"]))
    assert guarded.status_code in (401, 403), (
        f"precondition failed: stranger was not refused by the guard, got {guarded.status_code}"
    )

    bare = FastAPI()
    lifecycle_mod.register_routes(bare, None)
    bare_client = TestClient(bare, raise_server_exceptions=False)
    unguarded = bare_client.get(path, headers=_auth(stranger["token"]))
    assert unguarded.status_code == 200, (
        f"negative control did not bite: unguarded stranger got {unguarded.status_code}. "
        "If the guard is what refuses, the refusal assertions are real."
    )
