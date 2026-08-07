#!/usr/bin/env python3
"""Cassandra fleet client — uploads Claude Code transcripts to the brain.

Deliberately thin (docs/fleet.md): the update burden is proportional to how much
logic lives out here, so behaviour comes from the server and this file changes
rarely. **Standard library only** — no venv, no pip, nothing that breaks
differently on macOS, Fedora and Windows.

Install:

    python3 cassandra_client.py register --server https://HOST --code CODE --id mac-laptop
    python3 cassandra_client.py sync            # one pass
    python3 cassandra_client.py watch           # forever, every 5 minutes

Identity lives in ~/.cassandra/client.json, mode 600. It holds a bearer token,
so it is the one file here worth protecting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.3.0"
CONFIG = Path.home() / ".cassandra" / "client.json"
DEFAULT_INTERVAL = 300
TIMEOUT = 60

# This file's own checkout — updates are `git fetch` + `git checkout <sha>`
# against the public client repo.
HERE = Path(__file__).resolve().parent

# A new version must prove itself by completing one heartbeat. If it never does
# — it crashes on import, or cannot reach the server — the watchdog puts the
# previous checkout back rather than leaving the machine looping on a broken
# build with nobody watching.
ROLLBACK_AFTER_FAILURES = 3

# Claude Code names a session file <uuid>.jsonl. The .ccr-tip.json sidecars in
# the same directory are not the conversation and are not uploaded.
TRANSCRIPT_SUFFIX = ".jsonl"


# ---- platform ---------------------------------------------------------------
# The only genuinely OS-specific part, kept as a pure function so it can be
# tested for every platform from one machine (docs/fleet.md → Testing).

def transcript_root(system: str, home: Path) -> Path:
    """Where Claude Code keeps its transcripts.

    Same location on all three today — `~/.claude/projects`. It is a function
    rather than a constant because that is exactly the assumption most likely to
    stop being true, and when it does this is the only thing that changes.
    """
    return home / ".claude" / "projects"


def decode_cwd(dirname: str) -> str:
    """Guess the working directory from Claude Code's escaped folder name.

    `-Users-wes-dev` → `/Users/wes/dev`.

    **Only a fallback.** The escaping is lossy: a directory whose own name
    contains a dash is indistinguishable from a path separator, so
    `-home-cassandra-cassandra-system` decodes to `/home/cassandra/cassandra/system`
    when the real path is `/home/cassandra/cassandra-system`. Dashes in directory
    names are common enough that routing on this alone would misfile constantly.
    Use `read_cwd`, which asks the transcript.
    """
    if not dirname.startswith("-"):
        return dirname
    return "/" + dirname[1:].replace("-", "/")


def read_cwd(data: bytes, dirname: str) -> str:
    """The working directory, taken from the transcript itself.

    Claude Code records `cwd` on its entries, which is authoritative and
    unambiguous — unlike the folder name, which has already had the information
    squeezed out of it. Falls back to decoding the folder name only when no
    entry carries one.

    Reads just the first lines: `cwd` appears within the opening few entries and
    a session file can be tens of megabytes.
    """
    for line in data.split(b"\n", 60)[:60]:
        if b'"cwd"' not in line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        cwd = entry.get("cwd") if isinstance(entry, dict) else None
        if isinstance(cwd, str) and cwd:
            return cwd
    return decode_cwd(dirname)


def is_excluded(cwd: str, exclude: list[str]) -> bool:
    """Directories the server has said not to sync.

    Not every Claude session is project work — fixing the printer, a one-off
    script. Those are noise at best, and at worst something you would rather
    was never uploaded. The list comes down the heartbeat, so changing it is one
    call rather than an edit on every machine.
    """
    for base in exclude or []:
        base = str(base).rstrip("/\\")
        if base and (cwd == base or cwd.startswith(base + "/")
                     or cwd.startswith(base + "\\")):
            return True
    return False


def local_sessions(root: Path, exclude: list[str] | None = None) -> list[dict]:
    """Every transcript on this machine, with its hash."""
    out = []
    if not root.is_dir():
        return out
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob(f"*{TRANSCRIPT_SUFFIX}")):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            cwd = read_cwd(data, folder.name)
            if is_excluded(cwd, exclude or []):
                continue
            out.append({
                "session_id": path.stem,
                "hash": hashlib.sha256(data).hexdigest(),
                "cwd": cwd,
                "dirname": folder.name,
                "bytes": len(data),
                "path": str(path),
            })
    return out


# ---- config -----------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG.exists():
        return {}
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


# ---- transport --------------------------------------------------------------

def request(cfg: dict, method: str, path: str, body: dict | None = None,
            *, auth: bool = True) -> dict:
    url = cfg["server"].rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {cfg['token']}"
        headers["X-Machine-Id"] = cfg["machine_id"]
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"{path} failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"{path} failed: cannot reach {url} ({exc.reason})") from exc


def post(cfg: dict, path: str, body: dict, *, auth: bool = True) -> dict:
    return request(cfg, "POST", path, body, auth=auth)


def get(cfg: dict, path: str) -> dict:
    return request(cfg, "GET", path)


def put(cfg: dict, path: str, body: dict) -> dict:
    return request(cfg, "PUT", path, body)


# ---- self-update ------------------------------------------------------------
# The client is a git checkout of a public repo, so there are no credentials on
# any machine. The server names the sha to run; this fetches and checks it out.
#
# "Restart" is `exit`. launchd (KeepAlive) and systemd (Restart=always) already
# supervise this process, so exiting is the simplest correct way to come back up
# on the new code — no exec juggling, no half-updated process.

def git(*args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(["git", "-C", str(HERE), *args],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{exc!r}"
    return done.returncode, (done.stdout + done.stderr).strip()


def current_sha() -> str | None:
    code, out = git("rev-parse", "HEAD")
    return out.strip() if code == 0 else None


def is_checkout() -> bool:
    return (HERE / ".git").exists()


def apply_update(want: str) -> bool:
    """Move to `want`. Returns True if the process should exit and come back."""
    if not is_checkout():
        print(f"update to {want[:8]} skipped: {HERE} is not a git checkout")
        return False

    here = current_sha()
    code, out = git("fetch", "--quiet", "origin")
    if code != 0:
        print(f"update: fetch failed — {out[:200]}")
        return False

    target = "origin/main" if want == "main" else want
    code, resolved = git("rev-parse", target)
    if code != 0:
        print(f"update: unknown target {target} — {resolved[:200]}")
        return False
    if resolved.strip() == here:
        return False

    code, out = git("checkout", "--quiet", "--force", resolved.strip())
    if code != 0:
        print(f"update: checkout failed — {out[:200]}")
        return False

    cfg = load_config()
    cfg["pending_sha"] = resolved.strip()
    cfg["pending_failures"] = 0
    cfg.setdefault("last_good_sha", here)
    save_config(cfg)
    print(f"updated {(here or '?')[:8]} → {resolved[:8]}; restarting")
    return True


def confirm_or_rollback(cfg: dict, *, healthy: bool) -> bool:
    """Promote a pending version, or put the last good one back.

    Returns True if a rollback happened and the process should exit.
    """
    pending = cfg.get("pending_sha")
    if not pending:
        return False

    if healthy:
        cfg["last_good_sha"] = pending
        cfg.pop("pending_sha", None)
        cfg.pop("pending_failures", None)
        save_config(cfg)
        print(f"version {pending[:8]} confirmed healthy")
        return False

    failures = int(cfg.get("pending_failures", 0)) + 1
    cfg["pending_failures"] = failures
    save_config(cfg)
    if failures < ROLLBACK_AFTER_FAILURES:
        print(f"version {pending[:8]} unhealthy ({failures}/{ROLLBACK_AFTER_FAILURES})")
        return False

    good = cfg.get("last_good_sha")
    if not good:
        print(f"version {pending[:8]} unhealthy and there is no known-good sha to return to")
        return False
    code, out = git("checkout", "--quiet", "--force", good)
    if code != 0:
        print(f"rollback to {good[:8]} FAILED — {out[:200]}")
        return False
    cfg.pop("pending_sha", None)
    cfg.pop("pending_failures", None)
    save_config(cfg)
    print(f"rolled back to {good[:8]} after {failures} failed heartbeats; restarting")
    return True


# ---- commands ---------------------------------------------------------------

def cmd_register(args) -> None:
    machine_id = (args.id or socket.gethostname().split(".")[0]).lower()
    cfg = {"server": args.server, "machine_id": machine_id}
    result = post(cfg, "/api/fleet/register", {
        "machine_id": machine_id,
        "code": args.code,
        "hostname": socket.gethostname(),
        "os": sys.platform,
        "payload": {"client_version": VERSION, "python": platform.python_version()},
    }, auth=False)
    cfg["token"] = result["token"]
    save_config(cfg)
    print(f"registered as {machine_id}; token saved to {CONFIG}")


def cmd_sync(args) -> int:
    cfg = load_config()
    if not cfg.get("token"):
        raise SystemExit(f"not registered — run `register` first ({CONFIG} has no token)")

    root = transcript_root(sys.platform, Path.home())
    sessions = local_sessions(root, exclude)
    if not sessions:
        print(f"no transcripts under {root}")
        return 0

    by_id = {s["session_id"]: s for s in sessions}
    want = post(cfg, "/api/fleet/sync/check",
                {"files": [{"session_id": s["session_id"], "hash": s["hash"]}
                           for s in sessions]})["want"]
    if not want:
        print(f"{len(sessions)} transcript(s), all up to date")
        return 0

    sent = 0
    for session_id in want:
        session = by_id.get(session_id)
        if session is None:
            continue
        try:
            content = Path(session["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  skip {session_id}: {exc}")
            continue
        result = post(cfg, "/api/fleet/sync/upload", {
            "session_id": session_id,
            "cwd": session["cwd"],
            "dirname": session["dirname"],
            "content": content,
        })
        where = result.get("project") or "inbox"
        print(f"  sent {session_id[:8]}… ({session['bytes']:,}B) → {where}")
        sent += 1
    print(f"uploaded {sent} of {len(sessions)} transcript(s)")
    return sent


def try_heartbeat(cfg: dict) -> dict | None:
    """Check in. Returns the server's answer, or None if it could not be reached.

    Non-fatal by design: reachability is exactly the health signal the rollback
    watchdog needs, so a failure here is data, not a crash.
    """
    try:
        return post(cfg, "/api/fleet/heartbeat",
                    {"current_sha": current_sha(), "client_version": VERSION})
    except SystemExit as exc:
        print(f"heartbeat failed: {exc}")
        return None


def cmd_update(args) -> None:
    cfg = load_config()
    if not cfg.get("token"):
        raise SystemExit("not registered — run `register` first")
    beat = try_heartbeat(cfg)
    if beat is None:
        raise SystemExit("cannot reach the server")
    want = beat.get("want_sha") or "main"
    if apply_update(want):
        print("restart the service to pick it up")
    else:
        print(f"already on the wanted version ({want})")


def cmd_watch(args) -> None:
    interval = args.interval
    print(f"watching every {interval}s — ctrl-c to stop")
    while True:
        cfg = load_config()
        beat = try_heartbeat(cfg)

        # A pending version is confirmed by a heartbeat getting through, and
        # rolled back when several do not. Do this before anything else, so a
        # broken build cannot keep failing quietly.
        if confirm_or_rollback(load_config(), healthy=beat is not None):
            return                          # supervisor restarts us on the old code

        if beat is not None:
            interval = int(beat.get("config", {}).get("interval") or interval)
            if not beat.get("config", {}).get("enabled", True):
                print("disabled by the server; idling")
                time.sleep(interval)
                continue
            if apply_update(beat.get("want_sha") or "main"):
                return                      # ditto, on the new code

            try:
                cmd_sync(args, beat.get("config", {}).get("exclude"))
            except SystemExit as exc:       # a transient outage must not end the watch
                print(f"sync failed: {exc}")
            except Exception as exc:        # noqa: BLE001 — same reasoning
                print(f"sync error: {exc!r}")
        time.sleep(interval)


# ---- projects ---------------------------------------------------------------
# Creating a project from the machine you are working on, rather than from the
# panel. The client already knows its machine id and can read the working
# directory, so it fills in the transcript mapping itself — which is the one
# thing a web form cannot do well, since it would mean typing a path you are not
# standing in, for a machine you are not on.

def require_registered() -> dict:
    cfg = load_config()
    if not cfg.get("token"):
        raise SystemExit("not registered — run `register` first")
    return cfg


def cmd_project_list(args) -> None:
    cfg = require_registered()
    projects = get(cfg, "/api/projects").get("projects", [])
    if not projects:
        print("no projects yet")
        return
    here = os.getcwd()
    for p in projects:
        mine = any(s.get("machine_id") == cfg["machine_id"]
                   and here.startswith(s.get("cwd", "\0"))
                   for s in p.get("transcript_sources") or [])
        print(f"{'*' if mine else ' '} {p['slug']:24} {p['status']:8} {p['name']}")
    if any(p for p in projects):
        print("\n* = this directory feeds that project")


def _link(cfg: dict, slug: str, cwd: str) -> None:
    """Add this machine and directory to a project's transcript sources."""
    project = get(cfg, f"/api/projects/{slug}")
    sources = project.get("transcript_sources") or []
    if any(s.get("machine_id") == cfg["machine_id"] and s.get("cwd") == cwd
           for s in sources):
        print(f"{slug} already covers {cwd}")
        return
    sources.append({"machine_id": cfg["machine_id"], "cwd": cwd})
    put(cfg, f"/api/projects/{slug}", {"transcript_sources": sources})
    print(f"{slug} ← {cwd} ({cfg['machine_id']})")


