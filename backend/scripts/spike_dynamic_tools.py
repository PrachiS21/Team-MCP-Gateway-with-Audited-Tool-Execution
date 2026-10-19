# REFERENCE ONLY: throwaway spike proving FastMCP auth + dynamic-tool middleware work.
# Not imported by the app. Run from backend/:  uv run python scripts/spike_dynamic_tools.py
"""Spike: can FastMCP serve a dynamic tool list behind a custom API-key verifier?"""
import asyncio, socket, sys
sys.path.insert(0, "src"); sys.path.insert(0, ".")

import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from fastmcp.tools import Tool, ToolResult
from mcp.types import TextContent

from gateway.mcp.manager import UpstreamManager
from gateway.mcp.upstream_client import UpstreamConfig
from tests.fixtures.fake_upstream import FakeUpstream

KEYS = {"good-key": "alice"}  # stand-in for the users/api_keys tables


class KeyVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        user = KEYS.get(token)
        if user is None:
            return None
        return AccessToken(token=token, client_id=user, scopes=[], claims={"user": user})


class ProxyTool(Tool):
    """A tool that exists only to be listed; calls are answered by middleware."""
    async def run(self, arguments):  # never reached
        raise RuntimeError("handled by middleware")


class Gateway(Middleware):
    def __init__(self, manager):
        self.manager = manager

    async def on_list_tools(self, context, call_next):
        cat = await self.manager.list_all_tools()
        who = get_access_token().claims["user"]
        print(f"  [middleware] list_tools for user={who}: {[t.name for t in cat.tools]}")
        return [ProxyTool(name=t.name, description=t.tool.description or "",
                          parameters=t.tool.input_schema) for t in cat.tools]

    async def on_call_tool(self, context, call_next):
        who = get_access_token().claims["user"]
        name, args = context.message.name, context.message.arguments or {}
        print(f"  [middleware] call_tool {name} args={args} user={who}")
        res = await self.manager.call_tool(name, args)
        if res.error:
            return ToolResult(content=[TextContent(type="text", text=res.error.message)], is_error=True)
        return ToolResult(content=res.content.content, structured_content=res.content.structured_content)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


async def main():
    fake = FakeUpstream("fake", tools=("echo",)); await fake.start()
    manager = UpstreamManager()
    manager.add("1", "fake", UpstreamConfig(fake.url, timeout=2))

    mcp = FastMCP("gateway", auth=KeyVerifier(), middleware=[Gateway(manager)])
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(mcp.http_app(stateless_http=True), host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    while not server.started: await asyncio.sleep(0.05)
    url = f"http://127.0.0.1:{port}/mcp"

    print("1) good key: list + call")
    async with Client(url, auth="good-key") as c:
        print("  client sees:", [t.name for t in await c.list_tools()])
        r = await c.call_tool("fake__echo", {"text": "hi"})
        print("  result:", r.data, "| is_error:", r.is_error)
        r = await c.call_tool("nope__x", {}, raise_on_error=False)
        print("  unknown tool ->", r.is_error, [b.text for b in r.content])

    print("2) bad key")
    try:
        async with Client(url, auth="wrong") as c:
            await c.list_tools()
        print("  !! bad key was ACCEPTED")
    except Exception as e:
        print("  rejected:", type(e).__name__, str(e)[:100])

    print("3) no key")
    try:
        async with Client(url) as c:
            await c.list_tools()
        print("  !! no key was ACCEPTED")
    except Exception as e:
        print("  rejected:", type(e).__name__, str(e)[:100])

    server.should_exit = True; await task
    await manager.close(); await fake.stop()

asyncio.run(main())
