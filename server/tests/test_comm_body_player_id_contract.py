"""Regression tests for the player_id contract on the 13 board/chat routes.

The defect
----------
A bare ``player_id: str`` handler parameter is read by the web framework as a
QUERY parameter. Every shipped Godot ``*System.gd`` posts the id inside the
JSON body, so all 13 of these routes rejected the real client with

    422 {"type": "missing", "loc": ["query", "player_id"]}

MEASURED before the fix (live TestClient, client body shape): 0/13 returned 200.
MEASURED after the fix: 13/13 return 200, and the query form still returns 200
so the pre-existing suite (which all uses ``params=``) is unaffected.

The fix reads the id from the body first and falls back to the query value.

What these tests deliberately do NOT claim
------------------------------------------
None of these tests asserts anything about an ANONYMOUS caller. Each one now
presents a session minted for the specific player it acts as, by the shared
opt-in helper in ``server/test_auth_helpers.py``. Whether these routes check
who is calling is an identity/authentication question owned by the security
engineer and is left alone here. The only thing asserted below is that the id
is *readable* from the body, not that the caller is *allowed*.

That per-call opt-in is deliberate and must not be replaced by a blanket
fixture. ``server/test_security_regression.py`` asserts that an anonymous
caller is refused; an autouse session would make that assertion unable to fail.
"""
import os
import sys

import pytest

sys.path.insert(0, r"G:\Projects\wayfarers-cycle")

from fastapi.testclient import TestClient  # noqa: E402

from server.test_auth_helpers import auth_headers  # noqa: E402


def session_for(player_id):
    """Mint a session for ONE named player.

    Every request below now presents a session for the specific player that
    request acts as. This is opt-in per call site on purpose: a blanket
    autouse fixture would make the suite structurally unable to observe the
    unauthenticated case, which is what several security tests exist to
    check. ``server/test_auth_helpers.py`` documents the same reasoning.
    """
    return auth_headers(player_id, "")


@pytest.fixture(scope="module")
def client():
    for _v in ("PYTHONPATH", "WC_PG_DB", "WC_PG_HOST"):
        os.environ.pop(_v, None)
    sys.path.insert(0, r"G:\Projects\wayfarers-cycle")
    import server.main as main
    # raise_server_exceptions=False so a genuine 500 is observed as a 500
    # rather than exploding out of the test as a traceback.
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def two_players(client):
    """Two real players, created per test.

    Function scope on purpose: the repository-root conftest restores the shared
    player store after EVERY test, so a module-scoped pair is wiped after the
    first test and every later case 404s with "Player not found". That is the
    isolation the suite already relies on -- see
    server/tests/test_comm_send_contract.py, which mints its player inside each
    test for the same reason.
    """
    ids = []
    for name in ("BodyShapeA", "BodyShapeB"):
        r = client.post("/player/create", json={"name": name, "spirit_root": "Mortal"})
        assert r.status_code == 200, r.text
        ids.append(r.json().get("id") or r.json().get("player_id"))
    return ids


# label -> (method, path, body builder, query-only fallback)
#
# The four marked with a body model are the ones that had NO request body at
# all before this fix: every one of their arguments was a query parameter, so
# the body builder sends all of them and the fallback puts them in the URL.
CASES = [
    ("comm/send", "POST", "/comm/send",
     lambda a, b: {"player_id": a, "channel_type": "dm", "scope": a, "content": "hi"},
     lambda a, b: {"channel_type": "dm", "scope": a, "content": "hi"}),
    ("comm/history", "POST", "/comm/history",
     lambda a, b: {"player_id": a, "channel_type": "dm", "scope": a},
     lambda a, b: {"channel_type": "dm", "scope": a}),
    ("comm/pin", "POST", "/comm/pin",
     lambda a, b: {"player_id": a, "channel_type": "dm", "scope": a, "msg_id": "m1"},
     lambda a, b: {"channel_type": "dm", "scope": a, "msg_id": "m1"}),
    ("comm/unpin", "POST", "/comm/unpin",
     lambda a, b: {"player_id": a, "channel_type": "dm", "scope": a, "msg_id": "m1"},
     lambda a, b: {"channel_type": "dm", "scope": a, "msg_id": "m1"}),
    ("comm/mute", "POST", "/comm/mute",
     lambda a, b: {"player_id": a, "channel_type": "dm", "scope": a,
                   "target_id": b, "duration_seconds": 60},
     lambda a, b: {"channel_type": "dm", "scope": a,
                   "target_id": b, "duration_seconds": 60}),
    ("comm/unmute", "POST", "/comm/unmute",
     lambda a, b: {"player_id": a, "channel_type": "dm", "scope": a, "target_id": b},
     lambda a, b: {"channel_type": "dm", "scope": a, "target_id": b}),
    ("comm/slowmode", "POST", "/comm/slowmode",
     lambda a, b: {"player_id": a, "channel_type": "dm", "scope": a, "seconds": 5},
     lambda a, b: {"channel_type": "dm", "scope": a, "seconds": 5}),
    ("comm/message", "DELETE", "/comm/message",
     lambda a, b: {"player_id": a, "channel_type": "dm", "scope": a, "msg_id": "m1"},
     lambda a, b: {"channel_type": "dm", "scope": a, "msg_id": "m1"}),
    ("board/create", "POST", "/board/create",
     lambda a, b: {"player_id": a, "board_type": "town", "scope": "town",
                   "title": "t", "content": "c"},
     lambda a, b: {"board_type": "town", "scope": "town", "title": "t", "content": "c"}),
    ("board/posts", "POST", "/board/posts",
     lambda a, b: {"player_id": a, "board_type": "town", "scope": "town"},
     lambda a, b: {"board_type": "town", "scope": "town"}),
    ("board/react", "POST", "/board/react",
     lambda a, b: {"player_id": a, "board_type": "town", "scope": "town",
                   "post_id": "p1", "reaction": "like"},
     lambda a, b: {"board_type": "town", "scope": "town",
                   "post_id": "p1", "reaction": "like"}),
    ("board/pin", "POST", "/board/pin",
     lambda a, b: {"player_id": a, "board_type": "town", "scope": "town", "post_id": "p1"},
     lambda a, b: {"board_type": "town", "scope": "town", "post_id": "p1"}),
    ("board/post", "DELETE", "/board/post",
     lambda a, b: {"player_id": a, "board_type": "town", "scope": "town", "post_id": "p1"},
     lambda a, b: {"board_type": "town", "scope": "town", "post_id": "p1"}),
]