def cmd_project_create(args) -> None:
    cfg = require_registered()
    cwd = os.getcwd()
    project = post(cfg, "/api/projects", {"name": args.name})
    slug = project["slug"]
    print(f"created {slug}")

    if not args.no_link:
        _link(cfg, slug, cwd)
        # Sessions already run in this directory are sitting in the inbox with
        # nowhere to go. Creating the project is exactly the moment they should
        # be claimed, so the mapping applies backwards as well as forwards.
        moved = post(cfg, "/api/fleet/reroute", {}).get("moved", 0)
        if moved:
            print(f"claimed {moved} transcript(s) already uploaded from here")


def cmd_project_link(args) -> None:
    cfg = require_registered()
    _link(cfg, args.slug, os.getcwd())
    moved = post(cfg, "/api/fleet/reroute", {}).get("moved", 0)
    if moved:
        print(f"claimed {moved} transcript(s)")


def cmd_status(args) -> None:
    cfg = load_config()
    root = transcript_root(sys.platform, Path.home())
    sessions = local_sessions(root)
    sha = current_sha()
    print(f"client      {VERSION} on {sys.platform}")
    print(f"checkout    {HERE} ({(sha or 'not a git checkout')[:12]})")
    if cfg.get("pending_sha"):
        print(f"pending     {cfg['pending_sha'][:12]} "
              f"(failures: {cfg.get('pending_failures', 0)})")
    if cfg.get("last_good_sha"):
        print(f"last good   {cfg['last_good_sha'][:12]}")
    print(f"config      {CONFIG} ({'registered' if cfg.get('token') else 'NOT registered'})")
    print(f"server      {cfg.get('server', '-')}")
    print(f"machine id  {cfg.get('machine_id', '-')}")
    print(f"transcripts {len(sessions)} under {root}")
    for s in sessions[:10]:
        print(f"  {s['session_id'][:8]}…  {s['bytes']:>9,}B  {s['cwd']}")
    if len(sessions) > 10:
        print(f"  … and {len(sessions) - 10} more")


