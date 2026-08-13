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


# ---- roadmaps ---------------------------------------------------------------

ROADMAP = {
    "title": "Companion", "slug": "main", "parent": None,
    "progress": {"done": 1, "total": 3},
    "goals": [
        {"id": "gol-aaa", "text": "Voice input", "state": "done", "note": "Whisper",
         "children": []},
        {"id": "gol-bbb", "text": "Orchestrator", "state": "todo", "note": "",
         "children": [{"name": "tool-loop", "title": "Tool loop", "missing": False,
                       "progress": {"done": 0, "total": 2}}]},
    ],
}


def test_roadmap_read_shows_every_goal_id(brain) -> None:
    """Every write tool takes an id, and the only place the model can get one is
    this output — so a line without one is a dead end."""
    brain.responses["/api/projects/companion/roadmaps"] = ROADMAP
    out = text_of(rpc("tools/call", {"name": "roadmap_read",
                                     "arguments": {"slug": "companion"}}))
    assert "gol-aaa" in out and "gol-bbb" in out


def test_roadmap_read_shows_state_and_progress(brain) -> None:
    brain.responses["/api/projects/companion/roadmaps"] = ROADMAP
    out = text_of(rpc("tools/call", {"name": "roadmap_read",
                                     "arguments": {"slug": "companion"}}))
    assert "1/3 done" in out
    assert "[x] Voice input" in out
    assert "[ ] Orchestrator" in out


def test_roadmap_read_names_sub_roadmaps(brain) -> None:
    """The name is the handle the model passes back as `roadmap`."""
    brain.responses["/api/projects/companion/roadmaps"] = ROADMAP
    out = text_of(rpc("tools/call", {"name": "roadmap_read",
                                     "arguments": {"slug": "companion"}}))
    assert "'tool-loop'" in out and "0/2 done" in out


def test_roadmap_read_defaults_to_main(brain) -> None:
    brain.responses["/api/projects/companion/roadmaps"] = ROADMAP
    rpc("tools/call", {"name": "roadmap_read", "arguments": {"slug": "companion"}})
    assert brain[-1]["path"] == "/api/projects/companion/roadmaps/main"


def test_roadmap_read_before_there_is_one(brain) -> None:
    """A project with no roadmap is the normal starting state, not a failure —
    and the answer should say what to do about it."""
    brain.responses["/api/projects/companion/roadmaps"] = RuntimeError(
        "HTTP 404 from /api/projects/companion/roadmaps/main: unknown roadmap")
    result = rpc("tools/call", {"name": "roadmap_read", "arguments": {"slug": "companion"}})
    assert result["result"]["isError"] is False
    assert "roadmap_add" in text_of(result)


def test_a_real_roadmap_failure_still_surfaces(brain) -> None:
    brain.responses["/api/projects/companion/roadmaps"] = RuntimeError("cannot reach brain")
    result = rpc("tools/call", {"name": "roadmap_read", "arguments": {"slug": "companion"}})
    assert result["result"]["isError"] is True


def test_roadmap_add_posts_the_goal(brain) -> None:
    brain.responses["/api/projects/companion/roadmaps"] = {
        "id": "gol-ccc", "text": "New goal"}
    out = text_of(rpc("tools/call", {"name": "roadmap_add",
                                     "arguments": {"slug": "companion", "text": "New goal"}}))
    assert brain[-1]["method"] == "POST"
    assert brain[-1]["path"] == "/api/projects/companion/roadmaps/main/goals"
    assert brain[-1]["body"] == {"text": "New goal"}
    # The id comes back so the model can act on it without re-reading.
    assert "gol-ccc" in out


def test_roadmap_add_omits_an_empty_note(brain) -> None:
    """A blank note would overwrite nothing, but sending it is noise the server
    then has to decide about."""
    brain.responses["/api/projects/companion/roadmaps"] = {"id": "gol-c", "text": "x"}
    rpc("tools/call", {"name": "roadmap_add",
                       "arguments": {"slug": "companion", "text": "x"}})
    assert "note" not in brain[-1]["body"]


