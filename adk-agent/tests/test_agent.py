"""The agent's promises, checked through a real gateway (see conftest.py): it is offered exactly the gateway's tools;
a read runs at once and is audited under the agent's own user; a change waits for a yes and never runs without one;
revoking its key in the gateway stops it; and its settings come from the environment without ever echoing the key."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.adk.cli.utils.agent_loader import AgentLoader
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import InMemoryRunner
from google.genai import types

from conftest import AGENT_EMAIL, ScriptedLlm, call, say
from gateway.auth.keys import generate_key as generate_api_key, parse_key
from gateway_agent.factory import agent_from_env, build_agent, is_read_only

ADK_AGENT_DIR = Path(__file__).resolve().parents[1]


class Conversation:
    """One ADK session with the agent, as `adk web` holds it: messages in, events out."""

    def __init__(self, agent):
        self.runner = InMemoryRunner(agent=agent, app_name="test")
        self.session_id = None

    async def send(self, message: types.Content) -> list:
        if self.session_id is None:
            session = await self.runner.session_service.create_session(app_name="test", user_id="prachi")
            self.session_id = session.id
        return [e async for e in self.runner.run_async(user_id="prachi", session_id=self.session_id,
                                                       new_message=message)]


@pytest.fixture
async def talk():
    started = []

    def start(agent) -> Conversation:
        started.append(Conversation(agent))
        return started[-1]

    yield start
    for conversation in started:
        await conversation.runner.close()  # closes the MCP sessions too


def user(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def answer_confirmation(request_id: str, yes: bool) -> types.Content:
    """What `adk web` sends when the person clicks yes or no on a tool waiting for confirmation."""
    return types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        name="adk_request_confirmation", id=request_id, response={"confirmed": yes}))])


def parts(events, kind):
    return [getattr(p, kind) for e in events if e.content for p in e.content.parts or [] if getattr(p, kind)]


def texts(events) -> list[str]:
    return parts(events, "text")


# -- what the model is offered ---------------------------------------------------------------------------------

async def test_the_model_is_offered_exactly_the_gateways_tools(gateway, talk):
    model = ScriptedLlm(turns=[say("Hello.")])
    events = await talk(build_agent(gateway.mcp_url, gateway.key, model)).send(user("hi"))
    assert set(model.requests[0].tools_dict) == {"shop__list_orders", "shop__delete_order"}  # namespaced by the gateway
    assert texts(events) == ["Hello."] and gateway.shop.calls == []


@pytest.mark.parametrize("raw, read_only", [
    (SimpleNamespace(annotations=SimpleNamespace(read_only_hint=True)), True),
    (SimpleNamespace(annotations=SimpleNamespace(read_only_hint=False)), False),
    (SimpleNamespace(annotations=None), False),  # says nothing: treated as a change, so it needs a yes
    (None, False),
])
def test_only_a_tool_marked_read_only_runs_without_confirmation(raw, read_only):
    assert is_read_only(SimpleNamespace(raw_mcp_tool=raw)) is read_only


# -- reads, changes and the audit log --------------------------------------------------------------------------

async def test_a_read_runs_at_once_and_is_audited_under_the_agents_own_user(gateway, talk):
    model = ScriptedLlm(turns=[call("shop__list_orders"), say("You have one open order.")])
    events = await talk(build_agent(gateway.mcp_url, gateway.key, model)).send(user("which orders are open?"))
    assert gateway.shop.calls == ["list_orders"]
    [result] = parts(events, "function_response")
    assert "order 7" in json.dumps(result.response)
    assert texts(events)[-1] == "You have one open order."
    [row] = gateway.gw.audit.query(tool="list_orders")
    assert (row.user_id, row.user_email, row.key_id) == (gateway.user.id, AGENT_EMAIL, parse_key(gateway.key))
    assert (row.server_slug, row.outcome, row.source) == ("shop", "success", "mcp")


async def test_a_change_waits_for_a_yes_and_runs_only_after_it(gateway, talk):
    model = ScriptedLlm(turns=[call("shop__delete_order", order_id=7), say("Order 7 is deleted.")])
    chat = talk(build_agent(gateway.mcp_url, gateway.key, model))
    events = await chat.send(user("delete order 7"))
    [request] = [c for c in parts(events, "function_call") if c.name == "adk_request_confirmation"]
    assert request.args["originalFunctionCall"]["name"] == "shop__delete_order"
    assert gateway.shop.calls == [] and gateway.gw.audit.query(tool="delete_order") == []  # nothing ran yet

    events = await chat.send(answer_confirmation(request.id, yes=True))
    assert gateway.shop.calls == ["delete_order 7"]
    [row] = gateway.gw.audit.query(tool="delete_order")
    assert row.user_id == gateway.user.id and row.outcome == "success"
    assert texts(events)[-1] == "Order 7 is deleted."


async def test_a_change_that_is_turned_down_never_reaches_the_shop(gateway, talk):
    model = ScriptedLlm(turns=[call("shop__delete_order", order_id=7), say("Okay, order 7 stays.")])
    chat = talk(build_agent(gateway.mcp_url, gateway.key, model))
    [request] = [c for c in parts(await chat.send(user("delete order 7")), "function_call")
                 if c.name == "adk_request_confirmation"]
    events = await chat.send(answer_confirmation(request.id, yes=False))
    assert gateway.shop.calls == [] and gateway.gw.audit.query(tool="delete_order") == []
    [result] = [r for r in parts(events, "function_response") if r.name == "shop__delete_order"]
    assert result.response == {"error": "This tool call is rejected."}


async def test_revoking_the_key_in_the_gateway_stops_the_agent_at_once(gateway, talk, caplog):
    model = ScriptedLlm(turns=[call("shop__list_orders"), say("One open order."), say("I cannot reach the tools.")])
    chat = talk(build_agent(gateway.mcp_url, gateway.key, model))
    await chat.send(user("which orders are open?"))
    assert gateway.shop.calls == ["list_orders"]

    gateway.gw.auth.revoke_key(parse_key(gateway.key))  # in the dashboard: Keys > Revoke
    events = await chat.send(user("and now?"))  # the same conversation, no restart
    # ADK logs the refused tool listing and carries on WITHOUT tools: the model is offered none, nothing runs
    assert model.requests[-1].tools_dict == {} and texts(events) == ["I cannot reach the tools."]
    assert gateway.shop.calls == ["list_orders"]
    refused = gateway.gw.audit.query(outcome="auth_failed")
    assert refused and all(r.key_id == parse_key(gateway.key) for r in refused)  # the gateway saw who it refused
    assert gateway.key not in caplog.text


# -- settings ------------------------------------------------------------------------------------------------------

def test_settings_come_from_the_environment():
    key = generate_api_key()[1]
    agent = agent_from_env({"GATEWAY_API_KEY": key})
    assert isinstance(agent.model, LiteLlm) and agent.model.model == "groq/openai/gpt-oss-120b"
    other = agent_from_env({"GATEWAY_API_KEY": key, "ADK_MODEL": "groq/openai/gpt-oss-20b"})
    assert other.model.model == "groq/openai/gpt-oss-20b"


@pytest.mark.parametrize("env, says", [
    ({}, "set GATEWAY_API_KEY"),
    ({"GATEWAY_API_KEY": "gsk_s3cret-value"}, "does not look like a gateway API key"),
    ({"GATEWAY_API_KEY": "wmcp_0123abcd_s3cret-value"}, "does not look like a gateway API key"),
    ({"GATEWAY_MCP_URL": "ftp://gateway/mcp"}, "GATEWAY_MCP_URL"),
])
def test_a_missing_or_wrong_setting_is_refused_without_showing_the_key(env, says):
    if "GATEWAY_API_KEY" not in env and "GATEWAY_MCP_URL" in env:
        env = {**env, "GATEWAY_API_KEY": generate_api_key()[1]}
    with pytest.raises(RuntimeError, match=says) as e:
        agent_from_env(env)
    assert "s3cret-value" not in str(e.value)  # the value given is never repeated
    assert not re.search(r"wmcp_[0-9a-f]{8}_[0-9a-f]{64}", str(e.value))  # nor any real key


def test_adk_web_finds_the_agent_the_way_it_loads_every_agent(monkeypatch):
    monkeypatch.setenv("ADK_DISABLE_LOAD_DOTENV", "1")  # no developer .env in a test
    monkeypatch.setenv("GATEWAY_API_KEY", generate_api_key()[1])
    agent = AgentLoader(str(ADK_AGENT_DIR)).load_agent("gateway_agent")
    assert agent.name == "gateway_agent" and len(agent.tools) == 2