def main() -> None:
    # Under launchd or systemd, stdout is a file rather than a terminal, and
    # Python block-buffers it. A process that prints and then sleeps for five
    # minutes therefore writes nothing at all — launchd does not even create the
    # log file, which reads as "the service never started". Line buffering makes
    # the log a live view of what the client is doing.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except AttributeError:      # pragma: no cover — Python < 3.7
            pass

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", help="enrol this machine")
    p.add_argument("--server", required=True, help="e.g. https://cassandra-server.tailnet.ts.net")
    p.add_argument("--code", required=True, help="enrollment code from the brain's log")
    p.add_argument("--id", help="machine id (default: this hostname)")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("sync", help="upload anything the server does not have")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("watch", help="sync on a loop")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("update", help="move to the version the server pins")
    p.set_defaults(func=cmd_update)

    # `project` — create or link a project from the directory you are in, which
    # is where a project actually starts.
    projects = sub.add_parser("project", help="create or link a project from here")
    psub = projects.add_subparsers(dest="project_command", required=True)

    q = psub.add_parser("create", help="create a project and feed it this directory")
    q.add_argument("name")
    q.add_argument("--no-link", action="store_true",
                   help="do not map this directory to it")
    q.set_defaults(func=cmd_project_create)

    q = psub.add_parser("link", help="feed this directory to an existing project")
    q.add_argument("slug")
    q.set_defaults(func=cmd_project_link)

    q = psub.add_parser("list", help="projects, marking the one this directory feeds")
    q.set_defaults(func=cmd_project_list)

    p = sub.add_parser("status", help="what this machine holds and where it points")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
