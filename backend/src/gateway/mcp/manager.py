"""Owns every UpstreamClient and presents them as one tool catalogue.

Naming: a tool is exposed as "<slug>__<tool>". The name is split on the FIRST
"__", and slugs may not contain "__", so the split is never ambiguous. Two
servers with a tool called "search" become "woo__search" and "docs__search".
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from mcp.types import Tool

from .upstream_client import (
    CallResult,
    ErrorKind,
    Status,
    UpstreamClient,
    UpstreamConfig,
    UpstreamError,
)

log = logging.getLogger("gateway")

SEPARATOR = "__"
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # no "_" at all, so never "__"


class ManagerError(ValueError):
    """Raised for caller mistakes (bad slug, duplicate id), never for upstream faults."""


@dataclass(frozen=True)
class NamespacedTool:
    name: str  # "<slug>__<original_name>", what clients see
    server_id: str
    slug: str
    original_name: str  # what the upstream knows it as
    tool: Tool  # copy of the upstream definition with name = namespaced name


@dataclass(frozen=True)
class ServerState:
    server_id: str
    slug: str
    status: Status
    error: UpstreamError | None


@dataclass
class CatalogueResult:
    tools: list[NamespacedTool] = field(default_factory=list)
    servers: list[ServerState] = field(default_factory=list)  # one entry per registered server


@dataclass
class _Entry:
    slug: str
    client: UpstreamClient
    config: UpstreamConfig  # what the client was built from; lets sync() notice changes


def namespaced(slug: str, tool_name: str) -> str:
    return f"{slug}{SEPARATOR}{tool_name}"


def split_name(name: str) -> tuple[str, str] | None:
    slug, sep, tool = name.partition(SEPARATOR)
    if not sep or not slug or not tool:
        return None
    return slug, tool


class UpstreamManager:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}  # server_id -> entry

    # -- registration -----------------------------------------------------

    def _check_slug(self, slug: str, own_id: str | None = None) -> None:
        if not SLUG_RE.match(slug):
            raise ManagerError(f"invalid slug {slug!r}: use lowercase letters, digits and single hyphens")
        for sid, e in self._entries.items():
            if e.slug == slug and sid != own_id:
                raise ManagerError(f"slug {slug!r} is already used by another server")

    def add(self, server_id: str, slug: str, config: UpstreamConfig, enabled: bool = True) -> None:
        if server_id in self._entries:
            raise ManagerError(f"server id {server_id!r} already registered")
        self._check_slug(slug)
        self._entries[server_id] = _Entry(slug, UpstreamClient(config, enabled=enabled), config)

    async def update(self, server_id: str, *, slug: str | None = None, config: UpstreamConfig | None = None) -> None:
        entry = self._get(server_id)
        if slug is not None:
            self._check_slug(slug, own_id=server_id)
            entry.slug = slug
        if config is not None:
            # Connection details changed: replace the client, keep the enabled flag.
            was_enabled = entry.client.status is not Status.DISABLED
            await entry.client.close()
            entry.client = UpstreamClient(config, enabled=was_enabled)
            entry.config = config

    async def remove(self, server_id: str) -> None:
        entry = self._entries.pop(server_id, None)
        if entry is None:
            raise ManagerError(f"unknown server id {server_id!r}")
        await entry.client.close()

    async def disable(self, server_id: str) -> None:
        await self._get(server_id).client.disable()

    def enable(self, server_id: str) -> None:
        self._get(server_id).client.enable()

    async def sync(self, desired: Iterable[tuple[str, str, UpstreamConfig, bool]]) -> None:
        """Make the live servers match `desired` = (id, slug, config, enabled) rows from the
        registry: add new ones, drop deleted ones, apply edits. The database is the source of
        truth, so this is how changes made by another process (the CLI, another replica) arrive.
        A row that cannot be applied (e.g. a slug clash) is logged and skipped, never fatal."""
        want = {sid: (slug, cfg, enabled) for sid, slug, cfg, enabled in desired}
        for sid in [s for s in self._entries if s not in want]:
            await self.remove(sid)
        for sid, (slug, cfg, enabled) in want.items():
            try:
                entry = self._entries.get(sid)
                if entry is None:
                    self.add(sid, slug, cfg, enabled)
                    continue
                if entry.config != cfg:
                    await self.update(sid, config=cfg)
                if entry.slug != slug:
                    await self.update(sid, slug=slug)
                if enabled and not entry.client.enabled:
                    self.enable(sid)
                elif not enabled and entry.client.enabled:
                    await self.disable(sid)
            except ManagerError as exc:
                log.warning("sync skipped server %s: %s", sid, exc)

    def _get(self, server_id: str) -> _Entry:
        try:
            return self._entries[server_id]
        except KeyError:
            raise ManagerError(f"unknown server id {server_id!r}") from None

    # -- catalogue --------------------------------------------------------

    async def list_all_tools(self) -> CatalogueResult:
        """Ask every enabled server in parallel. A failing server adds no tools
        and shows its error in `servers`; the others are unaffected."""
        items = list(self._entries.items())
        results = await asyncio.gather(*(e.client.list_tools() for _, e in items))
        out = CatalogueResult()
        for (sid, e), res in zip(items, results):
            out.servers.append(ServerState(sid, e.slug, e.client.status, e.client.last_error))
            for t in res.tools:
                full = namespaced(e.slug, t.name)
                out.tools.append(NamespacedTool(full, sid, e.slug, t.name, t.model_copy(update={"name": full})))
        return out

    def servers(self) -> list[ServerState]:
        """Current status without contacting any upstream (cheap, for the dashboard)."""
        return [ServerState(sid, e.slug, e.client.status, e.client.last_error) for sid, e in self._entries.items()]

    # -- routing ----------------------------------------------------------

    def resolve(self, name: str) -> tuple[str, UpstreamClient, str] | None:
        """Map "<slug>__<tool>" to (server_id, client, original tool name)."""
        parts = split_name(name)
        if parts is None:
            return None
        slug, tool = parts
        for sid, e in self._entries.items():
            if e.slug == slug:
                return sid, e.client, tool
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallResult:
        target = self.resolve(name)
        if target is None:
            return CallResult(error=UpstreamError(ErrorKind.UNKNOWN_TOOL, f"no server owns tool {name!r}"))
        _, client, original = target
        return await client.call_tool(original, arguments)

    async def close(self) -> None:
        await asyncio.gather(*(e.client.close() for e in self._entries.values()))
