"""
Wayfarer's Cycle - Player identity and authorization as reusable dependencies.

This module is the single place where "who is calling" is decided. It exists
because the server had none: every route took ``player_id`` as a bare query or
path parameter, so any caller could read or mutate any other player's row by
guessing an id, and the ``/admin/*`` surface was reachable by anyone.

Three dependencies are exported:

``require_player``
    Resolves ``Authorization: Bearer <token>`` to a :class:`Principal`.
    401 when the header is absent, the token is unknown, or it has expired.

``require_admin``
    Resolves the ``X-Admin-Token`` header against the operator token in
    ``WC_ADMIN_TOKEN``. 503 when the server has no token configured, 401 when
    the presented token is absent or wrong.

``assert_owns`` / ``require_owned_path_param`` / ``require_owned_body_field``
    Ownership: the token's player must be the player being addressed. 403 on
    a mismatch. Correct and working, but NOT the live cross-player check
    -- see "Ownership is already mounted app-wide" below before wiring one
    of these into a route.

Ownership is already mounted app-wide
-----------------------------------
These three ownership helpers have no caller in ``server/`` outside this file
and its own tests. That is not dead code and should not be wired into routes
blindly; it is deliberate, and the mounted alternative is measured:

* ``server/ownership_guard.py`` installs ``auth.require_player`` on EVERY route
  whose shape is a bare ``player_id``/``pid`` with no auth of its own, from the
  resolved dependant tree of the live app (``main.py`` calls
  ``install_ownership_guard(app)``). It is a ratchet, not a route list: a route
  added tomorrow is covered without anyone editing a guard.
* The same module adds a claim-normalising dependency for the routes that name
  the parameter ``pid``, because ``auth.OWNERSHIP_FIELD`` is the literal
  ``"player_id"`` and a plain session check cannot see a ``pid`` claim.
  ``assert_owns`` does NOT do this, so hand-wiring it onto a ``pid`` route
  trades a claim that is compared for one that is not.

Measured on this tree (probe, both arms on one app): a route declaring
``require_owned_path_param("player_id")`` is reported by the installer as
``already-has-auth-dependency`` and left OUT of the guarded set, while a bare
``{player_id}`` route with no auth IS guarded. So adding one of these helpers
to a route is not free belt-and-braces -- it makes the app-wide ratchet skip
that route, replacing a guard that also normalises the claim with one that only
compares the one field the author remembered to name. The hand-wired form is
not WRONG (measured: cross-player -> 403, own-player -> 200), it is narrower.

Prefer the mounted guard. Reach for these helpers only when a route cannot be
matched by shape -- e.g. it names no player id at all, or takes the target from
a field ``enforce_ownership`` does not read. That is a deliberate per-route
opt-in, so a zero-caller count is the expected steady state, not a defect.

Design notes
------------
**Fail-closed by construction.** ``require_admin`` returns 503 when
``WC_ADMIN_TOKEN`` is unset rather than falling through to "allow", which is
the failure mode the existing ``_require_internal_mint_token`` in ``server/main.py``
already documents and rejects. A deployment that forgets to configure the token
is visibly broken, not silently open.

**The session table is injected, never imported.** ``sessions_db`` is a module
global in ``server/main.py``, and the modules that consume these dependencies
(``event_routes.py``, ``sect_wall.py``) deliberately never import ``main``.
So the table arrives through :func:`install_identity`, and this module depends
on a callable rather than on the app's internals. The only write site stays
where it was.

**Importing this module configures nothing.** Until :func:`install_identity`
runs, every dependency raises 503. A route wired to ``require_admin`` before the
hook is installed is closed, not open - the safe direction to fail in.

**Config is read per request, never latched at import.** ``os.environ`` is read
inside the dependency body, so a test that sets the env var after import sees
the change, and so does a process whose configuration is reloaded. Latching it
at module scope is the classic way a guard passes in tests and fails in
production, or the reverse.
"""
from __future__ import annotations

import hmac
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException

