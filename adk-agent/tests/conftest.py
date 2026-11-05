"""The agent tested end to end without the internet: ADK --HTTP--> a real gateway --HTTP--> a small upstream shop.

The gateway is the backend package, created with create_gateway() and served by uvicorn on a free local port, so the
agent goes through the same key check, routing and audit log as any client. Its seed registers the shop and creates
the agent's member user with a known key (seed "users", as on Cloud Run). The model is scripted: it plays back fixed
turns and keeps every request it was sent, so a test can see which tools it was offered.
"""

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest
import uvicorn
from fastmcp import FastMCP
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

from gateway.auth.keys import generate_key as generate_api_key
from gateway.config import Settings
from gateway.registry.crypto import generate_key as generate_secret_key
from gateway.server import create_gateway

AGENT_EMAIL = "adk-agent@example.com"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Served:
    """An ASGI app served by uvicorn on a free local port for the length of an `async with`."""

    def __init__(self, app):
        self.port = free_port()
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))

    async def __aenter__(self):
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                self._task.result()  # raises the reason it could not start
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc):
        self._server.should_exit = True
        await self._task


class Shop:
    """The upstream: one tool that only reads and one that changes something, marked the way woo-mcp marks them."""

    def __init__(self):
        self.calls: list[str] = []
        self.mcp = FastMCP("shop")

        def list_orders() -> str:
            """List the shop's open orders."""
            self.calls.append("list_orders")
            return "order 7: 2 mugs, processing"

        def delete_order(order_id: int) -> str:
            """Delete an order."""
            self.calls.append(f"delete_order {order_id}")
            return f"order {order_id} deleted"

        self.mcp.tool(list_orders, annotations={"readOnlyHint": True})
        self.mcp.tool(delete_order, annotations={"readOnlyHint": False, "destructiveHint": True})


class ScriptedLlm(BaseLlm):
    """Plays back `turns` in order, one per model call, and keeps every request it was sent."""

    model: str = "scripted"
    turns: list[LlmResponse] = Field(default_factory=list)
    requests: list = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream: bool = False):
        self.requests.append(llm_request)
        yield self.turns.pop(0)


def say(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


def call(tool: str, **args) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(name=tool, args=args))]))


@pytest.fixture
async def gateway(tmp_path):
    """A gateway in front of the shop, with the agent's member user seeded. Yields what a test needs to reach both."""
    shop = Shop()
    key = generate_api_key()[1]
    async with Served(shop.mcp.http_app(path="/mcp")) as upstream:
        seed = {"servers": [{"slug": "shop", "url": f"http://127.0.0.1:{upstream.port}/mcp"}],
                "users": [{"email": AGENT_EMAIL, "role": "member", "key_env": "AGENT_KEY"}]}
        settings = Settings(secret_key=generate_secret_key(), db_url=f"sqlite:///{tmp_path / 'gw.db'}",
                            jobs_db_url=f"sqlite:///{tmp_path / 'jobs.db'}", allow_loopback=True,
                            seed_json=json.dumps(seed), worker_interval=3600, sync_interval=3600)
        gw = create_gateway(settings, env={"AGENT_KEY": key})
        async with Served(gw.http_app()) as front:
            yield SimpleNamespace(mcp_url=f"http://127.0.0.1:{front.port}/mcp", key=key, gw=gw, shop=shop,
                                  user=gw.auth.get_user_by_email(AGENT_EMAIL))
