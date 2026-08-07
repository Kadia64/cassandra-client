"""Tests for the MCP server.

The brain is stubbed, so these cover the protocol handling and the tool
registry — the parts that break silently. A malformed response here does not
raise, it just makes Claude Code show nothing, which is why the JSON-RPC shape
is worth pinning down.
"""
from __future__ import annotations

import json

import pytest

import cassandra_mcp as mcp


class FakeBrain(list):
    """Records every call, and answers from `responses` keyed by path prefix.

    Longest prefix wins, so `/api/projects/x/file` is not shadowed by a stub
    registered for `/api/projects`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.responses: dict[str, object] = {}

    def __call__(self, method, path, body=None, params=None):
        self.append({"method": method, "path": path, "body": body, "params": params})
        for prefix in sorted(self.responses, key=len, reverse=True):
            if path.startswith(prefix):
                value = self.responses[prefix]
                if isinstance(value, Exception):
                    raise value
                return value
        return {}


@pytest.fixture
def brain(monkeypatch) -> FakeBrain:
    fake = FakeBrain()
    monkeypatch.setattr(mcp, "call_brain", fake)
    return fake


def rpc(method: str, params: dict | None = None, mid: int | None = 1) -> dict | None:
    message = {"jsonrpc": "2.0", "method": method}
    if mid is not None:
        message["id"] = mid
    if params is not None:
        message["params"] = params
    return mcp.handle(message)


def text_of(response: dict) -> str:
    return response["result"]["content"][0]["text"]


# ---- protocol --------------------------------------------------------------

def test_initialize_advertises_tools() -> None:
    result = rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "cassandra"


def test_initialize_falls_back_on_an_unknown_protocol() -> None:
    """A Claude Code upgrade must not silently break the server."""
    result = rpc("initialize", {"protocolVersion": "2099-01-01"})["result"]
    assert result["protocolVersion"] in mcp.SUPPORTED_PROTOCOLS


def test_notifications_are_never_answered() -> None:
    """A response to a notification is a protocol violation."""
    assert rpc("notifications/initialized", {}, mid=None) is None


def test_ping() -> None:
    assert rpc("ping")["result"] == {}


def test_unknown_method_is_a_json_rpc_error() -> None:
    assert rpc("nonsense/method")["error"]["code"] == -32601


def test_every_response_carries_its_request_id() -> None:
    for method in ("initialize", "ping", "tools/list"):
        assert mcp.handle({"jsonrpc": "2.0", "id": 42, "method": method})["id"] == 42


# ---- the registry ----------------------------------------------------------

def test_tools_list_is_well_formed() -> None:
    tools = rpc("tools/list")["result"]["tools"]
    assert tools, "no tools registered"
    for t in tools:
        assert t["name"] and t["description"]
        assert t["inputSchema"]["type"] == "object"
        assert "fn" not in t, "the implementation must not go on the wire"
        for name, spec in t["inputSchema"]["properties"].items():
            assert spec.get("type") and spec.get("description"), f"{t['name']}.{name}"


def test_required_arguments_are_declared() -> None:
    by_name = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
    assert by_name["project_read"]["inputSchema"]["required"] == ["slug", "path"]


def test_calling_an_unknown_tool() -> None:
    assert rpc("tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602


# ---- tools -----------------------------------------------------------------

def test_project_list(brain) -> None:
    brain.responses["/api/projects"] = {"projects": [
        {"slug": "mygame", "name": "My Game", "status": "active",
         "description": "a game", "code": {"kind": "github"}}]}
    out = text_of(rpc("tools/call", {"name": "project_list", "arguments": {}}))
    assert "mygame" in out and "My Game" in out and "code:github" in out


def test_project_list_when_there_are_none(brain) -> None:
    brain.responses["/api/projects"] = {"projects": []}
    assert "No projects" in text_of(rpc("tools/call", {"name": "project_list",
                                                       "arguments": {}}))


def test_project_context_includes_the_context_files(brain) -> None:
    brain.responses["/api/projects/mygame/file"] = {"text": "the overview text"}
    brain.responses["/api/projects/mygame"] = {
        "name": "My Game", "status": "active", "description": "",
        "code": {"kind": "machine", "machine_id": "win-desktop", "path": "C:\\dev"}}
    out = text_of(rpc("tools/call", {"name": "project_context",
                                     "arguments": {"slug": "mygame"}}))
    assert "My Game" in out and "the overview text" in out
    assert "win-desktop" in out, "where the code lives is part of the context"


def test_project_note_is_stored_verbatim(brain) -> None:
    brain.responses["/api/projects/mygame/ideas"] = {"file": "2026-08-07-x.md"}
    out = text_of(rpc("tools/call", {"name": "project_note",
                                     "arguments": {"slug": "mygame", "text": "an idea"}}))
    assert "2026-08-07-x.md" in out
    assert brain[-1]["body"] == {"text": "an idea"}
    assert brain[-1]["method"] == "POST"


def test_project_read_passes_the_path_as_a_parameter(brain) -> None:
    """Not interpolated into the URL — a path with spaces or slashes must not
    change which endpoint is hit."""
    brain.responses["/api/projects/mygame/file"] = {"text": "contents"}
    rpc("tools/call", {"name": "project_read",
                       "arguments": {"slug": "mygame", "path": "context/a b.md"}})
    assert brain[-1]["params"] == {"path": "context/a b.md"}


def test_project_read_missing_file(brain) -> None:
    brain.responses["/api/projects/mygame/file"] = RuntimeError("HTTP 404")
    out = text_of(rpc("tools/call", {"name": "project_read",
                                     "arguments": {"slug": "mygame", "path": "nope.md"}}))
    assert "No file" in out


# ---- failure is a result, not a crash --------------------------------------

def test_an_unreachable_brain_reads_back_as_an_error_result(brain) -> None:
    """A tool failure the model can see and act on, rather than a protocol
    error that ends the session."""
    brain.responses["/api/projects"] = RuntimeError("cannot reach the server")
    response = rpc("tools/call", {"name": "project_list", "arguments": {}})
    assert response["result"]["isError"] is True
    assert "cannot reach" in text_of(response)


def test_bad_arguments_read_back_as_an_error_result(brain) -> None:
    response = rpc("tools/call", {"name": "project_read", "arguments": {"slug": "x"}})
    assert response["result"]["isError"] is True
    assert "Bad arguments" in text_of(response)


def test_responses_are_serialisable_json_rpc() -> None:
    """Everything written to stdout must be one valid JSON object on one line;
    a stray newline or an unserialisable value corrupts the stream for good."""
    for response in (rpc("initialize", {"protocolVersion": "2025-06-18"}),
                     rpc("tools/list"),
                     rpc("nonsense")):
        line = json.dumps(response)
        assert "\n" not in line
        assert json.loads(line)["jsonrpc"] == "2.0"


# ---- status ----------------------------------------------------------------

def test_set_status_writes_a_handoff_not_a_log(brain) -> None:
    """Three headings, overwritten. History lives in transcripts and git; a
    status that accumulates stops being readable in ten seconds."""
    rpc("tools/call", {"name": "project_set_status", "arguments": {
        "slug": "mygame", "stands": "combat loop works",
        "next": "- tune damage\n- add sfx", "blocked": "waiting on art"}})

    call = brain[-1]
    assert call["method"] == "PUT"
    assert call["body"]["path"] == "context/status.md"
    body = call["body"]["text"]
    assert "## Where it stands" in body and "combat loop works" in body
    assert "## What's next" in body and "tune damage" in body
    assert "## Blocked" in body and "waiting on art" in body


def test_status_omits_the_blocked_heading_when_nothing_is(brain) -> None:
    rpc("tools/call", {"name": "project_set_status", "arguments": {
        "slug": "mygame", "stands": "fine", "next": "carry on"}})
    assert "## Blocked" not in brain[-1]["body"]["text"]


def test_status_tool_tells_the_model_when_to_call_it() -> None:
    """It is only useful if it is written at the END of a session, so the
    description has to say so — nothing else will."""
    tools = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
    assert "END" in tools["project_set_status"]["description"]
