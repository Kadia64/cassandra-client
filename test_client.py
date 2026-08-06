"""Tests for the fleet client.

Run: `python3 -m pytest test_client.py`

The point of these is the **platform-specific and lossy** parts — path handling
and cwd recovery — which are pure functions precisely so every platform can be
covered from one machine. The transport is thin enough not to need mocking; the
server side is tested in the brain's own suite.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cassandra_client as client


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- platform --------------------------------------------------------------

def test_transcript_root_on_every_platform() -> None:
    home = Path("/somewhere/home")
    for system in ("darwin", "linux", "win32"):
        assert client.transcript_root(system, home) == home / ".claude" / "projects"


# ---- cwd recovery ----------------------------------------------------------

def test_decode_cwd_is_lossy_which_is_why_it_is_a_fallback() -> None:
    """Claude Code escapes separators to dashes, so a directory whose own name
    contains a dash is indistinguishable from a nested path. Real examples that
    would misroute: `Project-Frontier`, `cassandra-system`, `Marketing Project`."""
    assert client.decode_cwd("-Users-wes-dev") == "/Users/wes/dev"
    assert client.decode_cwd("-Users-wes-Desktop-Project-Frontier") != \
        "/Users/wes/Desktop/Project-Frontier"
    assert client.decode_cwd("plain") == "plain"


def test_read_cwd_prefers_the_transcript() -> None:
    data = (b'{"type":"mode","sessionId":"x"}\n'
            b'{"type":"user","cwd":"/Users/wes/Desktop/Project-Frontier"}\n')
    assert client.read_cwd(data, "-Users-wes-Desktop-Project-Frontier") == \
        "/Users/wes/Desktop/Project-Frontier"


def test_read_cwd_handles_a_path_with_spaces() -> None:
    data = b'{"cwd":"/Users/wes/Desktop/Marketing Project"}\n'
    assert client.read_cwd(data, "-Users-wes-Desktop-Marketing-Project") == \
        "/Users/wes/Desktop/Marketing Project"


def test_read_cwd_falls_back_when_the_transcript_has_none() -> None:
    assert client.read_cwd(b'{"type":"mode"}\n', "-Users-wes-dev") == "/Users/wes/dev"


def test_read_cwd_skips_corrupt_lines() -> None:
    assert client.read_cwd(b'{ not json\n{"cwd":"/a/b"}\n', "-x") == "/a/b"


def test_read_cwd_on_an_empty_file() -> None:
    assert client.read_cwd(b"", "-Users-wes-dev") == "/Users/wes/dev"


# ---- discovery -------------------------------------------------------------

def test_local_sessions_finds_transcripts_and_skips_sidecars(tmp_path: Path) -> None:
    folder = tmp_path / "-Users-wes-dev-mygame"
    folder.mkdir()
    body = b'{"cwd":"/Users/wes/dev/mygame"}\n'
    (folder / "aaaa-1111.jsonl").write_bytes(body)
    (folder / "aaaa-1111.ccr-tip.json").write_bytes(b"{}")   # not a conversation

    found = client.local_sessions(tmp_path)
    assert len(found) == 1
    assert found[0]["session_id"] == "aaaa-1111"
    assert found[0]["cwd"] == "/Users/wes/dev/mygame"
    assert found[0]["hash"] == sha256(body)
    assert found[0]["bytes"] == len(body)


def test_local_sessions_across_several_directories(tmp_path: Path) -> None:
    for name, cwd in [("-a-one", "/a/one"), ("-a-two", "/a/two")]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "s.jsonl").write_bytes(json.dumps({"cwd": cwd}).encode() + b"\n")
    assert sorted(s["cwd"] for s in client.local_sessions(tmp_path)) == ["/a/one", "/a/two"]


def test_local_sessions_when_nothing_is_installed(tmp_path: Path) -> None:
    assert client.local_sessions(tmp_path / "no-claude-here") == []


# ---- rollback watchdog -----------------------------------------------------

def test_a_healthy_heartbeat_promotes_a_pending_version(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(client, "CONFIG", tmp_path / "client.json")
    cfg = {"pending_sha": "b" * 40, "last_good_sha": "a" * 40}
    client.save_config(cfg)

    assert client.confirm_or_rollback(dict(cfg), healthy=True) is False
    after = client.load_config()
    assert after["last_good_sha"] == "b" * 40
    assert "pending_sha" not in after


def test_failures_accumulate_before_rolling_back(tmp_path: Path, monkeypatch) -> None:
    """One missed heartbeat is a flaky network, not a bad build."""
    monkeypatch.setattr(client, "CONFIG", tmp_path / "client.json")
    client.save_config({"pending_sha": "b" * 40, "last_good_sha": "a" * 40})

    assert client.confirm_or_rollback(client.load_config(), healthy=False) is False
    assert client.load_config()["pending_failures"] == 1


def test_nothing_happens_without_a_pending_version(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(client, "CONFIG", tmp_path / "client.json")
    client.save_config({"last_good_sha": "a" * 40})
    assert client.confirm_or_rollback(client.load_config(), healthy=False) is False


# ---- config ----------------------------------------------------------------

def test_config_is_written_private(tmp_path: Path, monkeypatch) -> None:
    """It holds a bearer token — the one file here worth protecting."""
    monkeypatch.setattr(client, "CONFIG", tmp_path / "nested" / "client.json")
    client.save_config({"token": "secret"})
    assert (client.CONFIG.stat().st_mode & 0o777) == 0o600


def test_a_corrupt_config_reads_as_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(client, "CONFIG", tmp_path / "client.json")
    client.CONFIG.write_text("{ not json")
    assert client.load_config() == {}
