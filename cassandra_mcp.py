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

VERSION = "0.2.0"
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
      "use this whenever the user says something worth keeping.",
      params={"slug": SLUG,
              "text": {"type": "string", "description": "The idea, in the user's own words."}},
      required=("slug", "text"))
def project_note(slug: str, text: str) -> str:
    result = call_brain("POST", f"/api/projects/{slug}/ideas", {"text": text})
    return f"Saved to {slug} as {result['file']}."


def _read(slug: str, path: str) -> str | None:
    try:
        return call_brain("GET", f"/api/projects/{slug}/file", params={"path": path}).get("text")
    except RuntimeError:
        return None


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