def test_roadmap_set_sends_only_what_changed(brain) -> None:
    brain.responses["/api/projects/companion/roadmaps"] = {
        "id": "gol-aaa", "text": "Voice input", "state": "done"}
    rpc("tools/call", {"name": "roadmap_set",
                       "arguments": {"slug": "companion", "goal": "gol-aaa",
                                     "state": "done"}})
    assert brain[-1]["method"] == "PUT"
    assert brain[-1]["path"] == "/api/projects/companion/roadmaps/main/goals/gol-aaa"
    assert brain[-1]["body"] == {"state": "done"}


def test_roadmap_set_reorders_through_the_same_call(brain) -> None:
    """Reordering folds in here rather than being a fifth tool, because the
    server already takes `index` on this endpoint."""
    brain.responses["/api/projects/companion/roadmaps"] = {
        "id": "gol-aaa", "text": "x", "state": "todo"}
    rpc("tools/call", {"name": "roadmap_set",
                       "arguments": {"slug": "companion", "goal": "gol-aaa",
                                     "position": 0}})
    assert brain[-1]["body"] == {"index": 0}


def test_roadmap_set_with_nothing_to_change(brain) -> None:
    out = text_of(rpc("tools/call", {"name": "roadmap_set",
                                     "arguments": {"slug": "companion", "goal": "gol-aaa"}}))
    assert "Nothing to change" in out
    assert not brain, "an empty change must not reach the brain"


def test_roadmap_expand_returns_the_name_to_use_next(brain) -> None:
    """The model has to pass this name straight back as `roadmap`, so it has to
    be in the reply rather than needing another read."""
    brain.responses["/api/projects/companion/roadmaps"] = {
        "slug": "tool-loop", "title": "Tool loop"}
    out = text_of(rpc("tools/call", {
        "name": "roadmap_expand",
        "arguments": {"slug": "companion", "goal": "gol-bbb", "title": "Tool loop"}}))
    assert "tool-loop" in out and "roadmap_add" in out


def test_there_is_no_roadmap_delete_tool() -> None:
    """Deliberate: `dropped` retires a goal and keeps it on the record. An agent
    that can quietly erase a decision is a worse trade than a struck-through line."""
    names = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
    assert not any("delete" in n or "remove" in n for n in names)


def test_the_session_kickoff_carries_the_roadmap(brain) -> None:
    """It is literally "what is next", and a session that has to be told to go
    looking for it will not."""
    brain.responses["/api/projects/companion/roadmaps"] = ROADMAP
    brain.responses["/api/projects/companion"] = {"ideas": []}
    out = text_of(rpc("tools/call", {"name": "project_recent",
                                     "arguments": {"slug": "companion"}}))
    assert "## roadmap" in out and "Voice input" in out


def test_a_project_with_no_roadmap_still_has_a_kickoff(brain) -> None:
    brain.responses["/api/projects/companion/roadmaps"] = RuntimeError("HTTP 404")
    brain.responses["/api/projects/companion"] = {"ideas": []}
    result = rpc("tools/call", {"name": "project_recent", "arguments": {"slug": "companion"}})
    assert result["result"]["isError"] is False


# ---- the idea board --------------------------------------------------------
# `project_note` gained three optional classifiers, and two tools were added so
# a session can read the board and move a card without leaving the editor.

def test_note_without_classifiers_sends_only_the_text(brain) -> None:
    """The fast path must stay one gesture: an unassessed idea says so by
    omitting the fields, and the brain applies its own defaults."""
    brain.responses["/api/projects/mygame/ideas"] = {"file": "x.md", "status": "raw"}
    text_of(rpc("tools/call", {"name": "project_note",
                               "arguments": {"slug": "mygame", "text": "an idea"}}))
    assert brain[-1]["body"] == {"text": "an idea"}


def test_note_can_file_straight_into_a_column(brain) -> None:
    brain.responses["/api/projects/mygame/ideas"] = {"file": "x.md", "status": "quick"}
    out = text_of(rpc("tools/call", {"name": "project_note", "arguments": {
        "slug": "mygame", "text": "an idea", "column": "quick",
        "priority": "high", "complexity": "small"}}))
    assert brain[-1]["body"] == {"text": "an idea", "status": "quick",
                                 "priority": "high", "complexity": "small"}
    assert "quick" in out


