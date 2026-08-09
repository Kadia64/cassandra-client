#!/usr/bin/env python3
"""Cassandra MCP server — project context, in any Claude session.

An inversion of the usual direction: elsewhere Cassandra *consumes* MCP servers
(docs/atk.md); here it **is** one, so a Claude Code session on any registered
machine can read a project's context without being told about it.

Deliberately small. This is for **context, not development** — reading what a
project is and where it stands, and firing an idea at it. Iterating on code is
what the session itself is for.

Configure it in a project's `.mcp.json`, or globally:

    {
      "mcpServers": {
        "cassandra": {
          "command": "python3",
          "args": ["/Users/you/.cassandra/client/cassandra_mcp.py"]
        }
      }
    }

Talks to the brain over HTTP using the machine's existing fleet credentials
(`~/.cassandra/client.json`), so there is nothing extra to configure and no new
secret. Standard library only, same discipline as the client.

Transport is stdio: newline-delimited JSON-RPC 2.0, request on stdin, response
on stdout. **Nothing may print to stdout except protocol messages** — a stray
print corrupts the stream. Diagnostics go to stderr.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

VERSION = "0.3.0"
CONFIG = Path.home() / ".cassandra" / "client.json"
TIMEOUT = 30

# Echoed back to the client when it asks for one we know. Listing several rather
# than pinning one means a Claude Code upgrade does not silently break the
# server the day the protocol revs.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
FALLBACK_PROTOCOL = SUPPORTED_PROTOCOLS[0]


def log(message: str) -> None:
    """Diagnostics — stderr only. stdout belongs to the protocol."""
    print(f"[cassandra-mcp] {message}", file=sys.stderr, flush=True)


# ---- talking to the brain ---------------------------------------------------

def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def call_brain(method: str, path: str, body: dict | None = None,
               params: dict | None = None) -> Any:
    cfg = load_config()
    if not cfg.get("server"):
        raise RuntimeError(
            f"this machine is not registered with Cassandra ({CONFIG} has no server). "
            f"Run `cassandra_client.py register` first.")
    url = cfg["server"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Content-Type": "application/json"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
        headers["X-Machine-Id"] = cfg.get("machine_id", "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code} from {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url} ({exc.reason})") from exc


# ---- the tool registry ------------------------------------------------------
# Same shape as the brain's own registry (src/ai/tools/registry.py): the schema
# is declared next to the implementation and the JSON the model sees is
# generated from it. Adding a tool is one decorated function and nothing else.

TOOLS: list[dict[str, Any]] = []


def tool(name: str, description: str, *,
         params: dict[str, dict[str, Any]] | None = None,
         required: tuple[str, ...] = ()) -> Callable:
    def wrap(fn: Callable[..., str]) -> Callable[..., str]:
        TOOLS.append({
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": params or {},
                "required": list(required),
            },
            "fn": fn,
        })
        return fn
    return wrap


SLUG = {"type": "string", "description": "The project's slug, from project_list."}

# The board's vocabularies, mirrored from src/ai/projects.py. Spelled out in the
# tool schemas rather than described in prose, so a model picks from a list
# instead of guessing a word the brain will reject.
COLUMNS = ("raw", "quick", "big", "done")
PRIORITIES = ("high", "normal", "low")
COMPLEXITIES = ("trivial", "small", "medium", "large")

COLUMN = {"type": "string", "enum": list(COLUMNS),
          "description": "Which board column: raw (unsorted), quick (small and "
                         "obvious), big (needs expanding), done."}


@tool("project_list", "List the projects Cassandra knows about, with their status.")
def project_list() -> str:
    projects = call_brain("GET", "/api/projects").get("projects", [])
    if not projects:
        return "No projects yet."
    lines = []
    for p in projects:
        code = (p.get("code") or {}).get("kind", "none")
        lines.append(f"{p['slug']}  [{p['status']}, code:{code}]  {p['name']}"
                     + (f" — {p['description']}" if p.get("description") else ""))
    return "\n".join(lines)


@tool("project_context",
      "What a project is and where it stands: its overview and current status. "
      "Read this first when picking up work on a project.",
      params={"slug": SLUG}, required=("slug",))
def project_context(slug: str) -> str:
    project = call_brain("GET", f"/api/projects/{slug}")
    parts = [f"# {project['name']} ({slug})",
             f"status: {project['status']}"]
    if project.get("description"):
        parts.append(project["description"])
    code = project.get("code") or {}
    if code.get("kind") and code["kind"] != "none":
        where = code.get("url") or code.get("path") or ""
        machine = f" on {code['machine_id']}" if code.get("machine_id") else ""
        parts.append(f"code: {code['kind']}{machine} {where}".rstrip())
    for name in ("overview.md", "status.md"):
        found = _read(slug, f"context/{name}")
        if found:
            parts.append(f"\n## context/{name}\n{found}")
    return "\n".join(parts)


@tool("project_read",
      "Read one file from a project — use project_files to see what is there.",
      params={"slug": SLUG,
              "path": {"type": "string",
                       "description": "Path within the project, e.g. 'context/overview.md'."}},
      required=("slug", "path"))
def project_read(slug: str, path: str) -> str:
    found = _read(slug, path)
    return found if found is not None else f"No file '{path}' in {slug}."


@tool("project_files", "List the files in a project. Transcripts are summarised, not listed.",
      params={"slug": SLUG}, required=("slug",))
def project_files(slug: str) -> str:
    files = call_brain("GET", f"/api/projects/{slug}/files").get("files", [])
    if not files:
        return f"{slug} has no files yet."
    return "\n".join(f"{f['path']}  ({f['bytes']:,}B)" for f in files)


@tool("project_recent",
      "What was last worked on in a project and what is next — read this when starting "
      "a session, before asking the user to recap.",
      params={"slug": SLUG}, required=("slug",))
def project_recent(slug: str) -> str:
    parts = []
    status = _read(slug, "context/status.md")
    if status:
        parts.append(f"## status\n{status}")
    # The roadmap belongs in the session kickoff more than anywhere else: it is
    # literally "what is next", and a session that has to be told to go and look
    # for it will not. Folded in rather than left to a separate call.
    try:
        parts.append(f"## roadmap\n{_roadmap_text(slug)}")
    except RuntimeError:
        pass                            # no roadmap yet — not worth a line
    ideas = call_brain("GET", f"/api/projects/{slug}").get("ideas", [])
    if ideas:
        parts.append("## recent ideas")
        for idea in ideas[:5]:
            parts.append(f"- {idea['file']}\n{idea['text'][:400]}")
    return "\n\n".join(parts) or f"Nothing recorded yet for {slug}."


@tool("project_set_status",
      "Update where a project stands and what is next. Call this at the END of a working session, "
      "before finishing — a few sentences on what was done, what remains, and anything blocked. "
      "It replaces the previous status, and it is what the next session reads to pick up.",
      params={"slug": SLUG,
              "stands": {"type": "string",
                         "description": "Where the project is now, in a few sentences."},
              "next": {"type": "string",
                       "description": "What to do next, as a short list."},
              "blocked": {"type": "string",
                          "description": "Anything waiting on a decision or someone else. "
                                         "Omit if nothing is."}},
      required=("slug", "stands", "next"))
def project_set_status(slug: str, stands: str, next: str, blocked: str = "") -> str:
    """A handoff, not a log.

    Overwritten rather than appended: history already lives in the transcripts
    and in git, and a status that accumulates stops being the thing you can read
    in ten seconds to find out where you are. Three headings, deliberately —
    more structure would invite it to grow.
    """
    body = (f"# Status\n\n_Updated {_today()}_\n\n"
            f"## Where it stands\n\n{stands.strip()}\n\n"
            f"## What's next\n\n{next.strip()}\n")
    if blocked.strip():
        body += f"\n## Blocked\n\n{blocked.strip()}\n"
    call_brain("PUT", f"/api/projects/{slug}/file",
               {"path": "context/status.md", "text": body})
    return f"Status updated for {slug}."


def _today() -> str:
    # The brain stamps everything else; this is the one string written from a
    # client machine, so it uses the machine's own date.
    import datetime
    return datetime.date.today().isoformat()


@tool("project_note",
      "Capture an idea against a project. The text is stored verbatim, immediately — "
      "use this whenever the user says something worth keeping. The three "
      "classifiers are optional: omit them unless the user actually said where "
      "the idea belongs, and it lands unsorted in `raw` at the bottom of both "
      "scales, which is the honest default for a thought nobody has assessed.",
      params={"slug": SLUG,
              "text": {"type": "string", "description": "The idea, in the user's own words."},
              "column": COLUMN,
              "priority": {"type": "string", "enum": list(PRIORITIES),
                           "description": "How much it matters."},
              "complexity": {"type": "string", "enum": list(COMPLEXITIES),
                             "description": "How much work it looks like."},
              "module": {"type": "string",
                         "description": "Which module the idea is about, from "
                                        "module_list. Omit for the project as "
                                        "a whole."}},
      required=("slug", "text"))
def project_note(slug: str, text: str, column: str = "", priority: str = "",
                 complexity: str = "", module: str = "") -> str:
    body: dict[str, Any] = {"text": text}
    if column:
        body["status"] = column
    if priority:
        body["priority"] = priority
    if complexity:
        body["complexity"] = complexity
    if module:
        body["module"] = module
    result = call_brain("POST", f"/api/projects/{slug}/ideas", body)
    where = result.get("status", "raw")
    tagged = f", module: {result['module']}" if result.get("module") else ""
    return f"Saved to {slug} as {result['file']} (column: {where}{tagged})."


@tool("project_ideas",
      "The project's idea board, grouped by column. Read this before adding an "
      "idea that might already be there, or to answer what is queued up.",
      params={"slug": SLUG,
              "column": {"type": "string", "enum": list(COLUMNS),
                         "description": "Only this column. Omit for the whole board."}},
      required=("slug",))
def project_ideas(slug: str, column: str = "") -> str:
    ideas = call_brain("GET", f"/api/projects/{slug}").get("ideas", [])
    wanted = (column,) if column else COLUMNS
    out = []
    for name in wanted:
        rows = [i for i in ideas if i.get("status") == name]
        out.append(f"## {name} ({len(rows)})")
        for i in rows:
            pin = "PINNED " if i.get("pinned") else ""
            out.append(f"- {pin}{i['title']}  [{i.get('priority')}/{i.get('complexity')}]"
                       f"  {i['file']}")
        if not rows:
            out.append("- (empty)")
    return "\n".join(out)


@tool("project_idea_set",
      "Move an idea to another column, or change its priority, complexity or pin. "
      "Only the fields you pass are changed.",
      params={"slug": SLUG,
              "file": {"type": "string",
                       "description": "The idea's filename, from project_ideas."},
              "column": COLUMN,
              "priority": {"type": "string", "enum": list(PRIORITIES),
                           "description": "How much it matters."},
              "complexity": {"type": "string", "enum": list(COMPLEXITIES),
                             "description": "How much work it looks like."},
              "pinned": {"type": "boolean",
                         "description": "Hold it at the top of its column."}},
      required=("slug", "file"))
def project_idea_set(slug: str, file: str, column: str = "", priority: str = "",
                     complexity: str = "", pinned: bool | None = None) -> str:
    body: dict[str, Any] = {}
    if column:
        body["status"] = column
    if priority:
        body["priority"] = priority
    if complexity:
        body["complexity"] = complexity
    if pinned is not None:
        body["pinned"] = pinned
    if not body:
        return "Nothing to change — pass at least one of column, priority, complexity, pinned."
    result = call_brain("PUT", f"/api/projects/{slug}/ideas/{file}", body)
    return (f"{result['title']} → column:{result['status']} "
            f"priority:{result['priority']} complexity:{result['complexity']}"
            + (" pinned" if result.get("pinned") else ""))


def _read(slug: str, path: str) -> str | None:
    try:
        return call_brain("GET", f"/api/projects/{slug}/file", params={"path": path}).get("text")
    except RuntimeError:
        return None


# ---- roadmaps ---------------------------------------------------------------
# What a project is trying to get done. A roadmap is a flat ordered list of
# goals; a goal that has been built out expands into named sub-roadmaps, and
# that is the only hierarchy — goals never nest inside goals.
#
# Four tools, not six. Listing folds into `roadmap_read` (reading `main` already
# names every sub-roadmap) and reordering folds into `roadmap_set`, because the
# server takes `index` on the same call. This server is meant to stay small.
#
# There is deliberately **no delete**. `roadmap_set(state="dropped")` retires a
# goal and keeps it in the history; erasing one is a panel action. An agent that
# can quietly remove the record of a decision is a worse trade than an agent
# that leaves a struck-through line behind.

ROADMAP = {"type": "string",
           "description": "Roadmap name. Omit for 'main', the project's top-level roadmap. "
                          "Sub-roadmap names come from roadmap_read."}
GOAL = {"type": "string",
        "description": "The goal's id, e.g. 'gol-a1b2c3d4e5f6', exactly as roadmap_read shows it."}

STATE_BOX = {"todo": " ", "doing": ">", "blocked": "!", "done": "x", "dropped": "~"}


def _roadmap_text(slug: str, name: str = "main") -> str:
    """One roadmap as a listing the model can act on.

    Rendered here rather than using the brain's `?format=md`, for one reason:
    that render is for reading and carries no ids, and every write tool needs
    the goal's id. So ids are on every line, in full — a truncated id would be
    copied straight into a call that then fails.
    """
    data = call_brain("GET", f"/api/projects/{slug}/roadmaps/{name}")
    progress = data.get("progress") or {}
    lines = [f"# {data.get('title', name)} — roadmap '{data.get('slug', name)}' "
             f"({progress.get('done', 0)}/{progress.get('total', 0)} done)"]

    parent = data.get("parent")
    if parent:
        lines.append(f"(a sub-roadmap of '{parent['roadmap']}')")
    lines.append("")

    goals = data.get("goals") or []
    if not goals:
        lines.append("(no goals yet — roadmap_add puts one here)")
    for goal in goals:
        box = STATE_BOX.get(goal.get("state", "todo"), " ")
        line = f"- [{box}] {goal.get('text', '')}   {goal['id']}"
        lines.append(line)
        if goal.get("note"):
            lines.append(f"        note: {goal['note']}")
        for child in goal.get("children") or []:
            done = (child.get("progress") or {}).get("done", 0)
            total = (child.get("progress") or {}).get("total", 0)
            lines.append(f"        → sub-roadmap '{child['name']}' ({done}/{total} done)"
                         + ("  [MISSING]" if child.get("missing") else ""))
    return "\n".join(lines)


@tool("roadmap_read",
      "The project's roadmap: its goals, which are done, and which have been broken out into "
      "sub-roadmaps. Read this when picking up work, before proposing what to do next. "
      "Every goal line ends with the id you pass to roadmap_set and roadmap_expand.",
      params={"slug": SLUG, "roadmap": ROADMAP}, required=("slug",))
def roadmap_read(slug: str, roadmap: str = "main") -> str:
    try:
        return _roadmap_text(slug, roadmap)
    except RuntimeError as exc:
        if "404" in str(exc):
            return (f"{slug} has no roadmap '{roadmap}' yet. "
                    f"roadmap_add creates 'main' the first time you add a goal to it.")
        raise


@tool("roadmap_add",
      "Add a goal to a roadmap. On 'main' these are the project's top-level goals — a thing to "
      "achieve, which may be a rough idea; on a sub-roadmap they are the implementation steps. "
      "Use this when the user describes something they want done. Creates 'main' if it is the "
      "project's first goal.",
      params={"slug": SLUG, "roadmap": ROADMAP,
              "text": {"type": "string",
                       "description": "The goal, in one line, in the user's own terms."},
              "note": {"type": "string",
                       "description": "Optional detail — a constraint, an approach, a caveat."}},
      required=("slug", "text"))
def roadmap_add(slug: str, text: str, roadmap: str = "main", note: str = "") -> str:
    body: dict[str, Any] = {"text": text}
    if note:
        body["note"] = note
    goal = call_brain("POST", f"/api/projects/{slug}/roadmaps/{roadmap}/goals", body)
    return f"Added to '{roadmap}': {goal['text']}   {goal['id']}"


@tool("roadmap_set",
      "Change a goal: check it off, move it back, reword it, or reorder it. "
      "state is one of todo, doing, blocked, done, dropped — 'dropped' is how a goal is "
      "abandoned, and keeps it on the record struck through. Call this as work actually "
      "completes, not in a batch at the end.",
      params={"slug": SLUG, "roadmap": ROADMAP, "goal": GOAL,
              "state": {"type": "string", "enum": list(STATE_BOX),
                        "description": "The goal's new state."},
              "text": {"type": "string", "description": "Reword the goal."},
              "note": {"type": "string", "description": "Replace the goal's note."},
              "position": {"type": "integer",
                           "description": "Move the goal to this 0-based position."}},
      required=("slug", "goal"))
def roadmap_set(slug: str, goal: str, roadmap: str = "main", state: str = "",
                text: str = "", note: str = "", position: int | None = None) -> str:
    body: dict[str, Any] = {}
    if state:
        body["state"] = state
    if text:
        body["text"] = text
    if note:
        body["note"] = note
    if position is not None:
        body["index"] = position
    if not body:
        return "Nothing to change — pass state, text, note or position."
    updated = call_brain("PUT", f"/api/projects/{slug}/roadmaps/{roadmap}/goals/{goal}", body)
    return f"[{STATE_BOX.get(updated['state'], ' ')}] {updated['text']}"


@tool("roadmap_expand",
      "Turn a goal into a sub-roadmap of its own — the move for 'this is no longer just an "
      "idea, here is how it breaks down'. Returns the new roadmap's name; pass that as "
      "`roadmap` to roadmap_add to fill in the steps. A goal can hold several, for working on "
      "more than one part of it at once.",
      params={"slug": SLUG, "roadmap": ROADMAP, "goal": GOAL,
              "title": {"type": "string",
                        "description": "Title for the sub-roadmap. Its short name is derived "
                                       "from this, so keep it specific."}},
      required=("slug", "goal"))
def roadmap_expand(slug: str, goal: str, roadmap: str = "main", title: str = "") -> str:
    body = {"title": title} if title else {}
    sub = call_brain("POST",
                     f"/api/projects/{slug}/roadmaps/{roadmap}/goals/{goal}/expand", body)
    return (f"Created sub-roadmap '{sub['slug']}' ({sub['title']}). "
            f"Add its steps with roadmap_add(slug='{slug}', roadmap='{sub['slug']}', ...).")


# ---- creating things --------------------------------------------------------
# The VM cannot write into your repo — the fleet is upload-only and has no
# checkout — so these tools register the thing centrally and hand back the files
# for *this* session to write. That keeps one template on the server (eight
# projects cannot drift into eight templates) while the files still live where
# git can carry them between machines.


def _write_scaffold(scaffold: dict[str, str]) -> list[str]:
    """Write `{relative path: contents}` under the cwd. Never overwrites.

    Skipping rather than clobbering matters for `.gitignore` and `CLAUDE.md`,
    which often exist already and are not ours to replace.
    """
    written = []
    for relative, body in sorted(scaffold.items()):
        target = Path.cwd() / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        written.append(relative)
    return written


@tool("project_new",
      "Register the repository you are working in as a Cassandra project, and "
      "scaffold it. Creates the project centrally, links this machine and "
      "directory so its Claude Code transcripts file themselves, and writes "
      ".claude/cassandra.json, .mcp.json and CLAUDE.md into the repo. Run this "
      "from the repo root.",
      params={
          "name": {"type": "string",
                   "description": "What the project is called, in your words. "
                                  "The slug is derived from it; the folder and "
                                  "GitHub names do not have to match."},
          "description": {"type": "string", "description": "One or two sentences."},
      },
      required=("name",))
def project_new(name: str, description: str = "") -> str:
    cfg = load_config()
    project = call_brain("POST", "/api/projects", {
        "name": name, "description": description,
        "machine_id": cfg.get("machine_id", ""), "cwd": str(Path.cwd()),
    })
    written = _write_scaffold(project.get("scaffold") or {})
    lines = [f"Created project '{project['slug']}' ({project['name']}).",
             f"Linked {cfg.get('machine_id', '?')}:{Path.cwd()} — transcripts "
             f"from here now file themselves, including ones already uploaded."]
    if written:
        lines.append("Wrote: " + ", ".join(written))
    skipped = sorted(set(project.get("scaffold") or {}) - set(written))
    if skipped:
        lines.append("Left alone (already present): " + ", ".join(skipped))
    lines.append("Commit these — .claude/cassandra.json is what identifies this "
                 "repo, so cloning it elsewhere needs no further setup.")
    return "\n".join(lines)


@tool("module_new",
      "Start a new module — a durable area of work inside a project, like "
      "'pixel simulation' or 'world generation'. Registers it and writes its "
      "reference, decisions and checkpoints scaffold plus a /start-<module> "
      "command into this repo.",
      params={"slug": SLUG,
              "title": {"type": "string",
                        "description": "The module's name, in your words."},
              "description": {"type": "string",
                              "description": "One line on what it covers."}},
      required=("slug", "title"))
def module_new(slug: str, title: str, description: str = "") -> str:
    module = call_brain("POST", f"/api/projects/{slug}/modules",
                        {"title": title, "description": description})
    written = _write_scaffold(module.get("scaffold") or {})
    return "\n".join([
        f"Module '{module['slug']}' ({module['title']}) registered on {slug}.",
        ("Wrote: " + ", ".join(written)) if written else "No files written.",
        f"Ideas can now be filed against it: "
        f"project_note(slug='{slug}', module='{module['slug']}', text=...).",
    ])


@tool("module_list",
      "The modules of a project — the areas of work it is divided into.",
      params={"slug": SLUG}, required=("slug",))
def module_list(slug: str) -> str:
    modules = call_brain("GET", f"/api/projects/{slug}/modules").get("modules", [])
    if not modules:
        return f"{slug} has no modules yet. Create one with module_new."
    lines = []
    for m in modules:
        detail = f"{m['checkpoints']} checkpoint(s)"
        if m.get("description"):
            detail += f" — {m['description']}"
        lines.append(f"{m['slug']:<22} {detail}")
    return "\n".join(lines)


# ---- the transcript corpus --------------------------------------------------
# Every conversation Cassandra holds, whatever it came from: Claude Code
# sessions synced off each machine, and claude.ai history imported from an
# account export. One corpus with a `source` facet, so a search spans all of it
# by default (migration 0006).
#
# Deliberately *not* project-scoped, unlike everything above. Most of a web
# history is not project work, and making the corpus reachable only through a
# project would hide the majority of it.
#
# If these ever need to be handed to something that must not touch project
# state, this section is the seam to lift into a read-only server of its own —
# the split is about write permissions, not about topic.

@tool("search_transcripts",
      "Search every past conversation — Claude Code sessions from any machine, "
      "and claude.ai chat history. Use this to check whether a problem, "
      "decision or idea has come up before, and to find what was concluded. "
      "Returns the best-matching conversations with a snippet of the match; "
      "read_transcript opens one in full.",
      params={
          "query": {"type": "string",
                    "description": "Words to look for. Supports SQLite FTS5 "
                                   "syntax: quote a phrase, AND/OR/NOT, "
                                   "trailing * to match a prefix."},
          "source": {"type": "string", "enum": ["claude_code", "claude_web"],
                     "description": "Restrict to one source. Omit to search everything."},
          "project": {"type": "string",
                      "description": "Restrict to one project's slug. Most "
                                     "claude.ai chats belong to no project."},
          "since": {"type": "string",
                    "description": "Only conversations on or after this date, "
                                   "as YYYY-MM-DD."},
          "limit": {"type": "integer", "description": "Max results (default 10)."},
      },
      required=("query",))
def search_transcripts(query: str, source: str = "", project: str = "",
                       since: str = "", limit: int = 10) -> str:
    params: dict[str, Any] = {"q": query, "limit": limit}
    for key, value in (("source", source), ("project", project), ("since", since)):
        if value:
            params[key] = value
    found = call_brain("GET", "/api/transcripts/search", params=params)
    hits = found.get("results", [])
    if not hits:
        return f"No conversation mentions {query!r}."

    lines = [f"{len(hits)} conversation(s) matching {query!r}:", ""]
    for hit in hits:
        when = (hit.get("started_at") or hit.get("received_at") or "")[:10]
        where = hit.get("project_slug") or "unfiled"
        title = hit.get("title") or "(untitled)"
        lines.append(f"[{hit['id']}] {when}  {hit['source']}  {where}")
        lines.append(f"  {title}")
        # The snippet carries « » around the terms that matched, which is how
        # you tell a real hit from an incidental one without opening the file.
        lines.append(f"  {(hit.get('snippet') or '').strip()}")
        lines.append("")
    lines.append("Open one in full with read_transcript(id='tsc-...').")
    return "\n".join(lines)


@tool("read_transcript",
      "Read one past conversation, by the id search_transcripts returned. "
      "Long conversations are paged — pass offset to continue.",
      params={
          "id": {"type": "string", "description": "The 'tsc-...' id from search_transcripts."},
          "offset": {"type": "integer", "description": "First turn to return (default 0)."},
          "limit": {"type": "integer", "description": "How many turns (default 40)."},
      },
      required=("id",))
def read_transcript(id: str, offset: int = 0, limit: int = 40) -> str:
    got = call_brain("GET", f"/api/fleet/transcripts/{id}",
                     params={"offset": offset, "limit": limit})
    turns = got.get("turns", [])
    if not turns:
        return f"No turns at offset {offset}."

    out = [f"{got.get('title') or '(untitled)'} — turns {offset}–{offset + len(turns)} "
           f"of {got.get('total', '?')}", ""]
    for turn in turns:
        said = []
        for block in turn.get("blocks", []):
            kind = block.get("kind")
            if kind in ("text", "thinking") and block.get("text"):
                said.append(block["text"])
            elif kind == "tool_use":
                said.append(f"[tool: {block.get('name')}]")
        if said:
            out.append(f"--- {turn.get('role')}")
            out.append("\n".join(said))
    if offset + len(turns) < got.get("total", 0):
        out.append("")
        out.append(f"… continue with read_transcript(id='{id}', offset={offset + len(turns)}).")
    return "\n".join(out)


# ---- JSON-RPC ---------------------------------------------------------------

def handle(message: dict) -> dict | None:
    """One request in, one response out. None for notifications."""
    method = message.get("method")
    mid = message.get("id")

    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        return _ok(mid, {
            "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else FALLBACK_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "cassandra", "version": VERSION},
        })

    # Notifications carry no id and must not be answered at all.
    if mid is None:
        return None

    if method == "ping":
        return _ok(mid, {})

    if method == "tools/list":
        return _ok(mid, {"tools": [{k: v for k, v in t.items() if k != "fn"}
                                   for t in TOOLS]})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        found = next((t for t in TOOLS if t["name"] == name), None)
        if found is None:
            return _err(mid, -32602, f"unknown tool: {name}")
        try:
            text = found["fn"](**(params.get("arguments") or {}))
        except TypeError as exc:
            return _ok(mid, _content(f"Bad arguments for {name}: {exc}", error=True))
        except Exception as exc:      # noqa: BLE001
            # A tool failure is a result the model should see and can act on,
            # not a protocol error that kills the session.
            log(f"{name} failed: {exc!r}")
            return _ok(mid, _content(f"{name} failed: {exc}", error=True))
        return _ok(mid, _content(text))

    return _err(mid, -32601, f"method not found: {method}")


def _content(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _ok(mid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main() -> None:
    log(f"v{VERSION} ready, {len(TOOLS)} tools")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log(f"ignoring unparseable line: {line[:120]}")
            continue
        try:
            response = handle(message)
        except Exception as exc:      # noqa: BLE001 — never die on one bad message
            log(f"handler crashed: {exc!r}")
            response = _err(message.get("id"), -32603, str(exc))
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
