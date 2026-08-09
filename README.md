# Cassandra fleet client

Uploads Claude Code transcripts from this machine to the Cassandra brain, and
keeps itself up to date.

One file, **standard library only** — no venv, no pip, no dependency that
breaks differently on macOS, Fedora and Windows. Design and rationale:
`docs/fleet.md` in the brain repo.

**Public on purpose.** There are no secrets here; identity arrives separately at
`register` and lives in `~/.cassandra/client.json`. That is what lets every
machine clone it without a deploy key.

## What it does

- finds every `~/.claude/projects/*/<session>.jsonl`
- reads each session's real working directory **from inside the transcript**
  (`cwd`), because the folder name is lossy — `-Users-wes-Desktop-Project-Frontier`
  cannot be told apart from `/Users/wes/Desktop/Project/Frontier`
- offers `{session_id, hash}` and uploads only what the server asks for
- the **server** decides which project each session belongs to, so a mapping can
  be added later and applied to what already arrived
- checks the version the server pins and updates itself to it

## Install

Needs `python3` and `git`. On macOS both come with the Xcode command line
tools — `xcode-select --install` if `python3 --version` fails.

```bash
git clone https://github.com/Kadia64/cassandra-client ~/.cassandra/client

python3 ~/.cassandra/client/cassandra_client.py register \
  --server https://cassandra-server.tailfdafb1.ts.net \
  --code   <enrollment code> \
  --id     mac-laptop

python3 ~/.cassandra/client/cassandra_client.py status   # look before you sync
python3 ~/.cassandra/client/cassandra_client.py sync
```

The enrollment code is printed by the brain at startup:
`journalctl -u cassandra-brain | grep 'enrollment code'`.

Then install the supervisor from `install/` — the plist for macOS, the unit file
for Linux. Both have their steps in the header.

## Commands

| command | what it does |
|---|---|
| `register` | enrol this machine and store its token |
| `status` | what this machine holds, and which version it is on |
| `sync` | one pass: offer hashes, upload what is wanted |
| `update` | move to the version the server pins |
| `watch` | heartbeat, self-update, sync — on a loop. What the service runs |

## Updates

Clients follow a **sha the server pins**, not `main`. That separates pushing
from deploying: commit and push all day with nothing happening to your machines,
then promote when you mean to.

```bash
# on any machine
curl -X PUT https://cassandra-server.tailfdafb1.ts.net/api/fleet/pin \
  -H 'Content-Type: application/json' -d '{"pin":"<sha>"}'
```

Rolling back is the same call with the previous sha. Nothing is reverted in git.

**Restarting is exiting.** The client checks out the new commit and stops;
launchd (`KeepAlive`) or systemd (`Restart=always`) brings it back on the new
code. That is why those settings are not optional.

**If a new version cannot heartbeat three times running**, the client checks the
last known-good commit back out and restarts itself. A bad promotion costs a
couple of minutes on one machine, not a walk round the house with a keyboard.

Set the pin to `main` on a machine you are sitting at and it tracks the branch —
the canary, so a bad commit reaches one machine you are already watching.

## Files

- `~/.cassandra/client/` — this checkout
- `~/.cassandra/client.json` — server, machine id, bearer token, version state.
  Mode 600; the only thing here worth protecting.
- `~/.cassandra/client.log` — macOS only; Linux logs to the journal

## Tests

```bash
python3 -m pytest test_client.py
```

Covers the parts that are platform-specific or lossy — path handling, cwd
recovery, the rollback watchdog — so every platform is checked from one machine.

## MCP server — project context in any Claude session

`cassandra_mcp.py` exposes Cassandra's projects to Claude Code as tools, so a
session can read what a project is and where it stands without being told.

Add to `~/.claude.json` (global) or a project's `.mcp.json`:

```json
{
  "mcpServers": {
    "cassandra": {
      "command": "python3",
      "args": ["/Users/YOU/.cassandra/client/cassandra_mcp.py"]
    }
  }
}
```

Nothing else to configure — it reuses this machine's fleet credentials.

| tool | what it gives you |
|---|---|
| `project_list` | every project and its status |
| `project_context` | what a project is, where it stands, where its code lives |
| `project_recent` | what was last worked on and what is next |
| `project_files` | what is in the project |
| `project_read` | one file from it |
| `project_note` | capture an idea against it, optionally straight into a column |
| `project_ideas` | the idea board, grouped by column |
| `project_idea_set` | move a card, or change its priority, complexity or pin |
| `project_set_status` | write the handoff at the end of a session |
| `roadmap_read` | the project's goals, what is done, what is broken out |
| `roadmap_add` | add a goal, to `main` or to a sub-roadmap |
| `roadmap_set` | check off, backtrack, reword, reorder |
| `roadmap_expand` | turn a goal into a sub-roadmap of its own |
| `search_transcripts` | search every past conversation — Claude Code on any machine, and claude.ai history |
| `read_transcript` | open one of them in full |

**Context and planning, not development.** Reading a project, keeping its
roadmap honest, and firing an idea at it — the session itself is for the actual
work.

The idea board's four columns are `raw` (unsorted), `quick` (small and obvious),
`big` (needs expanding) and `done`. `project_note` takes them, plus `priority`
and `complexity`, but **all three are optional and should usually be omitted**:
an idea nobody has assessed belongs in `raw` at the bottom of both scales, and a
capture that stops to classify is a capture you stop reaching for.

Roadmaps have **no delete tool**, deliberately. `roadmap_set(state="dropped")`
retires a goal and leaves it struck through; erasing one is a panel action. An
agent that can quietly remove the record of a decision is a worse trade than one
that leaves a line behind.

Adding a tool is one decorated function in `cassandra_mcp.py`; the schema is
declared beside the implementation and the JSON the model sees is generated
from it.