def test_ideas_are_grouped_by_column(brain) -> None:
    brain.responses["/api/projects/mygame"] = {"ideas": [
        {"file": "a.md", "title": "Alpha", "status": "raw",
         "priority": "low", "complexity": "trivial", "pinned": False},
        {"file": "b.md", "title": "Bravo", "status": "done",
         "priority": "high", "complexity": "large", "pinned": True},
    ]}
    out = text_of(rpc("tools/call", {"name": "project_ideas",
                                     "arguments": {"slug": "mygame"}}))
    assert "## raw (1)" in out and "## done (1)" in out
    assert "Alpha" in out and "Bravo" in out
    assert "PINNED Bravo" in out
    assert "## quick (0)" in out and "(empty)" in out


def test_ideas_can_be_narrowed_to_one_column(brain) -> None:
    brain.responses["/api/projects/mygame"] = {"ideas": [
        {"file": "a.md", "title": "Alpha", "status": "raw",
         "priority": "low", "complexity": "trivial", "pinned": False},
        {"file": "b.md", "title": "Bravo", "status": "done",
         "priority": "low", "complexity": "trivial", "pinned": False},
    ]}
    out = text_of(rpc("tools/call", {"name": "project_ideas",
                                     "arguments": {"slug": "mygame", "column": "done"}}))
    assert "Bravo" in out and "Alpha" not in out


def test_idea_set_sends_only_what_changed(brain) -> None:
    brain.responses["/api/projects/mygame/ideas/a.md"] = {
        "title": "Alpha", "status": "done", "priority": "low",
        "complexity": "trivial", "pinned": False}
    out = text_of(rpc("tools/call", {"name": "project_idea_set",
                                     "arguments": {"slug": "mygame", "file": "a.md",
                                                   "column": "done"}}))
    assert brain[-1]["body"] == {"status": "done"}
    assert brain[-1]["method"] == "PUT"
    assert "column:done" in out


def test_idea_set_can_pin_without_moving(brain) -> None:
    brain.responses["/api/projects/mygame/ideas/a.md"] = {
        "title": "Alpha", "status": "raw", "priority": "low",
        "complexity": "trivial", "pinned": True}
    out = text_of(rpc("tools/call", {"name": "project_idea_set",
                                     "arguments": {"slug": "mygame", "file": "a.md",
                                                   "pinned": True}}))
    assert brain[-1]["body"] == {"pinned": True}
    assert "pinned" in out


def test_idea_set_with_nothing_to_change_does_not_call_the_brain(brain) -> None:
    before = len(brain)
    out = text_of(rpc("tools/call", {"name": "project_idea_set",
                                     "arguments": {"slug": "mygame", "file": "a.md"}}))
    assert len(brain) == before, "an empty PUT would clear nothing but still write"
    assert "Nothing to change" in out


def test_board_vocabularies_are_declared_as_enums() -> None:
    """A model picks from a list rather than guessing a word the brain rejects."""
    tools = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
    assert tools["project_note"]["inputSchema"]["properties"]["column"]["enum"] == [
        "raw", "quick", "big", "done"]
    assert tools["project_idea_set"]["inputSchema"]["properties"]["priority"]["enum"] == [
        "high", "normal", "low"]


# ---- the transcript corpus --------------------------------------------------
# Not project-scoped, unlike everything above: most of a claude.ai history
# belongs to no project, and these tools are the only way to reach any of it.

HITS = {
    "query": "sand",
    "count": 2,
    "results": [
        {"id": "tsc-aaa", "source": "claude_web", "title": "Falling sand",
         "project_slug": None, "started_at": "2025-06-08T10:00:00Z",
         "received_at": "2026-08-09T04:00:00Z",
         "snippet": "trying to make a «sand» simulation"},
        {"id": "tsc-bbb", "source": "claude_code", "title": "chunk loading",
         "project_slug": "game-engine", "started_at": "2026-04-07T09:00:00Z",
         "received_at": "2026-08-09T04:00:00Z",
         "snippet": "«sand» falls between chunks"},
    ],
}


