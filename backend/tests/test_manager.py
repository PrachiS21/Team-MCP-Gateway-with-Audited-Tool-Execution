import pytest

from gateway.mcp.manager import ManagerError, UpstreamManager
from gateway.mcp.upstream_client import ErrorKind, Status, UpstreamConfig
from tests.fixtures.fake_upstream import FakeUpstream


def cfg(fake, **kw):
    kw.setdefault("timeout", 1.5)
    return UpstreamConfig(url=fake.url, **kw)


@pytest.fixture
async def world():
    """Two fakes that both expose `echo` (collision); b also has `only_b`."""
    a = FakeUpstream("a", tools=("echo",))
    b = FakeUpstream("b", tools=("echo", "only_b"))
    await a.start()
    await b.start()
    m = UpstreamManager()
    m.add("id-a", "alpha", cfg(a))
    m.add("id-b", "beta", cfg(b))
    yield m, a, b
    await m.close()
    await a.stop()
    await b.stop()


async def test_lists_namespaced_tools_and_collision_is_resolved(world):
    m, *_ = world
    cat = await m.list_all_tools()
    assert sorted(t.name for t in cat.tools) == ["alpha__echo", "beta__echo", "beta__only_b"]
    # the Tool objects clients receive carry the namespaced name too
    assert {t.tool.name for t in cat.tools} == {t.name for t in cat.tools}


async def test_calls_are_routed_to_the_right_server(world):
    m, *_ = world
    ra = await m.call_tool("alpha__echo", {"text": "x"})
    rb = await m.call_tool("beta__echo", {"text": "x"})
    assert ra.content.data == "a:echo:x"
    assert rb.content.data == "b:echo:x"


async def test_unknown_tool_and_bad_names_are_structured_errors(world):
    m, *_ = world
    for name in ("nope__echo", "echo", "alpha__", "__echo"):
        res = await m.call_tool(name, {})
        assert res.error.kind is ErrorKind.UNKNOWN_TOOL, name


async def test_slow_upstream_does_not_break_others(world):
    m, a, b = world
    b.latency = 5.0  # beyond the 1.5s timeout
    cat = await m.list_all_tools()
    assert [t.name for t in cat.tools] == ["alpha__echo"]
    states = {s.slug: s for s in cat.servers}
    assert states["beta"].error.kind is ErrorKind.TIMEOUT
    assert states["alpha"].status is Status.CONNECTED
    assert (await m.call_tool("alpha__echo", {"text": "ok"})).ok


async def test_dead_upstream_is_unreachable_then_recovers(world):
    m, a, b = world
    await b.stop()
    cat = await m.list_all_tools()
    assert [t.name for t in cat.tools] == ["alpha__echo"]
    assert {s.slug: s.status for s in cat.servers}["beta"] is Status.UNREACHABLE
    await b.start()
    cat = await m.list_all_tools()
    assert len(cat.tools) == 3


async def test_disable_hides_tools_and_blocks_calls_enable_restores(world):
    m, *_ = world
    await m.disable("id-b")
    cat = await m.list_all_tools()
    assert [t.name for t in cat.tools] == ["alpha__echo"]
    assert {s.slug: s.status for s in cat.servers}["beta"] is Status.DISABLED
    assert (await m.call_tool("beta__echo", {})).error.kind is ErrorKind.DISABLED
    m.enable("id-b")
    assert len((await m.list_all_tools()).tools) == 3


async def test_remove_drops_the_server(world):
    m, *_ = world
    await m.remove("id-b")
    assert [t.name for t in (await m.list_all_tools()).tools] == ["alpha__echo"]
    assert (await m.call_tool("beta__echo", {})).error.kind is ErrorKind.UNKNOWN_TOOL


async def test_slug_rules(world):
    m, a, _ = world
    for bad in ("Has_Upper", "a__b", "with space", "-lead", "", "under_score"):
        with pytest.raises(ManagerError):
            m.add("x", bad, cfg(a))
    with pytest.raises(ManagerError):
        m.add("x", "alpha", cfg(a))  # duplicate slug
    with pytest.raises(ManagerError):
        m.add("id-a", "fresh", cfg(a))  # duplicate id


async def test_rename_slug_changes_tool_names(world):
    m, *_ = world
    await m.update("id-a", slug="renamed")
    names = [t.name for t in (await m.list_all_tools()).tools]
    assert "renamed__echo" in names and "alpha__echo" not in names


async def test_update_credential_switches_to_new_client():
    f = FakeUpstream("s", token="good")
    await f.start()
    m = UpstreamManager()
    try:
        m.add("s", "secure", cfg(f, credential="bad"))
        cat = await m.list_all_tools()
        assert cat.servers[0].error.kind is ErrorKind.AUTH and not cat.tools
        await m.update("s", config=cfg(f, credential="good"))
        assert [t.name for t in (await m.list_all_tools()).tools] == ["secure__echo"]
    finally:
        await m.close()
        await f.stop()
