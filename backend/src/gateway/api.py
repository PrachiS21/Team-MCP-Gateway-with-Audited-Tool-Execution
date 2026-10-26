"""The dashboard's HTTP API, under /api, in the same process as /mcp (so both share one registry, manager and audit log).

Login: POST /api/login with an API key starts a session; the browser gets an HttpOnly cookie, never the key.
Every request re-checks the session, so a revoked key or a deactivated user is logged out on the next request.

Cross-site request forgery (another website making the browser send our cookie) is blocked twice:
- the cookie is SameSite=Strict, so the browser does not send it on requests started by another site;
- POST and PATCH must carry a JSON body. A plain HTML form cannot send one, and a script on another site that tries
  needs a CORS "preflight" this API never approves. (Forms cannot send DELETE at all.)

Nothing secret ever leaves this module: server credentials appear only as has_credential; API keys only at the
moment they are created; session tokens only in the Set-Cookie header.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway.audit.service import AuditLog
from gateway.chat import ChatAgent, ChatInputError, LLMError
from gateway.auth.keys import parse_key
from gateway.auth.service import ROLES, SESSION_TTL, AuthService, User
from gateway.mcp.manager import UpstreamManager
from gateway.registry.registry import ServerRegistry, safe_url
from gateway.service import Caller, GatewayService

log = logging.getLogger("gateway")

COOKIE = "gw_session"
_SERVER_FIELDS = {"name", "slug", "url", "credential_type", "credential", "credential_header", "timeout", "enabled"}


class ApiError(Exception):
    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


@dataclass(frozen=True)
class Me:
    user: User
    key_id: str  # the API key this session was started with

    def caller(self, source: str = "api") -> Caller:
        return Caller.from_user(self.user, self.key_id, source)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


def _user_json(u: User) -> dict:
    return {"id": u.id, "email": u.email, "role": u.role, "active": u.active, "created_at": _iso(u.created_at)}


def _key_json(k) -> dict:  # KeyInfo: metadata only, never the key or its hash
    return {"key_id": k.key_id, "name": k.name, "created_at": _iso(k.created_at),
            "last_used_at": _iso(k.last_used_at), "revoked_at": _iso(k.revoked_at)}


def _from_value_error(exc: ValueError) -> ApiError:
    """The services raise ValueError subclasses with clear, safe messages; turn them into HTTP answers."""
    text = str(exc)
    if text.startswith(("unknown ", "no server", "no user")):
        return ApiError(404, "not_found", text)
    if "already" in text:
        return ApiError(409, "conflict", text)
    return ApiError(400, "invalid", text)


async def _json_body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise ApiError(415, "json_required", "send a JSON body with Content-Type: application/json")
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        raise ApiError(400, "invalid", "the body is not valid JSON") from None
    if not isinstance(body, dict):
        raise ApiError(400, "invalid", "the body must be a JSON object")
    return body


def register_api(mcp: FastMCP, *, auth: AuthService, registry: ServerRegistry, audit: AuditLog,
                 manager: UpstreamManager, service: GatewayService,
                 cookie_secure: bool = False, chat: ChatAgent | None = None) -> list[tuple[str, str, str]]:
    """Adds the /api routes to the server and returns them as (method, path, access); the tests walk that list."""
    routes: list[tuple[str, str, str]] = []

    def me_of(request: Request) -> Me | None:
        found = auth.session_user(request.cookies.get(COOKIE, ""))
        return Me(*found) if found else None

    def event(me: Me, action: str, server_slug: str | None = None, **details) -> None:
        """Record an admin action (never with a secret in it). The action already happened, so a failure is logged."""
        try:
            audit.record_event(actor=me.user.email, action=action, server_slug=server_slug, details=details)
        except Exception:  # noqa: BLE001
            log.exception("could not record admin event %s", action)

    def route(path: str, method: str, access: str = "user"):
        """access: "public" (no login), "user" (any logged-in user) or "admin"."""
        routes.append((method, path, access))

        def wrap(handler: Callable[..., Awaitable[Any]]):
            async def endpoint(request: Request) -> Response:
                try:
                    if method in ("POST", "PATCH"):
                        request.state.body = await _json_body(request)
                    me = None
                    if access != "public":
                        me = me_of(request)
                        if me is None:
                            raise ApiError(401, "unauthenticated", "log in first")
                        if access == "admin" and not me.user.is_admin:
                            raise ApiError(403, "forbidden", "only admins can do this")
                    result = await handler(request, me)
                except ApiError as e:
                    return JSONResponse({"error": e.kind, "message": e.message}, status_code=e.status)
                except ValueError as exc:
                    e = _from_value_error(exc)
                    return JSONResponse({"error": e.kind, "message": e.message}, status_code=e.status)
                return result if isinstance(result, Response) else JSONResponse(result)

            endpoint.__name__ = f"api_{method.lower()}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"
            mcp.custom_route(path, methods=[method])(endpoint)
            return handler
        return wrap

    async def apply_registry() -> None:
        """Make the live servers match the database now, instead of waiting for the background sync."""
        await manager.sync(registry.desired())

    # -- login ------------------------------------------------------------------------------------------------------

    @route("/api/login", "POST", "public")
    async def login(request, _me):
        key = request.state.body.get("api_key")
        started = auth.start_session(key) if isinstance(key, str) else None
        if started is None:
            try:  # the same trail as a rejected /mcp call; only the public key id, never the key
                audit.record(outcome="auth_failed", source="api",
                             key_id=parse_key(key) if isinstance(key, str) else None, error_kind="invalid_key")
            except Exception:  # noqa: BLE001
                log.exception("could not record failed login")
            raise ApiError(401, "invalid_key", "that API key is not valid (wrong, revoked, or its user is deactivated)")
        token, user = started
        resp = JSONResponse({"user": _user_json(user)})
        resp.set_cookie(COOKIE, token, max_age=int(SESSION_TTL.total_seconds()), path="/api", httponly=True,
                        samesite="strict", secure=cookie_secure)
        return resp

    @route("/api/logout", "POST")
    async def logout(request, _me):
        auth.end_session(request.cookies.get(COOKIE, ""))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/api", httponly=True, samesite="strict", secure=cookie_secure)
        return resp

    @route("/api/me", "GET")
    async def whoami(_request, me):
        return {"user": _user_json(me.user), "key_id": me.key_id}

    # -- servers ------------------------------------------------------------------------------------------------------

    @route("/api/servers", "GET")
    async def list_servers(_request, me):
        """Live: every enabled server is asked for its tools now (in parallel, each with its own timeout)."""
        catalogue = await service.list_tools(me.caller())
        states = {s.server_id: s for s in catalogue.servers}
        tools: dict[str, list] = {}
        for t in catalogue.tools:
            tools.setdefault(t.server_id, []).append({"name": t.name, "description": t.tool.description or ""})
        out = []
        for v in registry.list():
            state = states.get(v.id)
            item = {"id": v.id, "slug": v.slug, "name": v.name, "enabled": v.enabled,
                    "status": state.status.value if state else ("disabled" if not v.enabled else "unreachable"),
                    "error": {"kind": state.error.kind.value, "message": state.error.message}
                    if state and state.error and v.enabled else None,
                    "tools": tools.get(v.id, [])}
            if me.user.is_admin:  # connection details are for admins; members only need status and tools
                item.update(url=safe_url(v.url), credential_type=v.credential_type,
                            credential_header=v.credential_header, has_credential=v.has_credential,
                            timeout=v.timeout, created_at=_iso(v.created_at))
            out.append(item)
        return {"servers": out}

    def server_fields(body: dict) -> dict:
        unknown = set(body) - _SERVER_FIELDS
        if unknown:
            raise ApiError(400, "invalid", f"unknown field(s): {', '.join(sorted(unknown))}")
        if "timeout" in body:
            if not isinstance(body["timeout"], (int, float)) or not 0 < body["timeout"] <= 120:
                raise ApiError(400, "invalid", "timeout must be a number of seconds between 0 and 120")
        return body

    @route("/api/servers", "POST", "admin")
    async def create_server(request, me):
        b = server_fields(request.state.body)
        for required in ("slug", "url"):
            if not isinstance(b.get(required), str) or not b[required].strip():
                raise ApiError(400, "invalid", f"{required} is required")
        v = registry.create(name=b.get("name") or b["slug"], slug=b["slug"].strip(), url=b["url"].strip(),
                            credential_type=b.get("credential_type", "none"), credential=b.get("credential") or None,
                            credential_header=b.get("credential_header"), enabled=bool(b.get("enabled", True)),
                            timeout=float(b.get("timeout", 10.0)))
        await apply_registry()
        event(me, "server_created", v.slug, url=safe_url(v.url), credential_type=v.credential_type, enabled=v.enabled)
        return JSONResponse({"server": {"id": v.id, "slug": v.slug}}, status_code=201)

    @route("/api/servers/{server_id}", "PATCH", "admin")
    async def edit_server(request, me):
        b = server_fields(request.state.body)
        enabled = b.pop("enabled", None)
        old = registry.get(request.path_params["server_id"])
        changes = {k: b[k] for k in ("name", "slug", "url", "credential_type", "credential_header", "timeout") if k in b}
        if "credential" in b:  # absent = keep the stored secret; null or "" = remove it
            changes["credential"] = b["credential"] or None
        v = registry.update(old.id, **changes) if changes else old
        if enabled is not None and bool(enabled) != v.enabled:
            v = registry.set_enabled(v.id, bool(enabled))
            changes["enabled"] = v.enabled
        await apply_registry()
        if changes:
            event(me, "server_updated", v.slug, changed=sorted(changes))  # which fields, never the credential value
        return {"server": {"id": v.id, "slug": v.slug}}

    async def set_enabled(request, me, enabled: bool):
        v = registry.set_enabled(request.path_params["server_id"], enabled)
        await apply_registry()
        event(me, "server_enabled" if enabled else "server_disabled", v.slug)
        return {"server": {"id": v.id, "slug": v.slug, "enabled": v.enabled}}

    @route("/api/servers/{server_id}/enable", "POST", "admin")
    async def enable_server(request, me):
        return await set_enabled(request, me, True)

    @route("/api/servers/{server_id}/disable", "POST", "admin")
    async def disable_server(request, me):
        return await set_enabled(request, me, False)

    @route("/api/servers/{server_id}", "DELETE", "admin")
    async def remove_server(request, me):
        v = registry.get(request.path_params["server_id"])
        registry.delete(v.id)
        await apply_registry()
        event(me, "server_removed", v.slug)
        return {"ok": True}

    # -- users (admin) --------------------------------------------------------------------------------------------------

    @route("/api/users", "GET", "admin")
    async def list_users(_request, _me):
        return {"users": [{**_user_json(u), "keys": [_key_json(k) for k in auth.list_keys(u.id)]}
                          for u in auth.list_users()]}

    @route("/api/users", "POST", "admin")
    async def invite_user(request, me):
        """Invite = create the user and their first API key. The key is in this response only; hand it over safely."""
        b = request.state.body
        email, role = b.get("email"), b.get("role", "member")
        if not isinstance(email, str) or role not in ROLES:
            raise ApiError(400, "invalid", f"email is required and role must be one of {ROLES}")
        user = auth.create_user(email, role)
        issued = auth.issue_key(user.id, "first key")
        event(me, "user_created", email=user.email, role=user.role)
        event(me, "key_issued", email=user.email, key_id=issued.key_id, name=issued.name)
        return JSONResponse({"user": _user_json(user), "api_key": issued.key, "key_id": issued.key_id}, status_code=201)

    async def set_active(request, me, active: bool):
        user_id = request.path_params["user_id"]
        if not active and user_id == me.user.id:
            raise ApiError(400, "invalid", "you cannot deactivate yourself")
        user = auth.set_active(user_id, active)
        event(me, "user_activated" if active else "user_deactivated", email=user.email)
        return {"user": _user_json(user)}

    @route("/api/users/{user_id}/deactivate", "POST", "admin")
    async def deactivate_user(request, me):
        return await set_active(request, me, False)

    @route("/api/users/{user_id}/activate", "POST", "admin")
    async def activate_user(request, me):
        return await set_active(request, me, True)

    @route("/api/users/{user_id}/keys", "POST", "admin")
    async def issue_key_for_user(request, me):
        """An admin hands a member a new key, e.g. after they revoked the one they log in with and are locked out.
        The key is in this response only; hand it over safely."""
        name = request.state.body.get("name") or "issued by admin"
        if not isinstance(name, str) or len(name) > 120:
            raise ApiError(400, "invalid", "name must be a string of at most 120 characters")
        user = auth.get_user(request.path_params["user_id"])  # unknown id -> 404
        issued = auth.issue_key(user.id, name)  # a deactivated user -> 400: activate them first
        event(me, "key_issued", email=user.email, key_id=issued.key_id, name=name)
        return JSONResponse({"api_key": issued.key, "key_id": issued.key_id, "name": name}, status_code=201)

    # -- my keys --------------------------------------------------------------------------------------------------------

    @route("/api/keys", "GET")
    async def my_keys(_request, me):
        return {"keys": [_key_json(k) for k in auth.list_keys(me.user.id)]}

    @route("/api/keys", "POST")
    async def new_key(request, me):
        name = request.state.body.get("name") or "dashboard"
        if not isinstance(name, str) or len(name) > 120:
            raise ApiError(400, "invalid", "name must be a string of at most 120 characters")
        issued = auth.issue_key(me.user.id, name)
        event(me, "key_issued", email=me.user.email, key_id=issued.key_id, name=name)
        return JSONResponse({"api_key": issued.key, "key_id": issued.key_id, "name": name}, status_code=201)

    @route("/api/keys/{key_id}", "DELETE")
    async def revoke_key(request, me):
        """Your own keys, or anyone's if you are an admin. Someone else's key looks "unknown" to a member."""
        info = auth.get_key(request.path_params["key_id"])
        if info is None or (info.user_id != me.user.id and not me.user.is_admin):
            raise ApiError(404, "not_found", "unknown key")
        info = auth.revoke_key(info.key_id)
        event(me, "key_revoked", key_id=info.key_id, email=auth.get_user(info.user_id).email)
        return {"key": _key_json(info)}

    # -- audit ----------------------------------------------------------------------------------------------------------

    def int_param(request: Request, name: str, default: int) -> int:
        try:
            return int(request.query_params.get(name, default))
        except ValueError:
            raise ApiError(400, "invalid", f"{name} must be a whole number") from None

    @route("/api/audit", "GET")
    async def audit_log(request, me):
        """Admins see every call and can filter by user; a member sees only their own calls."""
        q = request.query_params
        if me.user.is_admin:
            user_id = None
            if q.get("user"):
                u = auth.get_user_by_email(q["user"])
                if u is None:
                    return {"entries": [], "total": 0}
                user_id = u.id
        else:
            user_id = me.user.id
        filters = {"user_id": user_id, "server_slug": q.get("server") or None, "outcome": q.get("outcome") or None}
        rows = audit.query(**filters, limit=int_param(request, "limit", 50), offset=int_param(request, "offset", 0))
        return {"total": audit.count(**filters),
                "entries": [{"id": r.id, "ts": _iso(r.ts), "user": r.user_email, "key_id": r.key_id,
                             "server": r.server_slug, "tool": r.tool, "duration_ms": r.duration_ms,
                             "outcome": r.outcome, "error_kind": r.error_kind, "source": r.source} for r in rows]}

    @route("/api/events", "GET", "admin")
    async def admin_events(request, _me):
        rows = audit.events(server_slug=request.query_params.get("server") or None,
                            limit=int_param(request, "limit", 50), offset=int_param(request, "offset", 0))
        return {"events": [{"id": e.id, "ts": _iso(e.ts), "actor": e.actor, "action": e.action,
                            "server": e.server_slug, "details": e.details} for e in rows]}

    # -- chat -----------------------------------------------------------------------------------------------------------

    @route("/api/chat", "POST")
    async def chat_turn(request, me):
        """One question. The agent acts as the logged-in user: its tool calls are audited under them (source "chat")."""
        if chat is None:
            raise ApiError(503, "chat_unavailable",
                           "The chat agent is not configured on this gateway (an admin must set the LLM model and key).")
        try:
            reply = await chat.run(me.caller("chat"), request.state.body.get("messages"))
        except ChatInputError as exc:
            raise ApiError(400, "invalid", str(exc)) from None
        except LLMError as exc:
            raise ApiError(502, "llm_error", str(exc)) from None
        return reply.as_json()

    return routes