def test_search_returns_an_id_for_every_hit(brain) -> None:
    """read_transcript takes an id and the only place to get one is this
    output, so a result without one is a dead end."""
    brain.responses["/api/transcripts/search"] = HITS
    out = text_of(rpc("tools/call", {"name": "search_transcripts",
                                     "arguments": {"query": "sand"}}))
    assert "tsc-aaa" in out and "tsc-bbb" in out
    assert "read_transcript" in out


def test_search_names_the_source_and_where_it_was_filed(brain) -> None:
    brain.responses["/api/transcripts/search"] = HITS
    out = text_of(rpc("tools/call", {"name": "search_transcripts",
                                     "arguments": {"query": "sand"}}))
    assert "claude_web" in out and "claude_code" in out
    # An unrouted conversation says so rather than showing a blank column.
    assert "unfiled" in out and "game-engine" in out


def test_search_dates_by_when_the_conversation_happened(brain) -> None:
    # Not received_at: an imported archive lands in one second, so showing that
    # would date fourteen months of history to import day.
    brain.responses["/api/transcripts/search"] = HITS
    out = text_of(rpc("tools/call", {"name": "search_transcripts",
                                     "arguments": {"query": "sand"}}))
    assert "2025-06-08" in out
    assert "2026-08-09" not in out


def test_search_passes_filters_through_and_omits_empty_ones(brain) -> None:
    brain.responses["/api/transcripts/search"] = HITS
    rpc("tools/call", {"name": "search_transcripts",
                       "arguments": {"query": "sand", "source": "claude_web",
                                     "limit": 5}})
    params = brain[-1]["params"]
    assert params["q"] == "sand" and params["source"] == "claude_web"
    # A blank project must not become `project=`, which the brain would treat
    # as a real filter and match nothing.
    assert "project" not in params and "since" not in params


def test_search_says_so_plainly_when_nothing_matches(brain) -> None:
    brain.responses["/api/transcripts/search"] = {"query": "zzz", "count": 0,
                                                  "results": []}
    out = text_of(rpc("tools/call", {"name": "search_transcripts",
                                     "arguments": {"query": "zzz"}}))
    assert "No conversation" in out


TURNS = {
    "title": "Falling sand", "total": 90,
    "turns": [
        {"role": "user", "blocks": [{"kind": "text", "text": "how do I make sand fall"}]},
        {"role": "assistant", "blocks": [
            {"kind": "thinking", "text": "cellular automaton"},
            {"kind": "text", "text": "Swap the cell below."},
            {"kind": "tool_use", "name": "repl"},
        ]},
    ],
}


def test_read_transcript_renders_turns_and_names_tools(brain) -> None:
    brain.responses["/api/fleet/transcripts/"] = TURNS
    out = text_of(rpc("tools/call", {"name": "read_transcript",
                                     "arguments": {"id": "tsc-aaa"}}))
    assert "how do I make sand fall" in out
    assert "Swap the cell below." in out
    # The tool call is named but not expanded — its output is not the point.
    assert "[tool: repl]" in out


def test_read_transcript_offers_the_next_page(brain) -> None:
    """A conversation longer than the window has to say how to continue, or the
    model reads the first 40 turns and assumes that is all of it."""
    brain.responses["/api/fleet/transcripts/"] = TURNS
    out = text_of(rpc("tools/call", {"name": "read_transcript",
                                     "arguments": {"id": "tsc-aaa"}}))
    assert "offset=2" in out


def test_read_transcript_stops_offering_at_the_end(brain) -> None:
    brain.responses["/api/fleet/transcripts/"] = {**TURNS, "total": 2}
    out = text_of(rpc("tools/call", {"name": "read_transcript",
                                     "arguments": {"id": "tsc-aaa"}}))
    assert "offset=" not in out


