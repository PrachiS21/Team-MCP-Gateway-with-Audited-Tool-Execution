"""The gateway's MCP endpoint (and, from api.py, the dashboard's /api routes).

  client --Bearer <api key>--> GatewayKeyVerifier --> GatewayMiddleware --> GatewayService
                                (who is this?)        (list / call)         (audit, route, undo)

The MCP layer here is deliberately thin. Tools are not registered on the server; the
middleware answers list/call from GatewayService, because upstream servers come and go
at runtime. The client's key is only ever checked here; upstream credentials live in the
registry and are used only by UpstreamClient.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Mapping

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from fastmcp.tools import Tool, ToolResult
from mcp.types import TextContent
from starlette.responses import JSONResponse

from gateway.api import register_api
from gateway.chat import LLM, ChatAgent, OpenAICompatibleLLM
from gateway.audit.service import AuditLog
from gateway.auth.keys import parse_key
from gateway.auth.service import AuthService
from gateway.compensation import CompensationRegistry
from gateway.config import Settings
from gateway.incidents import IncidentLog
from gateway.jobs import UndoJobStore
from gateway.locks import KeyedLocks
from gateway.mcp.manager import UpstreamManager
from gateway.registry.crypto import SecretBox
from gateway.registry.registry import ServerRegistry
from gateway.seed import apply_seed
from gateway.service import Caller, GatewayResult, GatewayService
from gateway.woo_compensation import register_woo_compensations
from gateway.undo_worker import UndoWorker

log = logging.getLogger("gateway")


class GatewayKeyVerifier(TokenVerifier):
    """Turns the bearer token into a user, or rejects the request with a 401."""

    def __init__(self, auth: AuthService, audit: AuditLog):
        super().__init__()
        self._auth, self._audit = auth, audit

    async def verify_token(self, token: str) -> AccessToken | None:
        user = self._auth.verify(token)
        key_id = parse_key(token)  # public id only; None for malformed tokens
        if user is None:
            self._record_failure(key_id)
            return None
        return AccessToken(token=token, client_id=user.id, scopes=[],
                           claims={"user_id": user.id, "email": user.email, "role": user.role, "key_id": key_id})

    def _record_failure(self, key_id: str | None) -> None:
        try:  # never store the token itself, only its public id
            self._audit.record(outcome="auth_failed", key_id=key_id, error_kind="invalid_key")
        except Exception:  # noqa: BLE001 - a broken audit DB must not turn a 401 into a 500
            log.exception("could not record failed authentication")


class ProxyTool(Tool):
    """Exists only to be listed. Calls never reach run(): the middleware answers them."""

    async def run(self, arguments):  # pragma: no cover
        raise RuntimeError("calls are handled by GatewayMiddleware")


def _caller() -> Caller:
    token = get_access_token()
    if token is None:  # auth is mandatory, so this is a bug if it ever happens
        raise RuntimeError("no authenticated user on the request")
    c = token.claims
    return Caller(c["user_id"], c["email"], c["key_id"], source="mcp")


def _to_tool_result(res: GatewayResult) -> ToolResult:
    if res.error_kind is not None:  # the gateway or upstream failed: fixed, safe text only
        return ToolResult(content=[TextContent(type="text", text=res.message or "The call failed.")], is_error=True)
    upstream = res.content  # the tool ran (successfully, or reporting its own error)
    return ToolResult(content=upstream.content, structured_content=upstream.structured_content,
                      is_error=res.is_error)


class GatewayMiddleware(Middleware):
    def __init__(self, service: GatewayService):
        self._svc = service

    async def on_list_tools(self, context, call_next):
        catalogue = await self._svc.list_tools(_caller())
        return [ProxyTool(name=t.name, description=t.tool.description or "", parameters=t.tool.input_schema,
                          output_schema=t.tool.output_schema, annotations=t.tool.annotations)
                for t in catalogue.tools]

    async def on_call_tool(self, context, call_next):
        msg = context.message
        res = await self._svc.call_tool(_caller(), msg.name, msg.arguments or {})
        return _to_tool_result(res)


@dataclass
class Gateway:
    settings: Settings
    manager: UpstreamManager
    registry: ServerRegistry
    auth: AuthService
    audit: AuditLog
    service: GatewayService
    worker: UndoWorker
    mcp: FastMCP
    api_routes: list  # (method, path, access) of every /api route

    def http_app(self):
        return self.mcp.http_app(path="/mcp", stateless_http=True)


def create_gateway(settings: Settings, compensations: CompensationRegistry | None = None,
                   env: Mapping[str, str] | None = None, llm: LLM | None = None) -> Gateway:
    """`llm` replaces the configured LLM (tests pass a scripted fake)."""
    box = SecretBox(settings.secret_key)
    registry = ServerRegistry(settings.db_url, box, allow_loopback=settings.allow_loopback)
    auth = AuthService(settings.db_url)
    audit = AuditLog(settings.db_url)
    jobs = UndoJobStore(settings.jobs_db_url, box)
    incidents = IncidentLog(settings.incident_log)
    locks = KeyedLocks()
    if compensations is None:
        compensations = CompensationRegistry()
        register_woo_compensations(compensations, settings.woo_slug)

    started = {"seed_file": settings.seed_file is not None}
    if settings.seed_file is not None:  # raises SeedError on a bad file: the gateway then refuses to start
        report = apply_seed(settings.seed_file, os.environ if env is None else env, registry, audit, incidents)
        started.update(created=[c.slug for c in report.changes if c.action == "created"],
                       updated=[c.slug for c in report.changes if c.action == "updated"],
                       unchanged=report.unchanged)
    try:  # every start and restart is audited, whether or not anything changed
        audit.record_event(actor="system", action="gateway_started", details=started)
    except Exception:  # noqa: BLE001 - a broken audit store must not keep the gateway from starting
        log.exception("could not record the gateway_started event")

    manager = UpstreamManager()
    registry.load_into(manager)

    service = GatewayService(manager, audit, compensations=compensations, jobs=jobs, locks=locks, incidents=incidents)
    worker = UndoWorker(manager, jobs, audit, compensations, locks, incidents)

    async def sync_forever() -> None:
        # The registry (database) is the source of truth: pick up servers added, edited,
        # disabled or removed by another process (the CLI, another replica).
        while True:
            await asyncio.sleep(settings.sync_interval)
            try:
                await manager.sync(registry.desired())
            except Exception:  # noqa: BLE001 - one bad pass must not stop syncing
                log.exception("registry sync failed")

    @asynccontextmanager
    async def lifespan(_server):
        worker_task = asyncio.create_task(worker.run_forever(settings.worker_interval))
        sync_task = asyncio.create_task(sync_forever())
        try:
            yield
        finally:
            worker.stop()
            sync_task.cancel()
            await asyncio.gather(worker_task, sync_task, return_exceptions=True)
            await manager.close()

    mcp = FastMCP("gateway", auth=GatewayKeyVerifier(auth, audit), middleware=[GatewayMiddleware(service)],
                  lifespan=lifespan)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):  # liveness: the process is up
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_request):  # readiness: the database answers
        try:
            registry.list()
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "database unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    if llm is None and settings.llm_model and settings.llm_api_key:
        llm = OpenAICompatibleLLM(settings.llm_base_url, settings.llm_model, settings.llm_api_key, settings.llm_timeout)
    chat = ChatAgent(service, llm) if llm is not None else None  # None: /api/chat answers 503 "not configured"
    if chat is None:
        log.info("chat agent off: set GATEWAY_LLM_MODEL and GATEWAY_LLM_API_KEY to turn it on")

    api_routes = register_api(mcp, auth=auth, registry=registry, audit=audit, manager=manager, service=service,
                              cookie_secure=settings.cookie_secure, chat=chat)

    return Gateway(settings, manager, registry, auth, audit, service, worker, mcp, api_routes)
