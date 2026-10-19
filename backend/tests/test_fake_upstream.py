import pytest
from fastmcp import Client

from tests.fixtures.fake_upstream import FakeUpstream


async def test_fake_upstream_serves_tools_and_requires_token():
    fake = FakeUpstream("a", tools=("echo", "ping"), token="s3cret")
    await fake.start()
    try:
        async with Client(fake.url, auth="s3cret") as c:
            assert {t.name for t in await c.list_tools()} == {"echo", "ping"}
            r = await c.call_tool("echo", {"text": "hi"})
            assert r.data == "a:echo:hi"
        with pytest.raises(Exception):
            async with Client(fake.url) as c:
                await c.list_tools()
    finally:
        await fake.stop()


async def test_fake_upstream_stop_and_restart():
    fake = FakeUpstream("b")
    await fake.start()
    await fake.stop()
    with pytest.raises(Exception):
        async with Client(fake.url, timeout=2) as c:
            await c.list_tools()
    await fake.start()
    try:
        async with Client(fake.url) as c:
            assert len(await c.list_tools()) == 1
    finally:
        await fake.stop()