# ---- creating things --------------------------------------------------------
# These write into the repo, because the VM cannot: the fleet is upload-only and
# has no checkout. The template comes from the server; the session writes it.

@pytest.fixture
def in_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


SCAFFOLD = {
    ".claude/cassandra.json": '{"id": "prj-abc", "slug": "demo"}\n',
    ".mcp.json": '{"mcpServers": {}}\n',
    "CLAUDE.md": "# Demo\n",
}


def test_project_new_writes_the_scaffold_into_the_repo(brain, in_repo) -> None:
    brain.responses["/api/projects"] = {
        "slug": "demo", "name": "Demo", "scaffold": SCAFFOLD}
    out = text_of(rpc("tools/call", {"name": "project_new",
                                     "arguments": {"name": "Demo"}}))
    assert (in_repo / ".claude/cassandra.json").exists()
    assert (in_repo / ".mcp.json").exists()
    assert "demo" in out


def test_project_new_reports_the_cwd_it_linked(brain, in_repo) -> None:
    """The session is the only thing that knows where it is running, and that is
    what stops transcripts landing in the inbox."""
    brain.responses["/api/projects"] = {"slug": "demo", "name": "Demo", "scaffold": {}}
    rpc("tools/call", {"name": "project_new", "arguments": {"name": "Demo"}})
    assert brain[-1]["body"]["cwd"] == str(in_repo)


def test_project_new_never_clobbers_an_existing_file(brain, in_repo) -> None:
    # CLAUDE.md and .gitignore usually exist already and are not ours to replace.
    (in_repo / "CLAUDE.md").write_text("mine, do not touch")
    brain.responses["/api/projects"] = {
        "slug": "demo", "name": "Demo", "scaffold": SCAFFOLD}
    out = text_of(rpc("tools/call", {"name": "project_new",
                                     "arguments": {"name": "Demo"}}))
    assert (in_repo / "CLAUDE.md").read_text() == "mine, do not touch"
    assert "Left alone" in out and "CLAUDE.md" in out


def test_module_new_writes_its_scaffold_and_start_command(brain, in_repo) -> None:
    brain.responses["/api/projects/demo/modules"] = {
        "slug": "pixel-sim", "title": "Pixel Sim",
        "scaffold": {".claude/modules/pixel-sim/reference.md": "# Pixel Sim\n",
                     ".claude/commands/start-pixel-sim.md": "brief\n"}}
    out = text_of(rpc("tools/call", {"name": "module_new",
                                     "arguments": {"slug": "demo",
                                                   "title": "Pixel Sim"}}))
    assert (in_repo / ".claude/modules/pixel-sim/reference.md").exists()
    assert (in_repo / ".claude/commands/start-pixel-sim.md").exists()
    # The next step is named, because a registered module nobody files against
    # is just an empty folder.
    assert "project_note" in out


def test_module_list_says_so_when_there_are_none(brain) -> None:
    brain.responses["/api/projects/demo/modules"] = {"modules": []}
    out = text_of(rpc("tools/call", {"name": "module_list",
                                     "arguments": {"slug": "demo"}}))
    assert "module_new" in out


def test_project_note_passes_the_module_through(brain) -> None:
    brain.responses["/api/projects/demo/ideas"] = {
        "file": "x.md", "status": "raw", "module": "pixel-sim"}
    out = text_of(rpc("tools/call", {"name": "project_note",
                                     "arguments": {"slug": "demo", "text": "sand",
                                                   "module": "pixel-sim"}}))
    assert brain[-1]["body"]["module"] == "pixel-sim"
    assert "pixel-sim" in out


def test_project_note_omits_an_empty_module(brain) -> None:
    # Capture stays one gesture: an untagged idea must not become module="".
    brain.responses["/api/projects/demo/ideas"] = {"file": "x.md", "status": "raw"}
    rpc("tools/call", {"name": "project_note",
                       "arguments": {"slug": "demo", "text": "sand"}})
    assert "module" not in brain[-1]["body"]


