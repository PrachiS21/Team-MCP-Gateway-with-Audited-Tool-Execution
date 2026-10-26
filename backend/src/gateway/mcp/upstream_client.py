"""One connection to one upstream MCP server (Streamable HTTP).

Public methods never raise for upstream problems. They return a ToolListResult /
CallResult carrying a structured error, so a bad server cannot break callers
that talk to other servers.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.types import Tool


class Status(str, Enum):
    CONNECTED = "connected"
    UNREACHABLE = "unreachable"
    DISABLED = "disabled"


class ErrorKind(str, Enum):
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    AUTH = "auth_failed"
    UPSTREAM = "upstream_error"
    DISABLED = "disabled"
    UNKNOWN_TOOL = "unknown_tool"  # raised by the manager when no server owns the name


@dataclass(frozen=True)
class UpstreamError:
    kind: ErrorKind
    message: str


@dataclass
class ToolListResult:
    tools: list[Tool] = field(default_factory=list)
    error: UpstreamError | None = None


@dataclass
class CallResult:
    content: Any = None  # fastmcp CallToolResult on success
    is_error: bool = False  # tool ran and reported an error itself
    error: UpstreamError | None = None  # call never completed

    @property
    def ok(self) -> bool:
        return self.error is None and not self.is_error


@dataclass(frozen=True)
class UpstreamConfig:
    url: str
    credential: str | None = None  # secret; never exposed in any result
    credential_header: str = "Authorization"  # header carrying the credential
    credential_scheme: str | None = "Bearer"  # prefix, or None to send the raw value
    timeout: float = 10.0  # seconds, per list / call / connect


_CONNECT_ERROR_NAMES = {"ConnectError", "ConnectTimeout", "ConnectionRefusedError", "RemoteProtocolError", "ReadError"}
_CONNECTION_CLOSED = -32000  # JSON-RPC code the MCP SDK uses for "Connection closed"
_REQUEST_TIMEOUT = -32001  # ... and for "Request timed out" (its own per-request timeout, on a reused session)


def _classify(exc: BaseException) -> UpstreamError:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return UpstreamError(ErrorKind.TIMEOUT, "upstream did not answer in time")
    # Walk the cause chain / exception groups for an HTTP status.
    stack, seen = [exc], set()
    while stack:
        e = stack.pop()
        if id(e) in seen:  # cause/context chains can be cyclic
            continue
        seen.add(id(e))
        # Timeouts first: TimeoutError is a subclass of OSError, so the connection check below would also match it.
        if isinstance(e, (TimeoutError, asyncio.TimeoutError)) or getattr(e, "code", None) == _REQUEST_TIMEOUT:
            return UpstreamError(ErrorKind.TIMEOUT, "upstream did not answer in time")
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
            return UpstreamError(ErrorKind.AUTH, f"upstream rejected the credential (HTTP {e.response.status_code})")
        # FastMCP bundles its own "httpx2", so match connection errors by class name as well.
        if isinstance(e, (ConnectionError, OSError)) or type(e).__name__ in _CONNECT_ERROR_NAMES:
            return UpstreamError(ErrorKind.UNREACHABLE, "upstream is not reachable")
        if getattr(e, "code", None) == _CONNECTION_CLOSED:  # MCPError raised when a live session drops
            return UpstreamError(ErrorKind.UNREACHABLE, "upstream closed the connection")
        stack.extend(getattr(e, "exceptions", ()))
        if e.__cause__:
            stack.append(e.__cause__)
        if e.__context__:
            stack.append(e.__context__)
    return UpstreamError(ErrorKind.UPSTREAM, f"{type(exc).__name__}: {exc}")


class UpstreamClient:
    def __init__(self, config: UpstreamConfig, enabled: bool = True):
        self._config = config
        self._enabled = enabled
        self._client: Client | None = None
        self._lock = asyncio.Lock()  # serialises connect / reconnect
        self.status = Status.UNREACHABLE if enabled else Status.DISABLED  # unknown until first probe
        self.last_error: UpstreamError | None = None
        self.last_checked_at: float | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- connection -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        c = self._config
        if not c.credential:
            return {}
        value = f"{c.credential_scheme} {c.credential}" if c.credential_scheme else c.credential
        return {c.credential_header: value}

    def _build(self) -> Client:
        c = self._config
        return Client(StreamableHttpTransport(c.url, headers=self._headers()), timeout=c.timeout)

    async def _diagnose(self, exc: BaseException) -> UpstreamError:
        """Classify a failure. FastMCP hides the HTTP status of a rejected connect
        behind a generic MCPError, so for that case we ask the server once more
        with a plain HTTP request to see whether it answers 401/403."""
        err = _classify(exc)
        if err.kind is not ErrorKind.UPSTREAM:
            return err
        try:
            async with httpx.AsyncClient(timeout=min(self._config.timeout, 3.0)) as http:
                resp = await http.post(self._config.url, headers=self._headers(), json={})
            if resp.status_code in (401, 403):
                return UpstreamError(ErrorKind.AUTH, f"upstream rejected the credential (HTTP {resp.status_code})")
        except Exception:  # noqa: BLE001 - the probe is best-effort; keep the original error
            pass
        return err

    async def _ensure_connected(self) -> Client:
        async with self._lock:
            if self._client is not None and self._client.is_connected():
                return self._client
            await self._drop()
            client = self._build()
            async with asyncio.timeout(self._config.timeout):
                await client.__aenter__()
            self._client = client
            return client

    async def _drop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.__aexit__(None, None, None), 2)
            except BaseException:  # noqa: BLE001 - best-effort teardown of a dead session
                pass

    def _mark_ok(self) -> None:
        self.status, self.last_error, self.last_checked_at = Status.CONNECTED, None, time.time()

    def _mark_failed(self, err: UpstreamError) -> None:
        self.status, self.last_error, self.last_checked_at = Status.UNREACHABLE, err, time.time()

    # -- operations -------------------------------------------------------

    async def list_tools(self) -> ToolListResult:
        if not self._enabled:
            return ToolListResult(error=UpstreamError(ErrorKind.DISABLED, "server is disabled"))
        try:
            client = await self._ensure_connected()
            async with asyncio.timeout(self._config.timeout):
                tools = await client.list_tools()
        except Exception as exc:  # noqa: BLE001
            err = await self._diagnose(exc)
            await self._drop()
            self._mark_failed(err)
            return ToolListResult(error=err)
        self._mark_ok()
        return ToolListResult(tools=list(tools))

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallResult:
        if not self._enabled:
            return CallResult(error=UpstreamError(ErrorKind.DISABLED, "server is disabled"))
        try:
            client = await self._ensure_connected()
            async with asyncio.timeout(self._config.timeout):
                result = await client.call_tool(name, arguments or {}, raise_on_error=False)
        except Exception as exc:  # noqa: BLE001
            err = await self._diagnose(exc)
            await self._drop()
            self._mark_failed(err)
            return CallResult(error=err)
        self._mark_ok()
        return CallResult(content=result, is_error=bool(getattr(result, "is_error", False)))

    async def check(self) -> Status:
        """Health probe used by the manager's periodic loop."""
        if self._enabled:
            await self.list_tools()
        return self.status

    # -- lifecycle --------------------------------------------------------

    async def disable(self) -> None:
        self._enabled = False
        await self._drop()
        self.status, self.last_error = Status.DISABLED, None

    def enable(self) -> None:
        self._enabled = True
        self.status = Status.UNREACHABLE  # unknown until the next probe

    async def close(self) -> None:
        await self._drop()

    def describe(self) -> dict[str, Any]:
        """Safe-to-expose view: never includes the credential."""
        return {
            "url": self._config.url,
            "status": self.status.value,
            "has_credential": self._config.credential is not None,
            "last_error": self.last_error and {"kind": self.last_error.kind.value, "message": self.last_error.message},
            "last_checked_at": self.last_checked_at,
        }
