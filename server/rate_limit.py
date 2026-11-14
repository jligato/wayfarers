"""Per-player rate limit for the gameplay CLAIM routes.

WHY THIS EXISTS
---------------
Measured before this module existed: 40 rapid ``POST /sign-in/{player_id}/claim``
calls all answered 200. The claim family (sign-in streak, battle pass, daily
boss, offline progress, events, missions, monthly event, leaderboard payout,
gift series, achievements, inner skills, daily streak, lifecycle) is where the
game pays out currency and items. Those are the routes worth spamming, and the
only thing standing between a script and a payout loop was the per-day ledger
inside each individual feature -- which stops a DOUBLE claim but says nothing
about VOLUME. Forty parallel claims race forty ledger reads.

So this is an abuse brake on request volume, not a correctness fix. It is the
same role the signup limiter plays on ``POST /player/create``: cheap, in-memory,
per-process, resets on restart.

SHAPE -- REUSED, NOT INVENTED
-----------------------------
Deliberately the same shape as the two limiters already in the tree, because a
third dialect would be a third thing to audit:

* ``social_routes.WindowLimiter`` (social_routes.py:313) -- per-key deque of
  attempt instants, lazy eviction, 429 carrying ``Retry-After``.
* ``social_policy.MessageRateLimiter`` (social_policy.py:144) -- the same, plus
  the window-start dict and the sweep that forgets a key once its window has
  CLOSED (not once a counter reaches a number), the injectable ``clock``, and
  the lock FastAPI's threadpool makes mandatory.
* ``main._enforce_signup_rate_limit`` (main.py:1756) -- the 429 body shape, the
  env-overridable cap/window, and the rule that an in-process mount has no
  remote caller to throttle.

WHAT IS DELIBERATELY DIFFERENT FROM THE SIGNUP LIMITER
-----------------------------------------------------
The signup limiter keys on the caller's NETWORK ADDRESS and refuses to count
an in-process mount at all, because FastAPI's test client reports the literal
peer string ``testclient`` for every request. Keying on that lumps a whole test
suite into one bucket; measured, that refused the 6th account any test file
created and failed 203 unrelated tests, each of which passed alone.

This limiter keys on the PLAYER, taken from the route's own path parameter
first. Two consequences, both intended:

* a test file that creates its own player gets its own bucket, so this cannot
  reproduce the 203-test failure;
* a test CAN prove the limiter bites, which the address-keyed signup limiter
  structurally cannot do in-process.

The ``testclient`` refusal is kept ONLY as the last-resort fallback for a claim
route that names no player in its path and arrives with no session. See
:func:`_caller_key`.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Tuple

from fastapi import HTTPException, Request
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute

__all__ = [
    "CLAIM_RATE_LIMIT",
    "CLAIM_RATE_WINDOW_SECONDS",
    "ClaimRateLimiter",
    "claim_limiter",
    "enforce_claim_rate_limit",
    "install_claim_rate_limit",
    "reset_claim_rate_limit",
]
# ``DEBIT_VERBS`` is deliberately NOT in ``__all__``. ``test_rate_limit_contract``
# pins the exported list to exactly these seven names, and that file is not this
# change's to edit. The constant is still importable as a module attribute --
# ``rate_limit.DEBIT_VERBS`` -- which is all the coverage test needs.


def _env_int(name: str, default: int) -> int:
    """Positive-int env override; a blank or unparseable value keeps the default.

    Copied from ``main._env_int`` (main.py:1685) so an operator learns one
    spelling, not two.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


#: Claims one player may make per window. 30/60s is well above what a person
#: does -- the shipped client's claim cadence is a handful per minute across
#: every claim surface -- and it refuses the 40-call burst the audit measured.
#: Overridable so a deployment can move it without a code change, and so a
#: test can drop it to a small number instead of making 30 real requests.
CLAIM_RATE_LIMIT = _env_int("WC_CLAIM_RATE_LIMIT", 30)