def test_project_new_records_the_git_remote(brain, in_repo, monkeypatch) -> None:
    """Asked of git rather than the user: the remote is already configured in
    the repo you are standing in, and a retyped link can be wrong."""
    monkeypatch.setattr(mcp, "_git_remote", lambda: "https://github.com/you/thing.git")
    brain.responses["/api/projects"] = {
        "slug": "thing", "name": "Thing", "scaffold": {},
        "code": {"kind": "git", "url": "https://github.com/you/thing.git"}}
    out = text_of(rpc("tools/call", {"name": "project_new",
                                     "arguments": {"name": "Thing"}}))
    assert brain[-1]["body"]["code"]["url"] == "https://github.com/you/thing.git"
    assert "github.com/you/thing.git" in out


def test_an_explicit_repo_beats_what_git_reports(brain, in_repo, monkeypatch) -> None:
    monkeypatch.setattr(mcp, "_git_remote", lambda: "https://github.com/you/wrong.git")
    brain.responses["/api/projects"] = {"slug": "t", "name": "T", "scaffold": {}}
    rpc("tools/call", {"name": "project_new",
                       "arguments": {"name": "T", "repo": "https://github.com/you/right.git"}})
    assert brain[-1]["body"]["code"]["url"] == "https://github.com/you/right.git"


def test_no_remote_is_not_an_error(brain, in_repo, monkeypatch) -> None:
    # A project with no repo yet is legitimate — code.kind stays "none".
    monkeypatch.setattr(mcp, "_git_remote", lambda: "")
    brain.responses["/api/projects"] = {"slug": "t", "name": "T", "scaffold": {}}
    rpc("tools/call", {"name": "project_new", "arguments": {"name": "T"}})
    assert "code" not in brain[-1]["body"]


# ---- ideas by id -----------------------------------------------------------

def test_ideas_are_listed_by_id_not_filename(brain) -> None:
    brain.responses["/api/projects/mygame"] = {"ideas": [
        {"file": "2026-08-11-a-long-dated-slug.md", "id": "ide-45ba3e2aae98",
         "title": "Alpha", "status": "raw", "priority": "low",
         "complexity": "trivial", "pinned": False},
    ]}
    out = text_of(rpc("tools/call", {"name": "project_ideas",
                                     "arguments": {"slug": "mygame"}}))
    assert "ide-45ba3e2aae98" in out
    assert "2026-08-11-a-long-dated-slug.md" not in out


def test_ideas_fall_back_to_the_filename_when_a_brain_sends_no_id(brain) -> None:
    """An older brain still lists something usable rather than `None`."""
    brain.responses["/api/projects/mygame"] = {"ideas": [
        {"file": "a.md", "title": "Alpha", "status": "raw",
         "priority": "low", "complexity": "trivial", "pinned": False},
    ]}
    out = text_of(rpc("tools/call", {"name": "project_ideas",
                                     "arguments": {"slug": "mygame"}}))
    assert "a.md" in out and "None" not in out


def test_project_idea_returns_the_body_for_an_id(brain) -> None:
    brain.responses["/api/projects/mygame"] = {"ideas": [
        {"file": "a.md", "id": "ide-45ba3e2aae98", "title": "Alpha",
         "status": "quick", "priority": "high", "complexity": "trivial",
         "captured": "2026-08-11T09:45:25Z",
         "text": "---\nid: ide-45ba3e2aae98\n---\n\nthe whole thought"},
    ]}
    out = text_of(rpc("tools/call", {"name": "project_idea",
                                     "arguments": {"slug": "mygame",
                                                   "id": "ide-45ba3e2aae98"}}))
    assert "Alpha" in out
    assert "the whole thought" in out
    assert "column: quick" in out


def test_project_idea_also_takes_a_filename(brain) -> None:
    brain.responses["/api/projects/mygame"] = {"ideas": [
        {"file": "a.md", "id": "ide-45ba3e2aae98", "title": "Alpha",
         "status": "raw", "text": "body"},
    ]}
    out = text_of(rpc("tools/call", {"name": "project_idea",
                                     "arguments": {"slug": "mygame", "id": "a.md"}}))
    assert "Alpha" in out


