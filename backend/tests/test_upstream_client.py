import pytest

from gateway.mcp.upstream_client import ErrorKind, Status, UpstreamClient, UpstreamConfig
from tests.fixtures.fake_upstream import FakeUpstream


@pytest.fixture
async def fake():
    f = FakeUpstream("a", tools=("echo", "ping"), token="s3cret")
    await f.start()
    yield f
    await f.stop()


def make(fake, **kw):
    kw.setdefault("credential", "s3cret")
    kw.setdefault("timeout", 2.0)
    return UpstreamClient(UpstreamConfig(url=fake.url, **kw))


async def test_list_and_call(fake):
    c = make(fake)
    res = await c.list_tools()
    assert res.error is None and {t.name for t in res.tools} == {"echo", "ping"}
    assert c.status is Status.CONNECTED
    call = await c.call_tool("echo", {"text": "hi"})
    assert call.ok and call.content.data == "a:echo:hi"
    await c.close()


async def test_wrong_credential_is_a_clear_auth_error(fake):
    c = make(fake, credential="nope")
    res = await c.list_tools()
    assert res.error.kind is ErrorKind.AUTH
    assert c.status is Status.UNREACHABLE
    await c.close()


async def test_missing_credential_is_auth_error(fake):
    c = make(fake, credential=None)
    assert (await c.list_tools()).error.kind is ErrorKind.AUTH
    await c.close()


async def test_slow_upstream_times_out(fake):
    c = make(fake, timeout=0.5)
    fake.latency = 2.0
    res = await c.call_tool("echo", {})
    assert res.error.kind is ErrorKind.TIMEOUT
    await c.close()


async def test_dead_then_recovered(fake):
    c = make(fake)
    assert (await c.list_tools()).error is None
    await fake.stop()
    res = await c.list_tools()
    assert res.error is not None and c.status is Status.UNREACHABLE
    await fake.start()
    assert (await c.list_tools()).error is None
    assert c.status is Status.CONNECTED
    await c.close()


async def test_unknown_tool_is_reported_not_raised(fake):
    c = make(fake)
    call = await c.call_tool("does_not_exist", {})
    assert not call.ok
    await c.close()


async def test_disabled_makes_no_connection(fake):
    c = make(fake)
    await c.disable()
    assert c.status is Status.DISABLED
    assert (await c.list_tools()).error.kind is ErrorKind.DISABLED
    c.enable()
    assert (await c.list_tools()).error is None
    await c.close()


async def test_describe_never_leaks_credential(fake):
    c = make(fake)
    await c.list_tools()
    d = c.describe()
    assert d["has_credential"] is True
    assert "s3cret" not in repr(d)
    await c.close()


async def test_server_dying_after_a_live_connection_is_unreachable_not_generic(fake):
    """Regression: an open session whose server dies raises MCPError(-32000); the
    following reconnect raises a bundled-httpx ConnectError. Both mean 'unreachable'."""
    c = make(fake)
    assert (await c.list_tools()).error is None
    await fake.stop()
    first = await c.call_tool("echo", {})
    second = await c.call_tool("echo", {})
    assert first.error.kind is ErrorKind.UNREACHABLE
    assert second.error.kind is ErrorKind.UNREACHABLE
    await fake.start()
    await c.close()


def _chained(outer: BaseException, inner: BaseException) -> BaseException:
    try:
        try:
            raise inner
        except BaseException:
            raise outer
    except BaseException as e:
        return e


def test_a_timeout_wrapped_by_the_sdk_is_a_timeout_not_unreachable():
    """Seen against the real "everything" server on a reused session: FastMCP's own request timeout raises
    MCPError(-32001) with the TimeoutError underneath. TimeoutError is also an OSError, which used to make this
    look like "unreachable"."""
    from mcp.shared.exceptions import MCPError

    from gateway.mcp.upstream_client import _classify
    sdk = _chained(MCPError(-32001, "Request 'tools/call' timed out"), TimeoutError())
    assert _classify(sdk).kind is ErrorKind.TIMEOUT
    assert _classify(_chained(RuntimeError("wrapper"), TimeoutError())).kind is ErrorKind.TIMEOUT
    assert _classify(ConnectionRefusedError()).kind is ErrorKind.UNREACHABLE  # real connection errors unchanged
