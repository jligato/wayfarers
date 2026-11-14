"""Regression tests for the board create path (POST /board/create).

Every assertion here was first measured live with fastapi's TestClient. The
"before" numbers in each docstring are real responses, not predictions.

The defects
-----------
1. ``CreatePostRequest.category`` was a REQUIRED field, so a client that omits
   it could not create a post at all. It now defaults to ``"general"``.
   MEASURED: this one is ALREADY FIXED in HEAD -- a probe that omits the field
   returned 200 before this file existed. The default is kept pinned by test
   below so it cannot silently regress into required again.

2. ``BulletinStore._can_post`` gated every board type on either a sect id or a
   staff role, so a player who belongs to no sect could not post ANYWHERE.
   MEASURED: a sectless player got HTTP 500 (unhandled PermissionError) on
   sect, realm and global boards alike. A "town" board is now open to any real
   player, and the denial is a 403 instead of a 500.

Sessions
--------
Every request below presents a session minted for the SPECIFIC player it acts
as, via the shared opt-in helper ``server/test_auth_helpers.py``. That is
per-call on purpose: a blanket autouse session would make this file unable to
observe the unauthenticated case, which is what the security tests elsewhere
exist to check.

What these tests deliberately do NOT claim
-----------------------------------------
The 403-vs-500 change is asserted, but the routes still take ``player_id`` as a
bare query parameter while both clients put it in the JSON body, so a real
client request still gets 422 ``missing@query.player_id``. That is an identity
contract owned by another engineer and is left alone here; see the open-issues
note in the change report.
"""
import os, sys

import pytest

sys.path.insert(0, r"G:\Projects\wayfarers-cycle")

from fastapi.testclient import TestClient

from server.test_auth_helpers import auth_headers


def session_for(player_id):
    """Mint a session for ONE named player, the way trusted server code does.

    Opt-in per call site on purpose. A blanket autouse session would make this
    file unable to observe the unauthenticated case, and
    ``server/test_auth_helpers.py`` says the same. ``auth_headers`` calls
    ``auth.issue_session`` directly, so it needs no signing key and works for
    rows that never went through ``/player/create``.
    """
    return auth_headers(player_id, "")


@pytest.fixture(scope="module")
def client():
    for _v in ("PYTHONPATH", "WC_PG_DB", "WC_PG_HOST"):
        os.environ.pop(_v, None)
    sys.path.insert(0, r"G:\Projects\wayfarers-cycle")
    import server.main as main
    # raise_server_exceptions=False so a genuine 500 is observed as a 500
    # instead of exploding out of the test as a traceback.
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


def _new_player(c, name, sect_id=None, sect_role="Member"):
    """Create a player through the real route, then give it a sect if asked."""
    r = c.post("/player/create", json={"name": name, "spirit_root": "Mortal"})
    assert r.status_code == 200, r.text
    pid = r.json().get("id") or r.json().get("player_id")
    if sect_id is not None:
        import server.main as main
        with main.players_db.transaction():
            main.players_db[pid]["sect_id"] = sect_id
            main.players_db[pid]["sect_role"] = sect_role
    return pid


def _post(c, pid, **body):
    body.setdefault("title", "T")
    body.setdefault("content", "C")
    return c.post("/board/create", params={"player_id": pid}, json=body,
                  headers=session_for(pid))


# --------------------------------------------------------------------------
# Defect 1: category is optional and defaults to "general"
# --------------------------------------------------------------------------

def test_create_post_without_category_succeeds(client):
    """A client that omits 'category' must still create the post.

    MEASURED before: 200 even with the field absent, so this pins an existing
    fix. It is here because the field being required again is the regression.
    """
    pid = _new_player(client, "BoardCatOmitted", sect_id="sect_cat")
    r = _post(client, pid, board_type="sect", scope="sect_cat")
    assert r.status_code == 200, r.text
    post = r.json()["post"]
    assert post["category"] == "general", (
        f"omitted category should default to 'general', got {post['category']!r}"
    )


def test_explicit_category_is_honoured_not_overwritten(client):
    """The default must not clobber a category the client did send."""
    pid = _new_player(client, "BoardCatExplicit", sect_id="sect_cat2")
    r = _post(client, pid, board_type="sect", scope="sect_cat2", category="trade")
    assert r.status_code == 200, r.text
    assert r.json()["post"]["category"] == "trade"


def test_category_field_is_not_required_on_the_model():
    """Unit-level cause of defect 1: the field must not be required.

    Asserted on the model, not on a string, so a rename cannot fake a pass.
    """
    from server.communication import CreatePostRequest
    fields = CreatePostRequest.model_fields
    assert "category" in fields, "CreatePostRequest must declare category"
    assert fields["category"].is_required() is False, (
        "category must have a default; the client does not always send it"
    )
    assert CreatePostRequest(board_type="sect", scope="s", title="t", content="c"
                             ).category == "general"


# --------------------------------------------------------------------------
# Defect 2: a player with no sect can post on the town board
# --------------------------------------------------------------------------

def test_sectless_player_can_post_on_town_board(client):
    """The reported bug: no sect meant no posting anywhere.

    MEASURED before: 500 on sect, realm AND global boards for a sectless player.
    """
    pid = _new_player(client, "BoardSectless")
    r = _post(client, pid, board_type="town", scope="town")
    assert r.status_code == 200, (
        f"sectless player must be able to post, got {r.status_code}: {r.text[:200]}"
    )
    post = r.json()["post"]
    assert post["board_type"] == "town"
    assert post["author"]["id"] == pid