#: Window length in seconds. Expiry is driven by THIS clock, never by a counter
#: reaching a value: a caller who stops claiming has their bucket swept the next
#: time any claim is enforced, which is the first moment it could no longer
#: change a decision.
CLAIM_RATE_WINDOW_SECONDS = _env_int("WC_CLAIM_RATE_WINDOW_SECONDS", 60)

#: Floor for the window. A window of 0 (or negative) is not a fast limit, it
#: is NO limit: the expiry test ``now - start >= window`` is then true for
#: every instant, so each call resets its own bucket and the cap can never
#: be reached. Measured: limit=1 with window_seconds=0.0 allowed 50 of 50.
#: The constructor clamps to this, the way it already clamps the limit.
_MIN_WINDOW_SECONDS: float = 0.001

#: The peer string every in-process ASGI mount reports. Not an IP address.
#: Same sentinel set as main.py:1701.
_INPROCESS_PEER_SENTINELS = frozenset({"testclient", "testclient.net", ""})

#: Route path params that name the acting player. Same set ownership_guard
#: matches on (ownership_guard.py:163), so the two never disagree about which
#: parameter identifies a player.
_PLAYER_PATH_PARAMS: Tuple[str, ...] = ("player_id", "pid")

#: Never mounted by this installer. Kept as data, not as a comment, because a
#: comment is not something a test can assert.
#:
#: * ``server/sect_routes.py`` is owned by another agent this wave and is out
#:   of scope for this change.
#: * ``/wallet/earn`` is main.py's own EXEMPT_PATH (main.py:195): it mints
#:   currency and its handler gate refuses a player session outright, so
#:   throttling it by player would break the only caller it is written for.
EXEMPT_PATHS: frozenset = frozenset({
    "/wallet/earn",
})

#: The DEBIT side of the same brake, as a second shape matcher.
#:
#: ``_is_claim_route`` only catches paths carrying a ``claim`` segment, so it
#: catches the routes where the player RECEIVES currency. The mirror half --
#: where the player SPENDS it -- carries no ``claim`` word at all and shipped
#: with no volume brake: 30 debit-bearing POST routes, measured by walking
#: ``main.app`` for POST routes whose last path segment starts with one of the
#: verbs below. They debit a balance and re-read it in the same request, so
#: forty parallel spends race forty balance reads in the same way forty parallel
#: claims race forty ledger reads.
#:
#: Matched on the LAST segment, PREFIX-wise, for the reason the claim matcher is
#: prefix-wise: equality against the bare verb misses the hyphenated and
#: suffixed siblings this tree actually ships (``/shop/buyout``,
#: ``/monthly-event/{player_id}/buy-shop``,
#: ``/battle-pass/api/purchase-tier``). ``unequip`` is spelled out because it
#: does NOT start with ``equip``.
#:
#: Kept as data, not as a comment, because a comment is not something a test can
#: assert and this set is the whole contract with
#: ``server/tests/test_claim_rate_limit_coverage.py``.
DEBIT_VERBS: Tuple[str, ...] = (
    "spend",
    "buy",
    "purchase",
    "pull",
    "convert",
    "contribute",
    "donate",
    "upgrade",
    "craft",
    "forge",
    "reforge",
    "equip",
    "unequip",
    "temper",
    "validate",
)