# Operator credential. Named for the role, matching the existing
# WC_INTERNAL_EARN_TOKEN convention in server/main.py.
ADMIN_TOKEN_ENV = "WC_ADMIN_TOKEN"
ADMIN_TOKEN_HEADER = "X-Admin-Token"

# auto_error=False on both schemes: FastAPI's own error is a 403 with a
# WWW-Authenticate challenge, and we need to distinguish "you sent nothing"
# from "your token is wrong" by status code (401 vs 503) without FastAPI
# raising before our own logic runs.
bearer_scheme = HTTPBearer(auto_error=False, description="Player session token")
admin_scheme = APIKeyHeader(name=ADMIN_TOKEN_HEADER, auto_error=False,
                            description="Operator token for /admin/* routes")

_UNAUTHENTICATED_HEADERS = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class Principal:
    """The authenticated caller.

    ``player_id`` is None for an operator: an admin token is not a player
    session, and conflating the two would let an operator principal satisfy an
    ownership check for an arbitrary player.
    """
    player_id: Optional[str]
    is_admin: bool
    token: Optional[str] = None

    def require_player_id(self) -> str:
        if self.player_id is None:
            raise HTTPException(status_code=403, detail="player identity required")
        return self.player_id


def _unauthenticated(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail,
                         headers=_UNAUTHENTICATED_HEADERS)


def _constant_time_equals(left: Any, right: Any) -> bool:
    """Timing-safe string equality that survives non-ASCII input.

    ``hmac.compare_digest`` raises TypeError when either str holds a
    character outside ASCII. Both the ``X-Admin-Token`` header and the
    ``player_id`` path/body value are caller-controlled, so an accented
    or CJK id turned the intended 401/403 into a 500 server error - a
    guard that crashes on garbage instead of refusing it. Encoding to
    UTF-8 bytes first keeps the constant-time property the guard exists
    for while refusing every value instead of raising on some.
    """
    return hmac.compare_digest(
        str(left).encode("utf-8"), str(right).encode("utf-8")
    )