def test_project_idea_says_so_when_the_id_is_unknown(brain) -> None:
    brain.responses["/api/projects/mygame"] = {"ideas": []}
    out = text_of(rpc("tools/call", {"name": "project_idea",
                                     "arguments": {"slug": "mygame",
                                                   "id": "ide-000000000000"}}))
    assert "no idea" in out and "project_ideas" in out


# ---- linking an existing project -------------------------------------------
# The migration case, and the second-machine case. Running project_new for a
# project that already exists is how you end up with two, one holding all the
# history.

def test_project_link_scaffolds_without_creating(brain, in_repo) -> None:
    brain.responses["/api/projects/frontier/link"] = {
        "slug": "frontier", "name": "Project Frontier",
        "scaffold": {".claude/cassandra.json": '{"id": "prj-old"}\n'}}
    out = text_of(rpc("tools/call", {"name": "project_link",
                                     "arguments": {"slug": "frontier"}}))
    assert (in_repo / ".claude/cassandra.json").read_text() == '{"id": "prj-old"}\n'
    # It must not hit the create endpoint at all.
    assert all(c["path"] != "/api/projects" or c["method"] != "POST" for c in brain)
    assert "frontier" in out


def test_project_link_sends_this_machine_and_directory(brain, in_repo) -> None:
    brain.responses["/api/projects/frontier/link"] = {
        "slug": "frontier", "name": "F", "scaffold": {}}
    rpc("tools/call", {"name": "project_link", "arguments": {"slug": "frontier"}})
    assert brain[-1]["body"]["cwd"] == str(in_repo)


# ---- work in flight ---------------------------------------------------------

def test_work_start_defaults_to_this_machine(brain, monkeypatch) -> None:
    """Recorded, never inferred by the server: the workflow's one real hazard is
    the same branch active on two machines, and only the caller knows which."""
    monkeypatch.setattr(mcp, "load_config", lambda: {"server": "x", "machine_id": "bazzite"})
    brain.responses["/api/projects/f/work"] = {
        "slug": "b", "branch": "feature/b", "machine": "bazzite"}
    rpc("tools/call", {"name": "work_start",
                       "arguments": {"project": "f", "slug": "b", "branch": "feature/b"}})
    assert brain[-1]["body"]["machine"] == "bazzite"


def test_work_start_surfaces_the_module_clash_warning(brain) -> None:
    brain.responses["/api/projects/f/work"] = {
        "slug": "b", "branch": "feature/b", "machine": "bazzite",
        "warning": "cave-shapes is already active in module 'world-generation'"}
    out = text_of(rpc("tools/call", {"name": "work_start",
                                     "arguments": {"project": "f", "slug": "b",
                                                   "branch": "feature/b"}}))
    assert "cave-shapes" in out and "Warning" in out


def test_work_get_inlines_the_goal_and_remaining_steps(brain) -> None:
    # The point of the tool: one call to brief a session, not four.
    brain.responses["/api/projects/f/work/b"] = {
        "slug": "b", "branch": "feature/b", "machine": "bazzite", "state": "active",
        "goal": "gol-123", "goal_text": "Biomes feel distinct",
        "remaining": [{"id": "gol-a", "text": "Temperature map", "state": "todo"}]}
    out = text_of(rpc("tools/call", {"name": "work_get",
                                     "arguments": {"project": "f", "slug": "b"}}))
    assert "Biomes feel distinct" in out and "Temperature map" in out


def test_work_list_says_so_when_nothing_is_in_flight(brain) -> None:
    brain.responses["/api/work"] = {"work": []}
    assert "Nothing" in text_of(rpc("tools/call", {"name": "work_list", "arguments": {}}))


def test_work_finish_does_not_claim_the_goal_is_done(brain) -> None:
    """A branch can land without its goal being finished — marking steps done
    stays a separate roadmap_set call."""
    brain.responses["/api/projects/f/work/b/finish"] = {"slug": "b", "commit": "abc12345"}
    out = text_of(rpc("tools/call", {"name": "work_finish",
                                     "arguments": {"project": "f", "slug": "b"}}))
    assert "roadmap_set" in out
