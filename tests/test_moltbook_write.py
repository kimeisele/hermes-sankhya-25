"""Tests for the Moltbook verified-write bridge.

All tests use redacted structurally faithful fixture payloads based on the
real Moltbook API contract observed during B001.  No live API calls.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MOLTBOOK_WRITE = str(SCRIPTS / "moltbook_write.py")


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_bridge_module():
    spec = importlib.util.spec_from_file_location(
        "moltbook_write", SCRIPTS / "moltbook_write.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["moltbook_write"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _cli_run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, MOLTBOOK_WRITE, *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------

@dataclass
class _MockClient:
    create_post_resp: dict[str, Any] = field(default_factory=dict)
    create_comment_resp: dict[str, Any] = field(default_factory=dict)
    verify_resp: dict[str, Any] = field(default_factory=dict)
    fetch_post_resp: dict[str, Any] = field(default_factory=dict)
    create_post_calls: list = field(default_factory=list)
    create_comment_calls: list = field(default_factory=list)
    verify_calls: list = field(default_factory=list)
    fetch_post_calls: list = field(default_factory=list)
    _verify_raises: RuntimeError | None = None
    _fetch_raises: RuntimeError | None = None

    def create_post(self, payload):
        self.create_post_calls.append(payload)
        return self.create_post_resp

    def create_comment(self, parent_id, payload):
        self.create_comment_calls.append((parent_id, payload))
        return self.create_comment_resp

    def fetch_post(self, post_id):
        self.fetch_post_calls.append(post_id)
        return self.fetch_post_resp
    def verify_challenge(self, code, answer):
        self.verify_calls.append((code, answer))
        if self._verify_raises:
            raise self._verify_raises
        return self.verify_resp


# ---------------------------------------------------------------------------
# Transport contract tests (monkeypatch urlopen)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): pass


def _capture_urlopen(monkeypatch):
    """Capture all urlopen calls and return a FakeResponse."""
    calls: list[dict] = []

    def _fake_urlopen(req, **kw):
        calls.append({
            "method": req.get_method(),
            "full_url": req.full_url,
            "data": req.data,
            "headers": {k: v for k, v in req.headers.items()},
        })
        return _FakeResponse(200, json.dumps({"success": True, "captured": True}).encode())

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    return calls


def _json_body(data: bytes | None) -> dict | None:
    return json.loads(data.decode()) if data else None


def test_transport_post_create_uses_posts_endpoint(monkeypatch) -> None:
    m = _load_bridge_module()
    calls = _capture_urlopen(monkeypatch)

    client = m.MoltbookClient("https://api.example.com")
    client.create_post({"content": "hello", "type": "post"})

    assert len(calls) == 1
    c = calls[0]
    assert c["method"] == "POST"
    assert c["full_url"] == "https://api.example.com/posts"
    body = _json_body(c["data"])
    assert body is not None
    assert body["content"] == "hello"


def test_transport_comment_create_uses_nested_path(monkeypatch) -> None:
    m = _load_bridge_module()
    calls = _capture_urlopen(monkeypatch)

    client = m.MoltbookClient("https://api.example.com")
    client.create_comment("post_abc", {"content": "reply", "type": "comment"})

    assert len(calls) == 1
    c = calls[0]
    assert c["method"] == "POST"
    assert c["full_url"] == "https://api.example.com/posts/post_abc/comments"


def test_transport_post_fetch_uses_posts_path(monkeypatch) -> None:
    m = _load_bridge_module()
    calls = _capture_urlopen(monkeypatch)

    client = m.MoltbookClient("https://api.example.com")
    client.fetch_post("post_xyz")

    assert len(calls) == 1
    c = calls[0]
    assert c["method"] == "GET"
    assert c["full_url"] == "https://api.example.com/posts/post_xyz"


def test_transport_verify_uses_verify_endpoint(monkeypatch) -> None:
    m = _load_bridge_module()
    calls = _capture_urlopen(monkeypatch)

    client = m.MoltbookClient("https://api.example.com")
    client.verify_challenge("ch_abc", "42")

    assert len(calls) == 1
    c = calls[0]
    assert c["method"] == "POST"
    assert c["full_url"] == "https://api.example.com/verify"
    body = _json_body(c["data"])
    assert body is not None
    assert body["verification_code"] == "ch_abc"
    assert body["answer"] == "42"


def test_transport_includes_authorization(monkeypatch) -> None:
    m = _load_bridge_module()
    calls = _capture_urlopen(monkeypatch)
    monkeypatch.setattr(m, "_get_token", lambda: "tok_abc123")

    client = m.MoltbookClient("https://api.example.com")
    client.fetch_post("p1")

    assert len(calls) == 1
    assert calls[0]["headers"].get("Authorization") == "Bearer tok_abc123"


# ---------------------------------------------------------------------------
# Response parsing — nested structures
# ---------------------------------------------------------------------------

def test_parse_create_post_nested_fields() -> None:
    m = _load_bridge_module()
    raw = _load_fixture("post_create_verified_pending.json")
    result = m._parse_create_response(raw, "post")
    assert result["content_id"] == "post_8f3a_20260726"
    assert result["url"] == "https://www.moltbook.com/posts/post_8f3a_20260726"
    assert result["verification_code"] == "ch_4a9f2b1c"
    assert result["challenge_text"] == "What is 7 + 4?"
    assert result["instructions"] == "Reply with the numeric answer to verify."


def test_parse_create_comment_nested_fields() -> None:
    m = _load_bridge_module()
    raw = _load_fixture("comment_create_verified_pending.json")
    result = m._parse_create_response(raw, "comment")
    assert result["content_id"] == "comment_2b7e_20260726"
    assert result["verification_code"] == "ch_7d3e5f6a"
    assert result["parent_post_id"] == "post_abc"


def test_parse_create_rejects_missing_success() -> None:
    m = _load_bridge_module()
    with __import__("pytest").raises(RuntimeError, match="success"):
        m._parse_create_response({"success": False, "post": {}}, "post")


def test_parse_create_rejects_missing_content_object() -> None:
    m = _load_bridge_module()
    with __import__("pytest").raises(RuntimeError, match="Expected 'post'"):
        m._parse_create_response({"success": True}, "post")


def test_parse_create_rejects_missing_verification() -> None:
    m = _load_bridge_module()
    with __import__("pytest").raises(RuntimeError, match="verification"):
        m._parse_create_response({"success": True, "post": {"id": "x"}}, "post")


def test_parse_create_rejects_missing_verification_code() -> None:
    m = _load_bridge_module()
    raw = {"success": True, "post": {"id": "x", "verification": {"challenge_text": "?"}}}
    with __import__("pytest").raises(RuntimeError, match="verification_code"):
        m._parse_create_response(raw, "post")


# ---------------------------------------------------------------------------
# Fetch parsing
# ---------------------------------------------------------------------------

def test_parse_fetch_post_extracts_post_object() -> None:
    m = _load_bridge_module()
    raw = _load_fixture("post_fetch_verified.json")
    obj = m._parse_fetch_response(raw, "post", "post_8f3a_20260726")
    assert obj["id"] == "post_8f3a_20260726"
    assert obj["verification_status"] == "verified"


def test_parse_fetch_post_rejects_mismatched_id() -> None:
    m = _load_bridge_module()
    raw = _load_fixture("post_fetch_verified.json")
    with __import__("pytest").raises(RuntimeError, match="id"):
        m._parse_fetch_response(raw, "post", "wrong_id")


def test_parse_fetch_comment_selects_exact_comment() -> None:
    m = _load_bridge_module()
    raw = _load_fixture("comment_fetch_verified.json")
    obj = m._parse_fetch_response(raw, "comment", "comment_2b7e_20260726")
    assert obj["id"] == "comment_2b7e_20260726"
    assert obj["verification_status"] == "verified"


def test_parse_fetch_comment_rejects_when_not_found() -> None:
    m = _load_bridge_module()
    raw = _load_fixture("comment_fetch_verified.json")
    with __import__("pytest").raises(RuntimeError, match="not found"):
        m._parse_fetch_response(raw, "comment", "nonexistent")


# ---------------------------------------------------------------------------
# Challenge extraction (passthrough)
# ---------------------------------------------------------------------------

def test_challenge_extraction_post_passthrough() -> None:
    m = _load_bridge_module()
    raw = _load_fixture("post_create_verified_pending.json")
    result = m.extract_challenge(raw, "post")
    assert result["challenge_text"] == "What is 7 + 4?"
    assert result["verification_code"] == "ch_4a9f2b1c"


def test_challenge_extraction_numeric_passthrough() -> None:
    m = _load_bridge_module()
    raw = _load_fixture("post_create_numeric_challenge.json")
    result = m.extract_challenge(raw, "post")
    assert result["challenge_text"] == "12"


# ---------------------------------------------------------------------------
# Transaction store
# ---------------------------------------------------------------------------

def test_store_roundtrip(tmp_path: Path) -> None:
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(transaction_id="t1", content_id="p1", content_type="post",
                        parent_post_id="", url="https://x", raw_challenge_text="?",
                        verification_code="ch", challenge_instructions="",
                        expires_at=time.time()+300, raw_create_response={"ok":True})
    store.save(txn)
    loaded = store.load("t1")
    assert loaded is not None
    assert loaded.content_id == "p1"
    assert loaded.state == "pending"


def test_store_update_state(tmp_path: Path) -> None:
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(transaction_id="t2", content_id="p2", content_type="post",
                        parent_post_id="", url="https://x", raw_challenge_text="?",
                        verification_code="ch", challenge_instructions="",
                        expires_at=time.time()+600, raw_create_response={})
    store.save(txn)
    store.update_state("t2", state="verified", verified_at=time.time())
    loaded = store.load("t2")
    assert loaded is not None
    assert loaded.state == "verified"


def test_store_load_nonexistent(tmp_path: Path) -> None:
    m = _load_bridge_module()
    assert m.TransactionStore(tmp_path).load("no") is None


def test_store_permissions_restrictive(tmp_path: Path) -> None:
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(transaction_id="tp", content_id="pp", content_type="post",
                        parent_post_id="", url="https://x", raw_challenge_text="?",
                        verification_code="ch", challenge_instructions="",
                        expires_at=time.time()+300, raw_create_response={})
    store.save(txn)
    mode = store.path().stat().st_mode & 0o777
    assert mode & 0o077 == 0, f"permissions too open: {oct(mode)}"


# ---------------------------------------------------------------------------
# Create command
# ---------------------------------------------------------------------------

def _mkcli(create_post=None, create_comment=None, verify=None, fetch_post=None):
    return _MockClient(create_post_resp=create_post or {}, create_comment_resp=create_comment or {},
                       verify_resp=verify or {}, fetch_post_resp=fetch_post or {})


def test_create_post_persists_and_outputs(tmp_path: Path) -> None:
    m = _load_bridge_module()
    resp = _load_fixture("post_create_verified_pending.json")
    client = _mkcli(create_post=resp)
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, json.dumps({"content": "test", "type": "post"}))
    assert exit_code == 0

    stored = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_data = stored[next(iter(stored))]
    assert txn_data["content_id"] == "post_8f3a_20260726"
    assert txn_data["content_type"] == "post"
    assert txn_data["raw_challenge_text"] == "What is 7 + 4?"
    assert txn_data["state"] == "pending"
    assert txn_data["raw_create_response"] == resp


def test_create_comment_persists_parent_id(tmp_path: Path) -> None:
    m = _load_bridge_module()
    resp = _load_fixture("comment_create_verified_pending.json")
    client = _mkcli(create_comment=resp)
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store,
        json.dumps({"content": "reply", "type": "comment", "parent_post_id": "post_abc"}))
    assert exit_code == 0

    stored = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_data = stored[next(iter(stored))]
    assert txn_data["content_type"] == "comment"
    assert txn_data["parent_post_id"] == "post_abc"


def test_create_rejects_ambiguous_type(tmp_path: Path) -> None:
    m = _load_bridge_module()
    exit_code = m.cmd_create(_mkcli(), m.TransactionStore(tmp_path),
                             json.dumps({"content": "x"}))
    assert exit_code == 1


def test_create_rejects_comment_without_parent(tmp_path: Path) -> None:
    m = _load_bridge_module()
    exit_code = m.cmd_create(_mkcli(), m.TransactionStore(tmp_path),
                             json.dumps({"content": "x", "type": "comment"}))
    assert exit_code == 1


def test_create_rejects_invalid_json(tmp_path: Path) -> None:
    m = _load_bridge_module()
    assert m.cmd_create(_mkcli(), m.TransactionStore(tmp_path), "bad{") == 1


def test_create_rejects_missing_content(tmp_path: Path) -> None:
    m = _load_bridge_module()
    assert m.cmd_create(_mkcli(), m.TransactionStore(tmp_path),
                        json.dumps({"type": "post"})) == 1


# ---------------------------------------------------------------------------
# Verify — expiry
# ---------------------------------------------------------------------------

def test_verify_rejects_expired(tmp_path: Path) -> None:
    m = _load_bridge_module()
    client = _mkcli(create_post=_load_fixture("post_create_expired.json"))
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x", "type": "post"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))

    assert m.cmd_verify(client, store, txn_id, "8") == 1


# ---------------------------------------------------------------------------
# Verify — single-attempt
# ---------------------------------------------------------------------------

def test_verify_rejects_second_attempt(tmp_path: Path) -> None:
    m = _load_bridge_module()
    create = dict(_load_fixture("post_create_verified_pending.json"))
    create["post"]["verification"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _mkcli(create_post=create,
                    verify=_load_fixture("verify_accepted.json"),
                    fetch_post=_load_fixture("post_fetch_verified.json"))
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x", "type": "post"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))

    assert m.cmd_verify(client, store, txn_id, "11") == 0
    assert m.cmd_verify(client, store, txn_id, "11") == 1  # terminal


# ---------------------------------------------------------------------------
# Crash-safe attempted semantics
# ---------------------------------------------------------------------------

def test_attempted_state_never_resubmits(tmp_path: Path) -> None:
    """A transaction in 'attempted' state must never call verify_challenge.

    Simulates process termination: create txn, mark attempted, reload in
    new store/client, call verify again — zero verify HTTP calls occur.
    """
    m = _load_bridge_module()
    create = dict(_load_fixture("post_create_verified_pending.json"))
    create["post"]["verification"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _mkcli(create_post=create,
                    verify=_load_fixture("verify_accepted.json"),
                    fetch_post=_load_fixture("post_fetch_verified.json"))
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x", "type": "post"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))

    # Mark attempted directly (simulate crash after mark but before verify response)
    store.update_state(txn_id, state="attempted", submitted_answer="11",
                       attempted_at=time.time())

    # --- Simulate new process ---
    fresh_store = m.TransactionStore(tmp_path)
    fresh_client = _mkcli(create_post=create,
                          verify=_load_fixture("verify_accepted.json"),
                          fetch_post=_load_fixture("post_fetch_verified.json"))

    exit_code = m.cmd_verify(fresh_client, fresh_store, txn_id, "11")
    assert exit_code == 0

    # Zero verify_challenge calls
    assert len(fresh_client.verify_calls) == 0
    # One fetch_post call (reconciliation)
    assert len(fresh_client.fetch_post_calls) == 1

    loaded = fresh_store.load(txn_id)
    assert loaded is not None
    assert loaded.state == "verified"


def test_attempted_reconciliation_stays_attempted_if_unverified(tmp_path: Path) -> None:
    """Reconciliation on attempted txn with unverified content stays attempted."""
    m = _load_bridge_module()
    create = dict(_load_fixture("post_create_verified_pending.json"))
    create["post"]["verification"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _mkcli(create_post=create, verify={},
                    fetch_post=_load_fixture("post_fetch_pending.json"))
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x", "type": "post"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))

    store.update_state(txn_id, state="attempted", submitted_answer="11",
                       attempted_at=time.time())

    fresh_client = _mkcli(create_post=create, verify={},
                          fetch_post=_load_fixture("post_fetch_pending.json"))
    fresh_store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_verify(fresh_client, fresh_store, txn_id, "11")
    assert exit_code == 1
    assert len(fresh_client.verify_calls) == 0  # no resubmit

    loaded = fresh_store.load(txn_id)
    assert loaded is not None
    assert loaded.state == "attempted"  # not indeterminate


# ---------------------------------------------------------------------------
# Verify — happy path
# ---------------------------------------------------------------------------

def test_verify_succeeds_post(tmp_path: Path) -> None:
    m = _load_bridge_module()
    create = dict(_load_fixture("post_create_verified_pending.json"))
    create["post"]["verification"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _mkcli(create_post=create,
                    verify=_load_fixture("verify_accepted.json"),
                    fetch_post=_load_fixture("post_fetch_verified.json"))
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x", "type": "post"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))

    assert m.cmd_verify(client, store, txn_id, "11") == 0
    loaded = store.load(txn_id)
    assert loaded is not None
    assert loaded.state == "verified"


def test_verify_succeeds_comment(tmp_path: Path) -> None:
    m = _load_bridge_module()
    create = dict(_load_fixture("comment_create_verified_pending.json"))
    create["comment"]["verification"]["expires_at"] = "2099-07-26T18:15:00Z"

    # comment fetch returns a post with comments array
    fetch = _load_fixture("comment_fetch_verified.json")

    client = _mkcli(create_comment=create,
                    verify=_load_fixture("verify_accepted.json"),
                    fetch_post=fetch)
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store,
        json.dumps({"content": "reply", "type": "comment", "parent_post_id": "post_abc"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))

    assert m.cmd_verify(client, store, txn_id, "4") == 0
    loaded = store.load(txn_id)
    assert loaded is not None
    assert loaded.state == "verified"


# ---------------------------------------------------------------------------
# Verify — timeout recovery
# ---------------------------------------------------------------------------

def test_verify_timeout_recovers_if_verified(tmp_path: Path) -> None:
    m = _load_bridge_module()
    create = dict(_load_fixture("post_create_verified_pending.json"))
    create["post"]["verification"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _mkcli(create_post=create, verify={},
                    fetch_post=_load_fixture("post_fetch_verified.json"))
    client._verify_raises = RuntimeError("timed out")
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x", "type": "post"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))

    exit_code = m.cmd_verify(client, store, txn_id, "11")
    assert exit_code == 0
    loaded = store.load(txn_id)
    assert loaded is not None
    assert loaded.state == "verified"


def test_verify_timeout_indeterminate_if_unverified(tmp_path: Path) -> None:
    m = _load_bridge_module()
    create = dict(_load_fixture("post_create_verified_pending.json"))
    create["post"]["verification"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _mkcli(create_post=create, verify={},
                    fetch_post=_load_fixture("post_fetch_pending.json"))
    client._verify_raises = RuntimeError("timed out")
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x", "type": "post"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))

    exit_code = m.cmd_verify(client, store, txn_id, "11")
    assert exit_code == 1
    loaded = store.load(txn_id)
    assert loaded is not None
    assert loaded.state == "indeterminate"


# ---------------------------------------------------------------------------
# Verify — nonexistent
# ---------------------------------------------------------------------------

def test_verify_nonexistent(tmp_path: Path) -> None:
    m = _load_bridge_module()
    assert m.cmd_verify(_mkcli(), m.TransactionStore(tmp_path), "no", "42") == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_create_help() -> None:
    assert _cli_run("create", "--help").returncode in (0, 2)


def test_cli_verify_help() -> None:
    assert _cli_run("verify", "--help").returncode in (0, 2)


def test_cli_no_subcommand() -> None:
    assert _cli_run().returncode != 0


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

def test_fixtures_contain_no_tokens() -> None:
    for name in sorted(FIXTURES.iterdir()):
        if name.suffix == ".json":
            text = name.read_text()
            assert "Bearer" not in text, f"{name.name} contains Bearer"
            assert "authorization" not in text.lower(), f"{name.name}"


# ---------------------------------------------------------------------------
# Dry-run transcript
# ---------------------------------------------------------------------------

def test_dry_run_transcript(tmp_path: Path) -> None:
    m = _load_bridge_module()
    create = dict(_load_fixture("post_create_verified_pending.json"))
    create["post"]["verification"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _mkcli(create_post=create,
                    verify=_load_fixture("verify_accepted.json"),
                    fetch_post=_load_fixture("post_fetch_verified.json"))
    store = m.TransactionStore(tmp_path)

    sys.stdout.write("=== CREATE ===\n")
    assert m.cmd_create(client, store, json.dumps({
        "content": "What is the smallest practical receipt?",
        "type": "post",
    })) == 0

    stored = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_id = next(iter(stored))
    txn = stored[txn_id]
    print(f"TX: {txn_id}  content: {txn['content_id']}  type: {txn['content_type']}")
    print(f"CHALLENGE: {txn['raw_challenge_text']}  code: {txn['verification_code']}")
    assert txn["raw_challenge_text"] == "What is 7 + 4?"

    sys.stdout.write("\n=== VERIFY (answer: 11) ===\n")
    assert m.cmd_verify(client, store, txn_id, "11") == 0
    loaded = store.load(txn_id)
    assert loaded is not None
    print(f"STATE: {loaded.state}  answer: {loaded.submitted_answer}")
    assert loaded.state == "verified"

    sys.stdout.write("\n=== VERIFY (second — rejected) ===\n")
    assert m.cmd_verify(client, store, txn_id, "11") == 1
    print("COMPLETE")