def _parse_expiry(value: Any) -> Optional[float]:
    """Normalize a stored ``expires`` value to a POSIX timestamp.

    ``generate_session_token`` in server/main.py writes an ISO-8601 string from
    a naive ``datetime.now()``; tests and future persistence may write a number.
    Returns None when the value is absent or unparseable, which the caller
    treats as "no expiry recorded" rather than "expired" - a record we cannot
    date is not evidence that it lapsed.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


class IdentityProvider:
    """Holds the session table and answers "who is calling".

    Constructed by :func:`install_identity`. The session mapping is read through
    a callable on every request so that a test can swap the table, and so that a
    later move to a real session store changes this class only.
    """

    def __init__(self, sessions_accessor: Callable[[], Mapping[str, Any]]):
        self._sessions_accessor = sessions_accessor

    # -- session lookup ---------------------------------------------------

    def _lookup(self, token: str) -> Optional[Mapping[str, Any]]:
        if not token:
            return None
        try:
            sessions = self._sessions_accessor()
        except StarletteHTTPException:
            # SITE: this bare `except Exception` used to swallow EVERY
            # fault, including the deliberate 503 the accessor itself
            # raises when the session table is missing or wrongly typed
            # (install_from_main, identity.py:395), and re-answer it as a
            # fresh 503 that names neither the wiring fault nor the cause.
            # A stated fault keeps its own status; a REAL store failure
            # still reaches the handler below and is still answered 503.
            raise
        except Exception:
            # A broken session store must not read as "no such token" with a
            # 404-shaped answer; it is a server fault and the caller is owed a
            # 503 so a retry is a sensible response.
            raise HTTPException(status_code=503, detail="session store unavailable")
        if not sessions:
            return None
        try:
            record = sessions.get(token)
        except StarletteHTTPException:
            # SITE: same laundering as the accessor read above -- a
            # deliberate HTTPException from the code under this block
            # was rewritten as 503.
            raise
        except Exception:
            raise HTTPException(status_code=503, detail="session store unavailable")
        return record if isinstance(record, Mapping) else None

    # -- dependencies -----------------------------------------------------

    def require_player(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    ) -> Principal:
        """Authenticate a player session. 401 unless the bearer token is live."""
        if credentials is None or not getattr(credentials, "credentials", None):
            raise _unauthenticated("missing bearer token")
        token = credentials.credentials
        record = self._lookup(token)
        if record is None:
            raise _unauthenticated("invalid session token")
        expires_at = _parse_expiry(record.get("expires"))
        if expires_at is not None and expires_at <= time.time():
            raise _unauthenticated("session token expired")
        player_id = record.get("player_id")
        if not isinstance(player_id, str) or not player_id:
            # A token that resolves to nobody is not a valid identity, and
            # letting it through would hand an empty principal to an ownership
            # check that then compares "" against every player_id.
            raise _unauthenticated("invalid session token")
        return Principal(player_id=player_id, is_admin=False, token=token)

    def require_admin(
        self,
        presented: Optional[str] = Depends(admin_scheme),
    ) -> Principal:
        """Authenticate an operator. 503 unconfigured, 401 when wrong.

        Compares through ``_constant_time_equals`` (a timing-safe
        compare) so a mismatch cannot be recovered
        from response timing, matching _require_internal_mint_token,
        request_signing.py and save_encryption.py. Delegates to the
        module-level _check_admin so there is exactly one implementation.
        """
        return _check_admin(presented)

    # -- ownership --------------------------------------------------------

    def assert_owns(self, principal: Principal, target_player_id: str) -> str:
        """Enforce that ``principal`` addresses ``target_player_id`` itself.

        Returns the target id so a route can use it inline. Raises 403 on a
        mismatch. An operator principal has no player identity and therefore
        owns no player row; operator routes are separate by construction.

        Before wiring this (or either factory) into a route, read the module
        docstring section "Ownership is already mounted app-wide". Declaring
        it makes ``ownership_guard.install_ownership_guard`` SKIP that route,
        so it replaces the app-wide guard rather than adding to it -- and it
        cannot normalise a ``pid`` claim the way that guard does.
        """
        if principal.player_id is None:
            raise HTTPException(
                status_code=403, detail="player identity required for this resource")
        if not isinstance(target_player_id, str) or not target_player_id:
            raise HTTPException(status_code=400, detail="player_id required")
        if not _constant_time_equals(principal.player_id, target_player_id):
            raise HTTPException(status_code=403, detail="not your player")
        return target_player_id

    def require_owned_path_param(self, param_name: str) -> Callable[..., str]:
        """Build a dependency that owns the player named by a PATH parameter.

        ``Depends(require_owned_path_param("player_id"))`` reads the value out
        of ``request.path_params`` rather than redeclaring it, so a route does
        not have to declare the same parameter twice and cannot drift between
        the ownership check and the handler body.
        """

        def _dependency(
            request: Request,
            principal: Principal = Depends(self.require_player),
        ) -> str:
            target = request.path_params.get(param_name)
            if target is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"ownership dependency expected path parameter {param_name!r}",
                )
            return self.assert_owns(principal, target)

        return _dependency

    def require_owned_body_field(self, field_name: str) -> Callable[..., str]:
        """Build a dependency that owns the player named by a BODY field.

        The body is read once by FastAPI and cached on the request, so this does
        not consume the stream; the handler still receives its own parsed model.
        """

        async def _dependency(
            request: Request,
            principal: Principal = Depends(self.require_player),
        ) -> str:
            try:
                payload = await request.json()
            except StarletteHTTPException:
                # SITE: this bare `except Exception` swallowed EVERY fault under it,
                # including a deliberate HTTPException raised by the code below, and
                # re-answered it as 400 -- telling the caller the wrong thing about
                # whose fault it was. A stated fault keeps its own status; a REAL fault
                # still reaches the handler below and is still answered 400.
                raise
            except Exception:
                raise HTTPException(status_code=400, detail="request body must be JSON")
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="request body must be a JSON object")
            target = payload.get(field_name)
            if not isinstance(target, str) or not target:
                raise HTTPException(status_code=400, detail=f"{field_name} required")
            return self.assert_owns(principal, target)

        return _dependency


# --- module-level installation -------------------------------------------
#
# Route modules import the bare dependency callables, so the installed provider
# has to be reachable without threading an object through every register_*(app)
# signature. A single module-level provider keeps those edits to one import and
# one Depends(...) per route.

_PROVIDER: Optional[IdentityProvider] = None


def _auth_module():
    """Resolve the sibling ``auth`` module, whichever way this file was imported.

    ``server/identity.py`` is imported both bare (``import identity``, with
    ``server/`` on sys.path, which is what main.py and every route module do)
    and as ``server.identity`` from the repo-root test suites. Those are two
    distinct module objects, so a bare ``import auth`` succeeds in the first
    case and raises ImportError in the second. Try the sibling path first,
    then the package-qualified name, and fail LOUDLY if neither resolves -
    a silent fallback to a private copy of the check here is exactly the
    duplication this delegation exists to remove.
    """
    try:
        import auth as _auth
    except ImportError:
        try:
            from server import auth as _auth  # type: ignore[no-redef]
        except ImportError as exc:  # pragma: no cover - broken tree, not config
            raise ImportError(
                "server/identity.py cannot import server/auth.py; the operator "
                "guard has one implementation and it lives there"
            ) from exc
    return _auth


def _check_admin(presented: Optional[str]) -> Principal:
    """Delegate the operator check to ``auth.require_admin``.

    There is exactly ONE implementation of "is this caller an operator":
    ``server/auth.py::require_admin``. This function used to carry a second,
    byte-identical copy of the same rules. Two copies of a security guard are
    one copy and one future edit: whichever is patched later leaves the other
    answering the old question, and nothing in the tests compares them across
    modules. So this no longer decides anything - it forwards, and adapts the
    returned principal into this module's own ``Principal`` so callers
    annotated against ``identity.Principal`` (event_routes, sect_wall,
    mint_authority, leaderboard_settlement, main) keep receiving the class
    they declared.

    Behaviour is unchanged and stays fail-closed: 503 when the server has no
    ``WC_ADMIN_TOKEN`` configured, 401 when the caller presented nothing or
    the wrong thing. Both are raised by auth.py; this wrapper adds no
    allowance of its own. ``admin_scheme`` stays defined here because FastAPI
    reads the ``Depends(admin_scheme)`` default at decoration time, before any
    of this runs, and because the OpenAPI document should name one scheme.
    """
    admin = _auth_module().require_admin(presented)
    return Principal(
        player_id=admin.player_id, is_admin=admin.is_admin, token=admin.token,
    )


def _provider() -> IdentityProvider:
    if _PROVIDER is None:
        # Never "succeed" because the hook was forgotten: a guard that silently
        # passes when uninstalled is worse than no guard, because it reads as
        # protection in the route table and in review.
        raise HTTPException(
            status_code=503, detail="identity provider not installed")
    return _PROVIDER


def install_identity(
    app: Any,
    sessions_accessor: Callable[[], Mapping[str, Any]],
) -> IdentityProvider:
    """Install the identity provider and return it.

    Idempotent per app object: ``register_*`` helpers may be called on every
    startup, and re-installing must not leave a provider bound to a stale
    session table. Re-installation refreshes the accessor on the existing
    provider rather than replacing it, so dependencies already captured by
    registered routes keep working against the current table.
    """
    global _PROVIDER
    provider = _PROVIDER
    if provider is None:
        provider = IdentityProvider(sessions_accessor)
        _PROVIDER = provider
    else:
        provider._sessions_accessor = sessions_accessor
    app.openapi_schema = None
    return provider


def install_from_main(app: Any) -> IdentityProvider:
    """Install against ``main.sessions_db`` WITHOUT editing main.py.

    This is the registration hook for a server whose main.py is owned by
    another agent. It binds the session table LAZILY - the import happens on
    the first authenticated request, not at install time - for two reasons.
    First, importing main.py from a module main.py itself imports (via
    event_routes and sect_wall) is a circular import that only happens to
    resolve because it is deferred. Second, and more importantly, the accessor
    is re-evaluated per request, so it always reads the CURRENT sessions_db
    object rather than whichever dict existed when the hook ran. main.py
    rebinds that name in tests that swap the session table.

    Sessions stay in-process and vanish on restart, exactly as they do today:
    this hook reads the table, it does not add a second store and does not
    pretend the table is durable.
    """
    import importlib

    def _sessions_accessor() -> Mapping[str, Any]:
        main_module = importlib.import_module("main")
        table = getattr(main_module, "sessions_db", None)
        if not isinstance(table, Mapping):
            # Not "empty table, therefore nobody is logged in". A missing or
            # wrongly-typed table is a wiring fault and the caller is owed a
            # retryable 503 rather than a 401 that reads as bad credentials.
            raise HTTPException(status_code=503,
                                detail="session store unavailable")
        return table

    return install_identity(app, _sessions_accessor)


def get_provider() -> IdentityProvider:
    """The installed provider, for tests and for routes that need the object."""
    return _provider()


def require_player(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Principal:
    """Module-level ``require_player``. See IdentityProvider.require_player."""
    return _provider().require_player(credentials)


def require_admin(presented: Optional[str] = Depends(admin_scheme)) -> Principal:
    """Module-level operator guard.

    Deliberately NOT routed through the installed provider. Operator auth
    consults only the environment, so it must work the instant the module is
    imported - with no install hook, no session table, and no main.py edit.
    Coupling it to the provider would have made the guard return 503
    ("not installed") on a server where the admin token is perfectly well
    configured, i.e. it would have failed open in every deployment that
    deployed the routes before the wiring, and 503 is a retryable answer a
    load balancer or a client will keep hammering.
    """
    return _check_admin(presented)


def assert_owns(principal: Principal, target_player_id: str) -> str:
    """Module-level ownership check. See IdentityProvider.assert_owns."""
    return _provider().assert_owns(principal, target_player_id)


def require_owned_path_param(param_name: str) -> Callable[..., str]:
    """Module-level path-param ownership dependency.

    Resolves the provider at REQUEST time, never at decoration time.
    These two factories are called from route modules inside their
    decorators, and a decorator runs at import - long before any
    startup hook, lifespan or ``install_identity`` call can have run.
    Resolving eagerly here raised the "provider not installed" 503 from
    inside the decorator, so importing a route module that owned a
    player took the whole application down at import instead of leaving
    that one route closed per-request.
    """

    def _dependency(
        request: Request,
        principal: Principal = Depends(require_player),
    ) -> str:
        provider = _provider()
        return provider.require_owned_path_param(param_name)(
            request=request, principal=principal)

    return _dependency


def require_owned_body_field(field_name: str) -> Callable[..., str]:
    """Module-level body-field ownership dependency.

    Defers provider resolution to request time for the same reason as
    :func:`require_owned_path_param`: the factory runs inside a route
    decorator, which is import time.
    """

    async def _dependency(
        request: Request,
        principal: Principal = Depends(require_player),
    ) -> str:
        provider = _provider()
        return await provider.require_owned_body_field(field_name)(
            request=request, principal=principal)

    return _dependency


def mint_session_entry(player_id: str, now: Optional[datetime] = None,
                       lifetime_days: int = 30) -> tuple[str, dict]:
    """Build a ``(token, record)`` pair shaped exactly like ``sessions_db``.

    ``server/main.py::generate_session_token`` is the live write site and still
    owns minting. This helper exists so the format is defined once: the reader
    in this module and the writer in main.py must agree on the keys
    (``player_id``/``created``/``expires``) or a correctly minted token reads as
    invalid. main.py should delegate to it - tracked as a follow-up rather than
    done here, because main.py is owned elsewhere.
    """
    import secrets
    from datetime import timedelta

    issued = now or datetime.now()
    token = secrets.token_urlsafe(32)
    record = {
        "player_id": player_id,
        "created": issued.isoformat(),
        "expires": (issued + timedelta(days=lifetime_days)).isoformat(),
    }
    return token, record
