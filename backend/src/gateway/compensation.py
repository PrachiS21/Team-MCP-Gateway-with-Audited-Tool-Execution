"""Opt-in undo for tools that change something.

MCP has no standard "undo", so the gateway can only reverse an action whose inverse
someone declared. A Compensation says, for one tool on one server:
  before_*  an optional read-only tool to run FIRST, capturing the state that the
            undo will need (e.g. an order's current status);
  undo_*    the tool that reverses the action, with arguments built from the
            original arguments, the call's result, and the captured state.
Tools with no entry cannot be reversed; the gateway then fails open and logs loudly.
"""

from dataclasses import dataclass
from typing import Any, Callable, Mapping

Args = Mapping[str, Any]
# (original args, result payload of the call, captured "before" payload) -> undo args,
# or None when there is nothing to undo.
BuildUndoArgs = Callable[[Args, Any, Any], dict[str, Any] | None]


@dataclass(frozen=True)
class Compensation:
    """Undo tools MUST be idempotent (delete-if-exists, set status to X): the undo
    worker retries until it succeeds, so one attempt may run twice."""

    undo_tool: str  # original (un-prefixed) tool name on the same server
    build_undo_args: BuildUndoArgs
    before_tool: str | None = None
    build_before_args: Callable[[Args], dict[str, Any]] | None = None
    # Optimistic check, run just before the undo: read the resource again and confirm it
    # is still as our action left it. If not (someone else changed it), the undo is NOT
    # run and the job ends as "conflict".
    verify_tool: str | None = None
    build_verify_args: Callable[[Args, Any, Any], dict[str, Any]] | None = None  # (args, result, before)
    still_as_we_left_it: Callable[[Any, Args, Any, Any], bool] | None = None  # (current, args, result, before)
    # Resource this call touches, e.g. lambda a: f"order:{a['order_id']}". Calls and undos
    # on the same key never overlap. Default: one key per (server, tool).
    lock_key: Callable[[Args], str] | None = None


class CompensationRegistry:
    def __init__(self) -> None:
        self._by_tool: dict[tuple[str, str], Compensation] = {}
        self._hidden: set[tuple[str, str]] = set()

    def hide(self, server_slug: str, tool: str) -> None:
        """Mark a tool internal: left out of every client's tool list and refused if a client calls
        it. Only the undo worker (which talks to the manager directly) may use it."""
        self._hidden.add((server_slug, tool))

    def is_hidden(self, server_slug: str, tool: str) -> bool:
        return (server_slug, tool) in self._hidden

    def register(self, server_slug: str, tool: str, comp: Compensation) -> None:
        if (comp.before_tool is None) != (comp.build_before_args is None):
            raise ValueError("before_tool and build_before_args must be given together")
        verify = (comp.verify_tool, comp.build_verify_args, comp.still_as_we_left_it)
        if any(v is None for v in verify) and any(v is not None for v in verify):
            raise ValueError("verify_tool, build_verify_args and still_as_we_left_it must be given together")
        self._by_tool[(server_slug, tool)] = comp

    def get(self, server_slug: str, tool: str) -> Compensation | None:
        return self._by_tool.get((server_slug, tool))


def payload(call_result: Any) -> Any:
    """The useful data of a FastMCP CallToolResult: structured data if present, else text."""
    if call_result is None:
        return None
    structured = getattr(call_result, "structured_content", None)
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    return [getattr(b, "text", None) for b in getattr(call_result, "content", [])]
