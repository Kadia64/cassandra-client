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