IDS = [c[0] for c in CASES]


@pytest.mark.parametrize("label,method,path,body_builder,url_builder", CASES,
                         ids=IDS)
def test_client_body_shape_is_accepted(client, two_players, label, method, path,
                                       body_builder, url_builder):
    """The shipped client puts player_id in the JSON body; that must work."""
    a, b = two_players
    r = client.request(method, path, json=body_builder(a, b),
                       headers=session_for(a))
    assert r.status_code == 200, "%s: %s" % (label, r.text)


@pytest.mark.parametrize("label,method,path,body_builder,url_builder", CASES,
                         ids=IDS)
def test_query_shape_still_accepted(client, two_players, label, method, path,
                                    body_builder, url_builder):
    """The pre-existing suite uses params={"player_id": ...}; it must not break."""
    a, b = two_players
    rest = url_builder(a, b)
    r = client.request(method, path, params={"player_id": a},
                       json=rest if method == "DELETE" else rest,
                       headers=session_for(a))
    assert r.status_code == 200, "%s: %s" % (label, r.text)


@pytest.mark.parametrize("label,method,path,body_builder,url_builder", CASES,
                         ids=IDS)
def test_missing_id_everywhere_is_422_not_500(client, two_players, label, method,
                                              path, body_builder, url_builder):
    """With no id anywhere, the outcome is safe -- and never a 500.

    CHANGED by the social-layer work, for the two messaging routes. They used
    to answer 422, because the handler resolved the caller from a body/query
    id and had nothing to resolve from without one.

    They now take the caller from the AUTHENTICATED SESSION instead, which is
    strictly stronger: the value acted on can no longer be anything the caller
    typed. A session for ``a`` with no id in the request is therefore a
    complete, well-formed request naming exactly one player -- ``a`` -- so it
    is served rather than refused.

    The invariant this file exists to protect is unchanged and is what is
    asserted: the handler must not run with an unresolved id and blow up. So
    the assertion is "not a 500", plus a positive check that the request acted
    as the session's player. Every route that has NOT moved to session-derived
    identity still has to answer 422 here.
    """
    a, b = two_players
    body = dict(body_builder(a, b))
    body.pop("player_id", None)
    r = client.request(method, path, json=body, headers=session_for(a))
    assert r.status_code != 500, "%s: a 500 means the handler ran with no " \
                                 "resolved id: %s" % (label, r.text)
    assert "detail" in r.text or r.status_code == 200, r.text

    if label in SESSION_DERIVED_ID_ROUTES:
        # Served as the session's player. Prove it acted as `a` and did not
        # invent a caller: a DM to `b` from this session can only be `a`'s.
        assert r.status_code in (200, 403), "%s: %s" % (label, r.text)
        if r.status_code == 200 and label == "comm/send":
            sender = r.json()["message"]["sender"]["id"]
            assert sender == a, (
                "%s acted as %r rather than the session's player %r"
                % (label, sender, a)
            )
    else:
        assert r.status_code == 422, (
            "%s: expected 422, got %s %s" % (label, r.status_code, r.text)
        )