def test_sectless_player_post_is_readable_back(client):
    """The write must be a real post, not a response the store discarded."""
    pid = _new_player(client, "BoardSectlessRead")
    assert _post(client, pid, board_type="town", scope="town_rb").status_code == 200
    r = client.post("/board/posts", params={"player_id": pid},
                    json={"board_type": "town", "scope": "town_rb"},
                    headers=session_for(pid))
    assert r.status_code == 200, r.text
    assert any(p["author"]["id"] == pid for p in r.json()["posts"])


def test_sectless_player_and_sect_member_share_the_town_board(client):
    """Town is one shared board, not a per-player board."""
    a = _new_player(client, "BoardTownA")
    b = _new_player(client, "BoardTownB", sect_id="sect_town_b")
    assert _post(client, a, board_type="town", scope="town_shared").status_code == 200
    assert _post(client, b, board_type="town", scope="town_shared").status_code == 200
    r = client.post("/board/posts", params={"player_id": a},
                    json={"board_type": "town", "scope": "town_shared"},
                    headers=session_for(a))
    ids = {p["author"]["id"] for p in r.json()["posts"]}
    assert {a, b} <= ids


# --------------------------------------------------------------------------
# Invariants the fix must NOT have broken (sect privacy)
# --------------------------------------------------------------------------

def test_sectless_player_is_refused_someone_elses_sect_board(client):
    """Fixing 'cannot post at all' must not mean 'can post anywhere'.

    MEASURED before: 500. MEASURED after: 403. The status changed, the refusal
    did not.
    """
    pid = _new_player(client, "BoardNoTrespass")
    r = _post(client, pid, board_type="sect", scope="sect_private")
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text[:200]}"
    assert "post access" in r.json()["detail"]


def test_sect_member_is_refused_another_sects_board(client):
    """Cross-sect posting stays closed, and is a 403 not a 500."""
    pid = _new_player(client, "BoardCrossSect", sect_id="sect_own")
    r = _post(client, pid, board_type="sect", scope="sect_other")
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text[:200]}"


def test_sect_member_can_still_post_to_their_own_sect_board(client):
    """The pre-existing happy path must keep working."""
    pid = _new_player(client, "BoardOwnSect", sect_id="sect_own2")
    r = _post(client, pid, board_type="sect", scope="sect_own2")
    assert r.status_code == 200, r.text
    assert r.json()["post"]["scope"] == "sect_own2"


def test_staff_only_boards_stay_closed_to_an_ordinary_player(client):
    """realm and global are staff-gated; town must not have opened them."""
    pid = _new_player(client, "BoardNotStaff", sect_id="sect_ordinary")
    for board_type, scope in (("realm", "Mortal"), ("global", "global")):
        r = _post(client, pid, board_type=board_type, scope=scope)
        assert r.status_code == 403, f"{board_type} leaked: {r.status_code} {r.text[:200]}"


def test_unknown_board_type_is_denied_not_allowed_by_fallthrough(client):
    """A typo must be refused, proving the town branch is not a catch-all."""
    pid = _new_player(client, "BoardTypo")
    r = _post(client, pid, board_type="sectt", scope="town")
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text[:200]}"


# --------------------------------------------------------------------------
# Negative controls: prove the harness can still fail
# --------------------------------------------------------------------------

def test_missing_required_field_is_still_422(client):
    """Delete a required field; the body must be rejected, not defaulted."""
    pid = _new_player(client, "BoardNegMissing", sect_id="sect_neg")
    r = client.post("/board/create", params={"player_id": pid},
                    json={"board_type": "sect", "scope": "sect_neg"},
                    headers=session_for(pid))
    assert r.status_code == 422, r.text
    assert any(d.get("loc") == ["body", "title"] for d in r.json()["detail"])


def test_wrong_type_is_still_422(client):
    """A bad type must not be coerced."""
    pid = _new_player(client, "BoardNegType", sect_id="sect_neg")
    r = _post(client, pid, board_type="sect", scope="sect_neg", expiry_days="soon")
    assert r.status_code == 422, r.text


def test_unknown_player_is_refused_by_the_auth_layer(client):
    """A caller naming an id that was never created is refused with 401.

    MEASURED 2026-10-05 against the real app. This used to read 404, which was
    the handler's own "Player not found" answer, reached by minting the
    session FOR the unknown id so the ownership guard would not answer 403
    first. It no longer reaches the handler: ``auth.authenticate`` confirms the
    session's player still exists and revokes the token when it does not.

    That refusal is the deleted-player defense and it is the right one -- a
    token for an account that was never created must not work, and the status
    must not tell a stranger whether an id is live. This is the ONLY arm that
    can observe the guard's answer, and the happy path is pinned by the
    board-create tests above, so it cannot be satisfied by refusing everyone.
    """
    unknown = "definitely-not-real"
    r = client.post("/board/create", params={"player_id": unknown},
                    json={"board_type": "town", "scope": "town",
                          "title": "T", "content": "C"},
                    headers=session_for(unknown))
    assert r.status_code == 401, r.text