class ClaimRateLimiter:
    """Per-player sliding-window limiter for claim routes.

    Shaped after ``social_policy.MessageRateLimiter`` (social_policy.py:144),
    which is itself shaped after ``main._enforce_signup_rate_limit``:

    * one deque of attempt instants per key;
    * a window-start per key, so a burst that straddles a window boundary is
      still counted as one burst;
    * a lazy sweep that forgets a key whose window has CLOSED -- the reason the
      dict is bounded rather than growing one entry per player ever seen;
    * a 429 carrying ``Retry-After``;
    * an injectable ``clock``, so a test can cross a window boundary without
      sleeping;
    * a lock, because FastAPI runs sync handlers in a threadpool and two claims
      from one player really can interleave.

    The key is the PLAYER id, never the network address: keying on address lets
    one player reset their allowance by changing address, and punishes every
    player behind one shared address for one player's behaviour.
    """

    def __init__(
        self,
        limit: int = CLAIM_RATE_LIMIT,
        window_seconds: float = CLAIM_RATE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = max(1, int(limit))
        self._window = max(_MIN_WINDOW_SECONDS, float(window_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: Dict[str, deque] = {}
        self._window_start: Dict[str, float] = {}

    @property
    def limit(self) -> int:
        """The cap, exposed so a test can assert the shipped default."""
        return self._limit

    @property
    def window_seconds(self) -> float:
        """The window, exposed so a test can assert the shipped default."""
        return self._window

    def check(self, key: str) -> None:
        """Count one claim attempt for ``key``; 429 when over the cap.

        Call this BEFORE the handler pays anything, so a refused burst costs the
        player nothing and mutates no ledger row.
        """
        if not key:
            # No key means no caller to attribute the call to. Counting it would
            # lump every such caller into one shared bucket, which is the exact
            # failure the signup limiter documents. Let it through; the feature's
            # own ledger is what stops a repeated claim from paying twice.
            return
        now = float(self._clock())
        with self._lock:
            self._evict_locked(now)
            attempts = self._attempts.get(key)
            if attempts is None:
                attempts = deque()
                self._attempts[key] = attempts
            # SLIDING window, not a fixed one: forget every instant that has
            # aged out of the window instead of resetting the whole bucket
            # when its FIRST instant does. Resetting the bucket is what let a
            # burst straddling the boundary be counted twice -- measured 20
            # claims inside 60.1s against a cap of 10, over real HTTP.
            while attempts and now - attempts[0] >= self._window:
                attempts.popleft()
            if len(attempts) >= self._limit:
                # Wait for the OLDEST live attempt to age out, not for the
                # bucket's start: that is the instant the next slot frees.
                # Ceiling, so the header never tells a client to come back
                # before a slot is actually free.
                retry_after = max(
                    1,
                    int(math.ceil(self._window - (now - attempts[0]))),
                )
                raise HTTPException(
                    status_code=429,
                    detail=(
                        "You are claiming rewards too quickly. "
                        f"Try again in {retry_after}s."
                    ),
                    headers={"Retry-After": str(retry_after)},
                )
            attempts.append(now)
            # The sweep's handle on this key is its OLDEST LIVE instant, so
            # ``now - start >= window`` means every instant for the key has
            # aged out and the whole key can go. Recorded after the append so
            # a freshly emptied bucket is never left untracked.
            self._window_start[key] = attempts[0]

    def remaining(self, key: str) -> int:
        """Claims still available to ``key`` in the current window."""
        with self._lock:
            attempts = self._attempts.get(key)
            return max(0, self._limit - len(attempts)) if attempts else self._limit

    def tracked_keys(self) -> int:
        """How many keys the limiter is holding. Bounded, or the sweep is broken."""
        with self._lock:
            return len(self._attempts)

    def reset(self) -> None:
        """Drop all counters. Exposed so a test can start from a clean slate."""
        with self._lock:
            self._attempts.clear()
            self._window_start.clear()

    def _evict_locked(self, now: float) -> None:
        """Forget every key whose window has already closed.

        Lazy, for the reason ``MessageRateLimiter._evict_locked`` gives: there is
        no background thread or timer for a cache eviction, and a key is dropped
        the first time ANY claim is enforced after its window closes.

        The sweep runs on each key's oldest LIVE attempt, not on a counter, so a
        caller who claims twice an hour forever never accumulates a stale entry
        -- their window closes between every claim. It trims per-instant for
        the same reason :meth:`check` does: a key is deleted only when it holds
        no live attempt at all.
        """
        # Trim per key, then forget the key only once NOTHING live is left.
        # Dropping the whole key the instant its OLDEST attempt aged out would
        # forgive the attempts that are still inside the window -- a fixed
        # window wearing a sliding window's name. Every key here is either
        # live or about to be deleted, so the dict stays bounded: a player who
        # stops claiming leaves no entry behind.
        empty = []
        for key, attempts in self._attempts.items():
            while attempts and now - attempts[0] >= self._window:
                attempts.popleft()
            if attempts:
                self._window_start[key] = attempts[0]
            else:
                empty.append(key)
        for key in empty:
            self._window_start.pop(key, None)
            self._attempts.pop(key, None)


#: The limiter the installer mounts. Module-level and process-wide so every
#: claim route shares one bucket per player: a player who spreads a burst across
#: 12 claim surfaces has still made 12 claims, and per-route limiters would not
#: see that.
claim_limiter = ClaimRateLimiter()


def reset_claim_rate_limit() -> None:
    """Clear the shared limiter's counters."""
    claim_limiter.reset()


def _client_address(request: Request) -> str | None:
    """The caller's real network address, or None when there isn't one.

    Copied from ``main._client_address`` (main.py:1707), sentinel set included,
    so the "is this a real address?" gate means the same thing in both limiters.
    """
    peer = request.client.host if request.client else None
    if peer and peer not in _INPROCESS_PEER_SENTINELS:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return None


def _caller_key(request: Request) -> str | None:
    """The key this request is counted against, or None to skip counting.

    In priority order:

    1. the player named by the route's own path parameter. This is the common
       case for the claim family (``/sign-in/{player_id}/claim`` and most of its
       siblings) and needs no session at all.
    2. the session token, when the route names no player in its path (the body
       carries it instead, as ``POST /missions/claim`` does). Keyed on the token
       rather than the body field because the body is not readable here without
       consuming the request stream, and a session token is at least stable and
       un-guessable.
    3. the real network address, as a last resort.
    4. None -- an in-process mount with no player and no session. There is no
       remote caller to throttle, and counting it would lump every such request
       in the process into one bucket.
    """
    for param in _PLAYER_PATH_PARAMS:
        value = request.path_params.get(param)
        if value:
            return f"player:{value}"
    token = (request.headers.get("authorization") or "").strip()
    if token:
        # Digest rather than store: the bucket key must not be a live bearer
        # token, and a per-token bucket is exactly the per-session allowance the
        # docstring argues against. This is a routing key, not a secret store;
        # it never leaves the process.
        import hashlib

        return "token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    address = _client_address(request)
    return f"addr:{address}" if address else None


def enforce_claim_rate_limit(request: Request) -> None:
    """Dependency body: count one claim for this request's player.

    Mounted by :func:`install_claim_rate_limit`. Raises 429 when the player is
    over the cap.
    """
    claim_limiter.check(_caller_key(request) or "")


def _is_claim_route(route: Any) -> bool:
    """True when this route pays out and answers POST.

    Shape-matched, not an allowlist of paths: a claim surface added next month is
    covered without editing this file, which is the same reason
    ``ownership_guard`` matches by shape. ``claim`` as a path SEGMENT catches
    ``/claim``, ``/claim-all``, ``/claim-offline``, ``/claim-payout``,
    ``/claim-daily``, ``/claim-loot`` and ``/claim-reward`` alike.
    """
    if not isinstance(route, APIRoute):
        return False
    if "POST" not in (getattr(route, "methods", None) or ()):
        return False
    if route.path in EXEMPT_PATHS:
        return False
    # PREFIX match on the segment, not equality. An equality test against the
    # bare word "claim" misses every hyphenated sibling the tree actually ships:
    # /wallet/claim-offline, /cultivation/claim-offline/v2,
    # /lifecycle/{pid}/claim-offline, /battle-pass/{player_id}/claim-all,
    # /leaderboard/{player_id}/claim-payout, /monthly-event/{player_id}/claim-daily,
    # /battle-pass/api/claim-reward, /instance/claim-loot. Measured on this tree:
    # equality matched 3 claim routes, the prefix matched all of them.
    segments = route.path.strip("/").split("/")
    return any(segment.startswith("claim") for segment in segments)


def _is_debit_route(route: Any) -> bool:
    """True when this route DEBITS currency/items and answers POST.

    The mirror of :func:`_is_claim_route`. Same three gates (POST API route,
    not an :data:`EXEMPT_PATHS` entry, POST method) so the two matchers cannot
    disagree about what is a throttleable route; only the shape test differs.

    Shape, not an allowlist, for the same reason the claim matcher is: a new
    spending surface is covered without editing this file. The test is on the
    LAST path segment and is a PREFIX test -- ``/shop/buyout``,
    ``/monthly-event/{player_id}/buy-shop`` and ``/battle-pass/api/purchase-tier``
    all end in a verb plus a suffix, and an equality test against the bare verb
    would miss every one of them.

    NOT mounted on the GET twins: an equip listing pays nothing and throttling it
    would break a screen that reads state on a timer.
    """
    if not isinstance(route, APIRoute):
        return False
    if "POST" not in (getattr(route, "methods", None) or ()):
        return False
    if route.path in EXEMPT_PATHS:
        return False
    segments = route.path.strip("/").split("/")
    return segments[-1].startswith(DEBIT_VERBS)


def _is_throttled_payout_route(route: Any) -> bool:
    """True for either half of the payout surface: a claim OR a debit.

    The single predicate :func:`install_claim_rate_limit` mounts on. Kept as one
    function so the installer has exactly one place that decides "is this a
    payout route", and so a caller auditing coverage does not have to know that
    there are two matchers.
    """
    return _is_claim_route(route) or _is_debit_route(route)


def install_claim_rate_limit(app: Any) -> Dict[str, List[str]]:
    """Attach :func:`enforce_claim_rate_limit` to every PAYOUT route on ``app``.

    Both halves of the payout surface: the CLAIM routes (where currency arrives)
    and the DEBIT routes (where it leaves). Measured on this tree the claim
    matcher alone covered 30 routes and the 30 debit routes below carried no
    volume brake at all, so the returned set roughly doubles. See
    ``server/tests/test_claim_rate_limit_coverage.py``, which names every path it
    expects rather than counting.

    Mirrors ``ownership_guard.install_ownership_guard`` (ownership_guard.py:477):
    it walks ``app.routes``, appends a dependency to the matched routes'
    dependants, and RETURNS what it changed so a caller (or a test) can assert
    on the actual set rather than on a count.

    Idempotent: a route already carrying this dependency is skipped, so a second
    call is a no-op and it is safe from more than one mount point.

    ORDERING -- read before moving the call. This appends to
    ``route.dependant.dependencies``, which FastAPI resolves when the route is
    first served. It therefore has to run after every ``@app.post`` decorator and
    every ``register_*()`` call that declares a claim route -- otherwise the
    later-declared ones are never seen. Mount it beside
    ``install_ownership_guard`` at the bottom of ``main.py``, never higher.
    """
    from rate_limit import enforce_claim_rate_limit as _dep

    guarded: Dict[str, List[str]] = {}
    for route in list(getattr(app, "routes", None) or ()):
        if not _is_throttled_payout_route(route):
            continue
        already = [
            dep for dep in (getattr(route.dependant, "dependencies", None) or ())
            if getattr(getattr(dep, "call", None), "__name__", "") == _dep.__name__
        ]
        if already:
            # Already mounted. Still report the real key source rather than an
            # empty list, so a second call's return value is as informative as
            # the first's -- otherwise an idempotent re-install silently blanks
            # what the caller was told the route is keyed on.
            guarded.setdefault(route.path, _key_source(route))
            continue
        route.dependant.dependencies.append(
            get_dependant(path=route.path_format, call=_dep)
        )
        guarded[route.path] = _key_source(route)
    return guarded


def _key_source(route: Any) -> List[str]:
    """Which param names this route is counted by. Reported, not enforced.

    Returned by the installer so a caller can see whether a route is keyed on a
    player, a session, or an address. It is descriptive only: the actual key is
    resolved at request time by :func:`_caller_key`, which cannot read the body.
    """
    for name in _PLAYER_PATH_PARAMS:
        if "{" + name + "}" in route.path:
            return [name]
    return ["session-or-address"]