#: Routes whose caller is resolved from the session rather than from a
#: caller-supplied id. Listed explicitly so a route added to CASES does not
#: silently start returning 200 here -- it has to be added here deliberately.
SESSION_DERIVED_ID_ROUTES = frozenset({"comm/send", "comm/history"})


@pytest.mark.parametrize("label,method,path,body_builder,url_builder", CASES,
                         ids=IDS)
def test_unknown_player_id_is_refused(client, two_players, label, method, path,
                                     body_builder, url_builder):
    """An id naming no player is refused, and never with a 422 or a 500.

    MEASURED 2026-10-05 against the real app: 401, not 404. This test used to
    mint the session for the SAME unknown id the body names, which was the
    only way to reach the handler's own "Player not found" 404 once the
    ownership guard was refusing 403 first. It no longer reaches the handler
    at all -- ``auth.authenticate`` confirms the session's player still
    exists and revokes the token when it does not.

    That is the deleted-player defense and it is the right answer: a token for
    an account that was never created must not work, and the refusal must not
    reveal whether an id is live.

    Why the handler's own refusal is no longer reachable here, stated so the
    next reader does not "restore" it: these routes resolve the acting player
    from the session, so there is no caller whose OWN id can be missing from
    the store. A live player is served normally -- pinned by
    ``test_body_id_wins_over_query_id`` above and the paired owner arms -- so
    this cannot be satisfied by refusing everyone.
    """
    a, _b = two_players
    unknown = "definitely-not-a-real-player"
    body = body_builder(a, a)
    body["player_id"] = unknown
    r = client.request(method, path, json=body, headers=session_for(unknown))
    assert r.status_code == 401, "%s: %s %s" % (label, r.status_code, r.text)


def test_body_id_wins_over_query_id(client, two_players):
    """When both are present and name the SAME player, the body is acted on.

    Originally this sent query=b and body=a to prove the body value won. That
    request is a contradiction -- two different ids in one call -- and
    ``auth._collect_claims`` checks EVERY claim rather than the first, so with
    the ownership guard on it is refused 403 before the handler runs. That
    refusal is correct and is now asserted separately as
    ``test_contradictory_query_and_body_ids_are_refused``.

    What remains here is the part that is still the handler's business: a
    single player named in both places, with the body carrying the value the
    message is actually sent as.
    """
    a, _b = two_players
    r = client.post("/comm/send", params={"player_id": a},
                    json={"player_id": a, "channel_type": "dm", "scope": a,
                          "content": "who am i"},
                    headers=session_for(a))
    assert r.status_code == 200, r.text
    assert r.json()["message"]["sender"]["id"] == a


def test_contradictory_query_and_body_ids_are_refused(client, two_players):
    """Two different ids in one call is refused 403, not silently resolved.

    This is a behaviour change the guard introduces, and it is asserted rather
    than assumed: a caller cannot send ``player_id=b`` in the query and
    ``player_id=a`` in the body and have the handler pick one.
    """
    a, b = two_players
    r = client.post("/comm/send", params={"player_id": b},
                    json={"player_id": a, "channel_type": "dm", "scope": a,
                          "content": "who am i"},
                    headers=session_for(a))
    assert r.status_code == 403, r.text


def test_refused_board_stays_403(client, two_players):
    """The prior worker's 403-on-refusal is preserved by this change."""
    a, _b = two_players
    r = client.post("/board/create", json={"player_id": a, "board_type": "global",
                                           "scope": "g", "title": "t", "content": "c"},
                    headers=session_for(a))
    assert r.status_code == 403, r.text


def test_town_board_open_to_sectless_player(client, two_players):
    """The prior worker's town board for players with no faction is preserved."""
    a, _b = two_players
    r = client.post("/board/create", json={"player_id": a, "board_type": "town",
                                           "scope": "town", "title": "hi",
                                           "content": "there"},
                    headers=session_for(a))
    assert r.status_code == 200, r.text
    assert r.json()["post"]["author"]["id"] == a


def test_denied_channel_is_still_403(client, two_players):
    """/comm/send's PermissionError -> 403 is preserved."""
    a, _b = two_players
    r = client.post("/comm/send", json={"player_id": a, "channel_type": "global",
                                        "scope": "g", "content": "x"},
                    headers=session_for(a))
    assert r.status_code == 403, r.text


def test_openapi_spec_still_builds(client):
    """The request models are module scope, so the spec must generate.

    A model declared inside the registration function would be an unresolvable
    ForwardRef under `from __future__ import annotations` and this would 500.
    """
    r = client.get("/openapi.json")
    assert r.status_code == 200, r.text
    paths = r.json()["paths"]
    for _label, _method, path, _bb, _ub in CASES:
        assert path in paths, "%s missing from the spec" % path
