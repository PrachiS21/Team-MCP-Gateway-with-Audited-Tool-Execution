"""Controllable fake MCP upstream served over Streamable HTTP.

Knobs: tool names (collisions), per-request latency (slow), required bearer
token (auth), and stop()/start() (dead / recovered). Port is kept across
restarts so a registered URL stays valid.
"""

import asyncio
import socket

import uvicorn
from fastmcp import FastMCP


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeUpstream:
    def __init__(
        self,
        name: str,
        tools: tuple[str, ...] = ("echo",),
        token: str | None = None,
        latency: float = 0.0,
    ):
        self.name = name
        self.token = token
        self.latency = latency  # mutable at runtime
        self.port = _free_port()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

        self.mcp = FastMCP(name)
        for tool_name in tools:
            self._register(tool_name)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def _register(self, tool_name: str) -> None:
        upstream_name = self.name

        def handler(text: str = "") -> str:
            return f"{upstream_name}:{tool_name}:{text}"

        self.mcp.tool(handler, name=tool_name, description=f"{tool_name} from {upstream_name}")

    def _app(self):
        inner = self.mcp.http_app()
        fake = self

        async def app(scope, receive, send):
            if scope["type"] == "http":
                if fake.latency:
                    await asyncio.sleep(fake.latency)
                if fake.token is not None:
                    headers = dict(scope["headers"])
                    if headers.get(b"authorization", b"").decode() != f"Bearer {fake.token}":
                        await send({"type": "http.response.start", "status": 401,
                                    "headers": [(b"content-type", b"text/plain")]})
                        await send({"type": "http.response.body", "body": b"unauthorized"})
                        return
            await inner(scope, receive, send)

        return app, inner

    async def start(self) -> None:
        app, inner = self._app()
        config = uvicorn.Config(inner, host="127.0.0.1", port=self.port, log_level="error")
        # Wrap: serve `app` but let uvicorn drive inner's lifespan.
        config.app = _Combined(app, inner)
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.should_exit = True
        await self._task
        self._server = self._task = None


class _Combined:
    """Route http through the filtering app, lifespan through the inner app."""

    def __init__(self, http_app, inner):
        self._http, self._inner = http_app, inner

    async def __call__(self, scope, receive, send):
        target = self._inner if scope["type"] == "lifespan" else self._http
        await target(scope, receive, send)
