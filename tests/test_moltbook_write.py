"""Tests for the Moltbook verified-write bridge — round 3.

Covers: credentials, attempted-expiry reconciliation, malformed-create
persistence, outbound body stripping, comment read shapes.
"""
from __future__ import annotations

import importlib.util
import json
import pytest
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


def _load_bridge_module():
    spec = importlib.util.spec_from_file_location(
        "moltbook_write", SCRIPTS / "moltbook_write.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["moltbook_write"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Fake HTTP response for transport tests
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): pass


def _capture_urlopen(monkeypatch):
    calls: list[dict] = []

    def _fake(req, **kw):
        calls.append({
            "method": req.get_method(),
            "full_url": req.full_url,
            "data": req.data,
            "headers": dict(req.headers),
        })
        return _FakeResponse(200, json.dumps({"success": True, "captured": True}).encode())

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    return calls


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------

@dataclass
class _MockClient:
    create_post_resp: dict[str, Any] = field(default_factory=dict)
    create_comment_resp: dict[str, Any] = field(default_factory=dict)
    verify_resp: dict[str, Any] = field(default_factory=dict)
    fetch_post_resp: dict[str, Any] = field(default_factory=dict)
    fetch_comments_resp: dict[str, Any] = field(default_factory=dict)
    create_post_calls: list = field(default_factory=list)
    create_comment_calls: list = field(default_factory=list)
    verify_calls: list = field(default_factory=list)
    fetch_post_calls: list = field(default_factory=list)
    fetch_comments_calls: list = field(default_factory=list)
    _verify_raises: RuntimeError | None = None

    def create_post(self, payload):
        self.create_post_calls.append(payload)
        return self.create_post_resp

    def create_comment(self, parent_id, payload):
        self.create_comment_calls.append((parent_id, payload))
        return self.create_comment_resp

    def verify_challenge(self, code, answer):
        self.verify_calls.append((code, answer))
        if self._verify_raises:
            raise self._verify_raises
        return self.verify_resp

    def fetch_post(self, post_id):
        self.fetch_post_calls.append(post_id)
        return self.fetch_post_resp

    def fetch_comments(self, parent_id):
        self.fetch_comments_calls.append(parent_id)
        return self.fetch_comments_resp


# ---------------------------------------------------------------------------
# Credential tests
# ---------------------------------------------------------------------------

def test_credential_token_env_takes_precedence(monkeypatch, tmp_path: Path) -> None:
    """MOLTBOOK_TOKEN env var takes precedence over credentials file."""
    m = _load_bridge_module()
    monkeypatch.delenv("MOLTBOOK_TOKEN", raising=False)
    monkeypatch.setenv("MOLTBOOK_TOKEN", "tok_env")
    creds_dir = tmp_path / ".config" / "moltbook"
    creds_dir.mkdir(parents=True)
    (creds_dir / "credentials.json").write_text(json.dumps({"api_key": "tok_file"}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert m._get_token() == "tok_env"


def test_credential_api_key_from_file(tmp_path: Path, monkeypatch) -> None:
    m = _load_bridge_module()
    monkeypatch.delenv("MOLTBOOK_TOKEN", raising=False)
    creds_dir = tmp_path / ".config" / "moltbook"
    creds_dir.mkdir(parents=True)
    (creds_dir / "credentials.json").write_text(json.dumps({"api_key": "tok_file"}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    token = m._get_token()
    assert token == "tok_file"


def test_credential_no_token_blocks_create(tmp_path: Path, monkeypatch) -> None:
    """No credential → no network call, immediate failure."""
    m = _load_bridge_module()
    monkeypatch.setattr(m, "_get_token", lambda: None)
    calls = _capture_urlopen(monkeypatch)

    resp = _load_fixture("post_create_verified_pending.json")
    client = _MockClient(create_post_resp=resp)
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store,
                             json.dumps({"title": "x", "submolt": "s", "type": "post"}))
    assert exit_code == 1
    # Zero network calls
    assert len(calls) == 0


def test_credential_token_never_persisted(tmp_path: Path, monkeypatch) -> None:
    """Token does not appear in stored transaction data."""
    m = _load_bridge_module()
    monkeypatch.setattr(m, "_get_token", lambda: "secret_tok")
    resp = _load_fixture("post_create_verified_pending.json")
    client = _MockClient(create_post_resp=resp)
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store,
                        json.dumps({"title": "x", "submolt": "s", "type": "post"})) == 0

    raw = (tmp_path / "data" / "moltbook" / "transactions.json").read_text()
    assert "secret_tok" not in raw
    assert "Bearer" not in raw
    assert "Authorization" not in raw


def test_verify_blocks_no_credential_pending_unchanged(tmp_path: Path) -> None:
    """Pending txn without credential: stays pending, zero verify, zero fetch."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: None)

    client = _MockClient()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(transaction_id="t_pend_nocred", content_id="p1",
                        content_type="post", parent_post_id="", url="https://x",
                        raw_challenge_text="q", verification_code="c",
                        challenge_instructions="", expires_at=time.time()+9999,
                        raw_create_response={}, state="pending")
    store.save(txn)

    exit_code = m.cmd_verify(client, store, "t_pend_nocred", "42")
    assert exit_code == 1
    assert len(client.verify_calls) == 0
    assert len(client.fetch_post_calls) == 0

    loaded = store.load("t_pend_nocred")
    assert loaded is not None
    assert loaded.state == "pending"


def test_verify_blocks_no_credential_attempted_unchanged(tmp_path: Path) -> None:
    """Attempted txn without credential: stays attempted, zero verify, zero fetch."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: None)

    client = _MockClient()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(transaction_id="t_att_nocred", content_id="p1",
                        content_type="post", parent_post_id="", url="https://x",
                        raw_challenge_text="q", verification_code="c",
                        challenge_instructions="", expires_at=time.time()+9999,
                        raw_create_response={}, state="attempted",
                        submitted_answer="11", attempted_at=time.time())
    store.save(txn)

    exit_code = m.cmd_verify(client, store, "t_att_nocred", "11")
    assert exit_code == 1
    assert len(client.verify_calls) == 0
    assert len(client.fetch_post_calls) == 0

    loaded = store.load("t_att_nocred")
    assert loaded is not None
    assert loaded.state == "attempted"


# ---------------------------------------------------------------------------
# Transport: outbound body stripping
# ---------------------------------------------------------------------------

def test_outbound_body_strips_type_and_parent_post_id(monkeypatch) -> None:
    m = _load_bridge_module()
    calls = _capture_urlopen(monkeypatch)
    monkeypatch.setattr(m, "_get_token", lambda: "tok")

    client = m.MoltbookClient("https://api.example.com")
    client.create_post(m._build_api_payload({
        "content": "hello", "type": "post", "parent_post_id": "should_be_removed"
    }))

    assert len(calls) == 1
    body = json.loads(calls[0]["data"].decode())
    assert "type" not in body
    assert "parent_post_id" not in body
    assert body["content"] == "hello"


def test_outbound_body_maps_reply_to_comment_id(monkeypatch) -> None:
    m = _load_bridge_module()
    calls = _capture_urlopen(monkeypatch)
    monkeypatch.setattr(m, "_get_token", lambda: "tok")

    client = m.MoltbookClient("https://api.example.com")
    client.create_comment("post_abc", m._build_api_payload({
        "content": "reply", "type": "comment",
        "parent_post_id": "post_abc", "reply_to_comment_id": "comment_x",
    }))

    assert len(calls) == 1
    body = json.loads(calls[0]["data"].decode())
    assert "type" not in body
    assert "parent_post_id" not in body
    assert "reply_to_comment_id" not in body
    assert body["parent_id"] == "comment_x"
    assert body["content"] == "reply"


def test_outbound_comment_create_exact_url_and_body(monkeypatch) -> None:
    m = _load_bridge_module()
    calls = _capture_urlopen(monkeypatch)
    monkeypatch.setattr(m, "_get_token", lambda: "tok")

    client = m.MoltbookClient("https://api.example.com")
    client.create_comment("parent_123", {"content": "a comment"})

    assert len(calls) == 1
    c = calls[0]
    assert c["method"] == "POST"
    assert c["full_url"] == "https://api.example.com/posts/parent_123/comments"
    body = json.loads(c["data"].decode())
    assert body == {"content": "a comment"}


# ---------------------------------------------------------------------------
# Transport: basic routing
# ---------------------------------------------------------------------------

def test_transport_post_create_uses_posts(monkeypatch) -> None:
    m = _load_bridge_module()
    calls = _capture_urlopen(monkeypatch)
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = m.MoltbookClient("https://api.example.com")
    client.create_post({"content": "x"})
    assert calls[0]["full_url"] == "https://api.example.com/posts"
    assert calls[0]["method"] == "POST"


def test_transport_verify_uses_verify_endpoint(monkeypatch) -> None:
    m = _load_bridge_module()
    calls = _capture_urlopen(monkeypatch)
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = m.MoltbookClient("https://api.example.com")
    client.verify_challenge("ch_x", "42")
    assert calls[0]["full_url"] == "https://api.example.com/verify"
    body = json.loads(calls[0]["data"].decode())
    assert body == {"verification_code": "ch_x", "answer": "42"}


def test_transport_authorization_header(monkeypatch) -> None:
    m = _load_bridge_module()
    monkeypatch.setattr(m, "_get_token", lambda: "tok_abc")
    calls = _capture_urlopen(monkeypatch)
    m.MoltbookClient("https://api.example.com").fetch_post("p1")
    assert calls[0]["headers"].get("Authorization") == "Bearer tok_abc"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def test_parse_create_post_nested() -> None:
    m = _load_bridge_module()
    raw = _load_fixture("post_create_verified_pending.json")
    identity = m._extract_content_identity(raw, "post")
    assert identity["content_id"] == "post_8f3a_20260726"
    ver = m._extract_verification(raw, "post")
    assert ver["challenge_text"] == "What is 7 + 4?"
    assert ver["verification_code"] == "ch_4a9f2b1c"


def test_parse_fetch_post() -> None:
    m = _load_bridge_module()
    raw = _load_fixture("post_fetch_verified.json")
    obj = m._parse_fetch_response(raw, "post", "post_8f3a_20260726")
    assert obj["verification_status"] == "verified"


def test_parse_fetch_comment_via_post_comments() -> None:
    m = _load_bridge_module()
    raw = _load_fixture("comment_fetch_verified.json")
    obj = m._parse_fetch_response(raw, "comment", "comment_2b7e_20260726")
    assert obj["verification_status"] == "verified"


def test_parse_fetch_comment_via_list_endpoint() -> None:
    """Comment-list endpoint shape: top-level "comments" array."""
    m = _load_bridge_module()
    raw = _load_fixture("comment_list_verified.json")
    obj = m._parse_fetch_response(raw, "comment", "comment_2b7e_20260726")
    assert obj["verification_status"] == "verified"


def test_parse_fetch_comment_via_comments_array_directly() -> None:
    """Bare comments array at top level (no 'post' or 'comments' wrapper)."""
    m = _load_bridge_module()
    raw = {"comments": [
        {"id": "c1", "verification_status": "verified"},
        {"id": "c2", "verification_status": "unverified"},
    ]}
    obj = m._parse_fetch_response(raw, "comment", "c2")
    assert obj["verification_status"] == "unverified"


def test_fetch_content_object_falls_back_to_comments_list(tmp_path: Path) -> None:
    """When fetch_post() succeeds but the target comment is absent,
    _fetch_content_object falls back to fetch_comments().

    Both read calls occur exactly once.
    """
    m = _load_bridge_module()

    # Parent post response: has comments[] but not the target
    parent_without = _load_fixture("comment_fetch_missing_target.json")
    # Comment list response: has the target
    comment_list = _load_fixture("comment_list_verified.json")

    client = _MockClient(
        fetch_post_resp=parent_without,
        fetch_comments_resp=comment_list,
    )

    txn = m.Transaction(
        transaction_id="t_fb", content_id="comment_2b7e_20260726",
        content_type="comment", parent_post_id="post_abc",
        url="https://x", raw_challenge_text="q",
        verification_code="c", challenge_instructions="",
        expires_at=time.time() + 999, raw_create_response={},
    )

    obj = m._fetch_content_object(client, txn)
    assert obj["id"] == "comment_2b7e_20260726"
    assert obj["verification_status"] == "verified"

    # Both calls happened exactly once
    assert len(client.fetch_post_calls) == 1
    assert client.fetch_post_calls[0] == "post_abc"
    assert len(client.fetch_comments_calls) == 1
    assert client.fetch_comments_calls[0] == "post_abc"


def test_fetch_content_object_falls_back_when_parent_fetch_raises(tmp_path: Path) -> None:
    """When fetch_post() raises, _fetch_content_object falls back to fetch_comments().

    One parent-fetch call (raises), one comment-list call, correct comment selected.
    """
    m = _load_bridge_module()

    parent_resp = {}  # unused — fetch_post will raise
    comment_list = _load_fixture("comment_list_verified.json")

    client = _MockClient(
        fetch_post_resp=parent_resp,
        fetch_comments_resp=comment_list,
    )

    # Replace fetch_post to raise
    def _raising_fetch(post_id):
        client.fetch_post_calls.append(post_id)
        raise RuntimeError("parent fetch failed")
    client.fetch_post = _raising_fetch

    txn = m.Transaction(
        transaction_id="t_fb_raise", content_id="comment_2b7e_20260726",
        content_type="comment", parent_post_id="post_abc",
        url="https://x", raw_challenge_text="q",
        verification_code="c", challenge_instructions="",
        expires_at=time.time() + 999, raw_create_response={},
    )

    obj = m._fetch_content_object(client, txn)
    assert obj["id"] == "comment_2b7e_20260726"
    assert obj["verification_status"] == "verified"

    assert len(client.fetch_post_calls) == 1
    assert client.fetch_post_calls[0] == "post_abc"
    assert len(client.fetch_comments_calls) == 1
    assert client.fetch_comments_calls[0] == "post_abc"


# ---------------------------------------------------------------------------
# Malformed-create persistence (content identity always saved)
# ---------------------------------------------------------------------------

def test_malformed_missing_verification_object_persists(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    resp = _load_fixture("post_create_missing_verification.json")
    client = _MockClient(create_post_resp=resp)
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store,
                             json.dumps({"title": "x", "submolt": "s", "type": "post"}))
    assert exit_code == 1  # not success

    stored = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn = stored[next(iter(stored))]
    assert txn["state"] == "challenge_unavailable"
    assert txn["content_id"] == "post_no_ver_01"
    assert txn["content_type"] == "post"
    assert txn["parse_failure"] != ""
    assert txn["raw_create_response"] == resp


def test_malformed_missing_vcode_persists(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    resp = _load_fixture("post_create_missing_vcode.json")
    client = _MockClient(create_post_resp=resp)
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store,
                             json.dumps({"title": "x", "submolt": "s", "type": "post"}))
    assert exit_code == 1
    stored = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn = stored[next(iter(stored))]
    assert txn["state"] == "challenge_unavailable"
    assert txn["content_id"] == "post_no_vcode_01"
    assert "verification_code" in txn["parse_failure"].lower() or "Missing" in txn["parse_failure"]


def test_malformed_missing_challenge_text_persists(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    resp = _load_fixture("post_create_missing_challenge_text.json")
    client = _MockClient(create_post_resp=resp)
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store,
                             json.dumps({"title": "x", "submolt": "s", "type": "post"}))
    assert exit_code == 1
    stored = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn = stored[next(iter(stored))]
    assert txn["state"] == "challenge_unavailable"
    assert txn["content_id"] == "post_no_chall_01"


def test_malformed_expired_challenge_creates_pending(tmp_path: Path) -> None:
    """Expired-but-structurally-valid challenge: creates as pending.
    Expiry is enforced at verify time, not at create time."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    resp = _load_fixture("post_create_expired.json")
    client = _MockClient(create_post_resp=resp)
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store,
                             json.dumps({"title": "x", "submolt": "s", "type": "post"}))
    assert exit_code == 0  # structurally valid challenge
    stored = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn = stored[next(iter(stored))]
    assert txn["state"] == "pending"
    assert txn["content_id"] == "post_exp_01"
    # Now verify must reject due to expiry
    exit_code = m.cmd_verify(client, store,
                             next(iter(stored)), "8")
    assert exit_code == 1


# ---------------------------------------------------------------------------
# Transaction store
# ---------------------------------------------------------------------------

def test_store_roundtrip(tmp_path: Path) -> None:
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(transaction_id="t1", content_id="p1", content_type="post",
                        parent_post_id="", url="https://x", raw_challenge_text="?",
                        verification_code="ch", challenge_instructions="",
                        expires_at=time.time()+300, raw_create_response={})
    store.save(txn)
    loaded = store.load("t1")
    assert loaded is not None
    assert loaded.content_id == "p1"


def test_store_permissions(tmp_path: Path) -> None:
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(transaction_id="tp", content_id="pp", content_type="post",
                        parent_post_id="", url="https://x", raw_challenge_text="?",
                        verification_code="ch", challenge_instructions="",
                        expires_at=time.time()+300, raw_create_response={})
    store.save(txn)
    mode = store.path().stat().st_mode & 0o777
    assert mode & 0o077 == 0


# ---------------------------------------------------------------------------
# Create — normal path
# ---------------------------------------------------------------------------

def test_create_post(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient(create_post_resp=_load_fixture("post_create_verified_pending.json"))
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store,
                        json.dumps({"title": "x", "submolt": "s", "type": "post"})) == 0

    txn = store.load(next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text()))))
    assert txn is not None
    assert txn.content_id == "post_8f3a_20260726"
    assert txn.state == "pending"


def test_create_comment(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient(create_comment_resp=_load_fixture("comment_create_verified_pending.json"))
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store,
        json.dumps({"content": "r", "type": "comment", "parent_post_id": "post_abc"})) == 0

    txn = store.load(next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text()))))
    assert txn is not None
    assert txn.content_type == "comment"
    assert txn.parent_post_id == "post_abc"


def test_create_rejects_ambiguous_type(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    assert m.cmd_create(_MockClient(), m.TransactionStore(tmp_path),
                        json.dumps({"content": "x"})) == 1


def test_create_rejects_comment_without_parent(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    assert m.cmd_create(_MockClient(), m.TransactionStore(tmp_path),
                        json.dumps({"content": "x", "type": "comment"})) == 1


# ---------------------------------------------------------------------------
# Content-type validation — posts
# ---------------------------------------------------------------------------

def test_post_requires_title_and_submolt(tmp_path: Path) -> None:
    """Post with title + submolt is accepted."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient(create_post_resp=_load_fixture("post_create_verified_pending.json"))
    store = m.TransactionStore(tmp_path)
    assert m.cmd_create(client, store,
        json.dumps({"title": "Test", "submolt": "abc123", "type": "post"})) == 0


def test_post_requires_title_and_submolt_name(tmp_path: Path) -> None:
    """Post with title + submolt_name is accepted."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient(create_post_resp=_load_fixture("post_create_verified_pending.json"))
    store = m.TransactionStore(tmp_path)
    assert m.cmd_create(client, store,
        json.dumps({"title": "Test", "submolt_name": "some_sub", "type": "post"})) == 0


def test_post_requires_title_and_submolt_id(tmp_path: Path) -> None:
    """Post with title + submolt_id is accepted."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient(create_post_resp=_load_fixture("post_create_verified_pending.json"))
    store = m.TransactionStore(tmp_path)
    assert m.cmd_create(client, store,
        json.dumps({"title": "Test", "submolt_id": "id456", "type": "post"})) == 0


def test_post_rejects_missing_title(tmp_path: Path) -> None:
    """Post without title is rejected with zero create calls."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient()
    store = m.TransactionStore(tmp_path)
    assert m.cmd_create(client, store,
        json.dumps({"submolt": "abc123", "type": "post"})) == 1
    assert len(client.create_post_calls) == 0


def test_post_rejects_missing_submolt_identifier(tmp_path: Path) -> None:
    """Post without any Submolt identifier is rejected with zero create calls."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient()
    store = m.TransactionStore(tmp_path)
    assert m.cmd_create(client, store,
        json.dumps({"title": "Test", "type": "post"})) == 1
    assert len(client.create_post_calls) == 0


def test_post_rejects_blank_title(tmp_path: Path) -> None:
    """Post with blank title is rejected."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient()
    store = m.TransactionStore(tmp_path)
    assert m.cmd_create(client, store,
        json.dumps({"title": "   ", "submolt": "abc", "type": "post"})) == 1
    assert len(client.create_post_calls) == 0


def test_post_no_dummy_content_required(tmp_path: Path) -> None:
    """Valid post requires no dummy content/text/body field."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient(create_post_resp=_load_fixture("post_create_verified_pending.json"))
    store = m.TransactionStore(tmp_path)
    payload = {"title": "Test", "submolt": "abc123", "type": "post"}
    assert "content" not in payload
    assert "text" not in payload
    assert "body" not in payload
    assert m.cmd_create(client, store, json.dumps(payload)) == 0


def test_post_blank_submolt_does_not_mask_valid_later(tmp_path: Path) -> None:
    """Blank submolt does not mask a valid submolt_name later in the payload."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient(create_post_resp=_load_fixture("post_create_verified_pending.json"))
    store = m.TransactionStore(tmp_path)
    assert m.cmd_create(client, store,
        json.dumps({"title": "Test", "submolt": "   ", "submolt_name": "introductions", "type": "post"})) == 0


# ---------------------------------------------------------------------------
# Content-type validation — comments
# ---------------------------------------------------------------------------

def test_comment_requires_content_and_parent(tmp_path: Path) -> None:
    """Comment with content + parent_post_id is accepted."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient(create_comment_resp=_load_fixture("comment_create_verified_pending.json"))
    store = m.TransactionStore(tmp_path)
    assert m.cmd_create(client, store,
        json.dumps({"content": "A reply", "type": "comment", "parent_post_id": "post_abc"})) == 0


def test_comment_rejects_missing_content(tmp_path: Path) -> None:
    """Comment without content is rejected with zero create calls."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient()
    store = m.TransactionStore(tmp_path)
    assert m.cmd_create(client, store,
        json.dumps({"type": "comment", "parent_post_id": "post_abc"})) == 1
    assert len(client.create_comment_calls) == 0


def test_comment_rejects_blank_content(tmp_path: Path) -> None:
    """Comment with blank content is rejected with zero create calls."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient()
    store = m.TransactionStore(tmp_path)
    assert m.cmd_create(client, store,
        json.dumps({"content": "  ", "type": "comment", "parent_post_id": "post_abc"})) == 1
    assert len(client.create_comment_calls) == 0


# ---------------------------------------------------------------------------
# Bridge comment-refetch regression — real API post_id shape
# ---------------------------------------------------------------------------

def test_comment_create_parses_real_post_id_shape(tmp_path: Path) -> None:
    """Comment create response with post_id (not parent_post_id) is parsed."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    resp = _load_fixture("comment_create_real_shape.json")
    identity = m._extract_content_identity(resp, "comment")
    assert identity["content_id"] == "comment_fixture_real_shape"
    assert identity["parent_post_id"] == "parent_post_fixture_id"


def test_comment_refetch_reaches_verified_with_real_shape(tmp_path: Path) -> None:
    """Full create → verify → refetch cycle with real post_id shape reaches verified."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")

    create = dict(_load_fixture("comment_create_real_shape.json"))
    create["comment"]["verification"]["expires_at"] = "2099-07-27T10:05:00Z"

    client = _MockClient(
        create_comment_resp=create,
        verify_resp=_load_fixture("verify_accepted.json"),
        fetch_post_resp=_load_fixture("comment_fetch_real_shape_verified.json"),
    )
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({
        "content": "regression test", "type": "comment",
        "parent_post_id": "parent_post_fixture_id",
    })) == 0

    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))

    assert m.cmd_verify(client, store, txn_id, "4") == 0
    txn = store.load(txn_id)
    assert txn is not None
    assert txn.state == "verified"
    assert txn.parent_post_id == "parent_post_fixture_id"


def test_comment_refetch_falls_back_to_list_with_real_shape(tmp_path: Path) -> None:
    """Fallback comment-list path works with real post_id shape."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")

    create = dict(_load_fixture("comment_create_real_shape.json"))
    create["comment"]["verification"]["expires_at"] = "2099-07-27T10:05:00Z"

    # fetch_post returns a post without the target comment → triggers fallback
    parent_without = {"success": True, "post": {"id": "parent_post_fixture_id", "comments": []}}

    client = _MockClient(
        create_comment_resp=create,
        verify_resp=_load_fixture("verify_accepted.json"),
        fetch_post_resp=parent_without,
        fetch_comments_resp=_load_fixture("comment_list_real_shape_verified.json"),
    )
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({
        "content": "regression test", "type": "comment",
        "parent_post_id": "parent_post_fixture_id",
    })) == 0

    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))

    assert m.cmd_verify(client, store, txn_id, "4") == 0
    txn = store.load(txn_id)
    assert txn is not None
    assert txn.state == "verified"


def test_comment_malformed_real_shape_fails_closed(tmp_path: Path) -> None:
    """Malformed response with neither post_id nor parent_post_id raises RuntimeError."""
    m = _load_bridge_module()
    with pytest.raises(RuntimeError, match="Missing parent identifier"):
        m._extract_content_identity({
            "success": True,
            "comment": {"id": "c1", "content": "x"},
        }, "comment")


def test_comment_no_duplicate_verify_with_real_shape(tmp_path: Path) -> None:
    """Second verify on real-shape transaction is rejected (terminal state)."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")

    create = dict(_load_fixture("comment_create_real_shape.json"))
    create["comment"]["verification"]["expires_at"] = "2099-07-27T10:05:00Z"

    client = _MockClient(
        create_comment_resp=create,
        verify_resp=_load_fixture("verify_accepted.json"),
        fetch_post_resp=_load_fixture("comment_fetch_real_shape_verified.json"),
    )
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({
        "content": "regression test", "type": "comment",
        "parent_post_id": "parent_post_fixture_id",
    })) == 0

    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))
    assert m.cmd_verify(client, store, txn_id, "4") == 0
    # second verify blocked
    assert m.cmd_verify(client, store, txn_id, "4") == 1


# ---------------------------------------------------------------------------
# Verify — terminal and expiry
# ---------------------------------------------------------------------------

def test_verify_rejects_terminal_state(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    client = _MockClient()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(transaction_id="t1", content_id="p1", content_type="post",
                        parent_post_id="", url="https://x", raw_challenge_text="q",
                        verification_code="c", challenge_instructions="",
                        expires_at=time.time()+999, raw_create_response={},
                        state="challenge_unavailable")
    store.save(txn)
    assert m.cmd_verify(client, store, "t1", "42") == 1


def test_verify_rejects_pending_expired(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    resp = _load_fixture("post_create_expired.json")
    # Override to make it pending despite expired challenge (synthetic)
    client = _MockClient(create_post_resp=resp)
    store = m.TransactionStore(tmp_path)

    # Force-create pending despite expired (test internal consistency)
    txn = m.Transaction(transaction_id="t_exp", content_id="p_exp", content_type="post",
                        parent_post_id="", url="https://x", raw_challenge_text="q",
                        verification_code="c", challenge_instructions="",
                        expires_at=0, raw_create_response={})
    store.save(txn)
    assert m.cmd_verify(client, store, "t_exp", "8") == 1


# ---------------------------------------------------------------------------
# Verify — happy path
# ---------------------------------------------------------------------------

def test_verify_post_happy(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    create = dict(_load_fixture("post_create_verified_pending.json"))
    create["post"]["verification"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _MockClient(create_post_resp=create,
                         verify_resp=_load_fixture("verify_accepted.json"),
                         fetch_post_resp=_load_fixture("post_fetch_verified.json"))
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"title": "x", "submolt": "s", "type": "post"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))
    assert m.cmd_verify(client, store, txn_id, "11") == 0
    assert store.load(txn_id).state == "verified"


def test_verify_comment_happy(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    create = dict(_load_fixture("comment_create_verified_pending.json"))
    create["comment"]["verification"]["expires_at"] = "2099-07-26T18:15:00Z"

    client = _MockClient(create_comment_resp=create,
                         verify_resp=_load_fixture("verify_accepted.json"),
                         fetch_post_resp=_load_fixture("comment_fetch_verified.json"))
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store,
        json.dumps({"content": "r", "type": "comment", "parent_post_id": "post_abc"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))
    assert m.cmd_verify(client, store, txn_id, "4") == 0
    assert store.load(txn_id).state == "verified"


# ---------------------------------------------------------------------------
# Verify — single-attempt
# ---------------------------------------------------------------------------

def test_verify_rejects_second_attempt(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    create = dict(_load_fixture("post_create_verified_pending.json"))
    create["post"]["verification"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _MockClient(create_post_resp=create,
                         verify_resp=_load_fixture("verify_accepted.json"),
                         fetch_post_resp=_load_fixture("post_fetch_verified.json"))
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"title": "x", "submolt": "s", "type": "post"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))
    assert m.cmd_verify(client, store, txn_id, "11") == 0
    assert m.cmd_verify(client, store, txn_id, "11") == 1  # terminal


# ---------------------------------------------------------------------------
# Crash-safe: attempted reconciles after expiry
# ---------------------------------------------------------------------------

def test_attempted_after_expiry_reconciles_to_verified(tmp_path: Path) -> None:
    """Attempted txn with past expires_at: reconciles, 0 verify calls, 1 fetch, →verified."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")

    store = m.TransactionStore(tmp_path)

    txn = m.Transaction(transaction_id="t_attempted_exp", content_id="post_8f3a_20260726",
                        content_type="post", parent_post_id="", url="https://x",
                        raw_challenge_text="q", verification_code="c",
                        challenge_instructions="", expires_at=0.0,  # past
                        raw_create_response={}, state="attempted",
                        submitted_answer="11", attempted_at=time.time())
    store.save(txn)

    fresh_client = _MockClient(fetch_post_resp=_load_fixture("post_fetch_verified.json"))
    fresh_store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_verify(fresh_client, fresh_store, "t_attempted_exp", "11")
    assert exit_code == 0
    assert len(fresh_client.verify_calls) == 0
    assert len(fresh_client.fetch_post_calls) == 1
    loaded = fresh_store.load("t_attempted_exp")
    assert loaded is not None
    assert loaded.state == "verified"


def test_attempted_after_expiry_stays_attempted_if_pending(tmp_path: Path) -> None:
    """Attempted txn, past expiry, content still pending: stays attempted."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")

    store = m.TransactionStore(tmp_path)

    txn = m.Transaction(transaction_id="t_att_pend", content_id="p2",
                        content_type="post", parent_post_id="", url="https://x",
                        raw_challenge_text="q", verification_code="c",
                        challenge_instructions="", expires_at=0.0,
                        raw_create_response={}, state="attempted",
                        submitted_answer="11", attempted_at=time.time())
    store.save(txn)

    fresh_client = _MockClient(fetch_post_resp=_load_fixture("post_fetch_pending.json"))
    fresh_store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_verify(fresh_client, fresh_store, "t_att_pend", "11")
    assert exit_code == 1
    assert len(fresh_client.verify_calls) == 0
    loaded = fresh_store.load("t_att_pend")
    assert loaded is not None
    assert loaded.state == "attempted"


# ---------------------------------------------------------------------------
# Timeout recovery
# ---------------------------------------------------------------------------

def test_verify_timeout_recovers_if_verified(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    create = dict(_load_fixture("post_create_verified_pending.json"))
    create["post"]["verification"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _MockClient(create_post_resp=create, verify_resp={},
                         fetch_post_resp=_load_fixture("post_fetch_verified.json"))
    client._verify_raises = RuntimeError("timed out")
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"title": "x", "submolt": "s", "type": "post"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text())))
    assert m.cmd_verify(client, store, txn_id, "11") == 0
    assert store.load(txn_id).state == "verified"


# ---------------------------------------------------------------------------
# Security: no tokens in fixtures
# ---------------------------------------------------------------------------

def test_fixtures_contain_no_tokens() -> None:
    for name in sorted(FIXTURES.iterdir()):
        if name.suffix == ".json":
            text = name.read_text()
            assert "Bearer" not in text, f"{name.name}"
            assert "authorization" not in text.lower(), f"{name.name}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_run(*args):
    return subprocess.run([sys.executable, MOLTBOOK_WRITE, *args],
                          capture_output=True, text=True, cwd=str(REPO_ROOT))


def test_cli_create_help() -> None:
    assert _cli_run("create", "--help").returncode in (0, 2)


def test_cli_verify_help() -> None:
    assert _cli_run("verify", "--help").returncode in (0, 2)


def test_cli_no_subcommand() -> None:
    assert _cli_run().returncode != 0


# ---------------------------------------------------------------------------
# Dry-run transcript
# ---------------------------------------------------------------------------

def test_dry_run_transcript(tmp_path: Path) -> None:
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")
    create = dict(_load_fixture("post_create_verified_pending.json"))
    create["post"]["verification"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _MockClient(create_post_resp=create,
                         verify_resp=_load_fixture("verify_accepted.json"),
                         fetch_post_resp=_load_fixture("post_fetch_verified.json"))
    store = m.TransactionStore(tmp_path)

    sys.stdout.write("=== CREATE ===\n")
    assert m.cmd_create(client, store, json.dumps({
        "title": "What is the smallest practical receipt?",
        "submolt": "s",
        "type": "post"})) == 0

    stored = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_id = next(iter(stored))
    txn = stored[txn_id]
    print(f"TX: {txn_id}  content: {txn['content_id']}  type: {txn['content_type']}")
    print(f"CHALLENGE: {txn['raw_challenge_text']}  code: {txn['verification_code']}")

    sys.stdout.write("\n=== VERIFY (answer: 11) ===\n")
    assert m.cmd_verify(client, store, txn_id, "11") == 0
    loaded = store.load(txn_id)
    print(f"STATE: {loaded.state}")
    assert loaded.state == "verified"

    sys.stdout.write("\n=== VERIFY (second — rejected) ===\n")
    assert m.cmd_verify(client, store, txn_id, "11") == 1
    print("COMPLETE")


# ---------------------------------------------------------------------------
# Historical indeterminate comment reconciliation — real post_id shape
# ---------------------------------------------------------------------------

def test_derive_parent_from_create_response_real_shape(tmp_path: Path) -> None:
    """Real create shape (post_id only) yields parent post id."""
    m = _load_bridge_module()
    txn = m.Transaction(
        transaction_id="t1", content_id="c1", content_type="comment",
        parent_post_id="", url="", raw_challenge_text="",
        verification_code="", challenge_instructions="", expires_at=0.0,
        raw_create_response=_load_fixture("comment_create_real_shape.json"),
    )
    assert m._derive_parent_from_create_response(txn) == "parent_post_fixture_id"


def test_derive_parent_from_create_response_ambiguous(tmp_path: Path) -> None:
    """Both parent_post_id and post_id present but different → fail closed."""
    m = _load_bridge_module()
    raw = dict(_load_fixture("comment_create_real_shape.json"))
    raw["comment"]["parent_post_id"] = "other_parent"
    txn = m.Transaction(
        transaction_id="t1", content_id="c1", content_type="comment",
        parent_post_id="", url="", raw_challenge_text="",
        verification_code="", challenge_instructions="", expires_at=0.0,
        raw_create_response=raw,
    )
    assert m._derive_parent_from_create_response(txn) == ""


def test_derive_parent_from_create_response_missing(tmp_path: Path) -> None:
    """Neither parent_post_id nor post_id present → fail closed."""
    m = _load_bridge_module()
    raw = dict(_load_fixture("comment_create_real_shape.json"))
    raw["comment"].pop("post_id", None)
    raw["comment"].pop("parent_post_id", None)
    txn = m.Transaction(
        transaction_id="t1", content_id="c1", content_type="comment",
        parent_post_id="", url="", raw_challenge_text="",
        verification_code="", challenge_instructions="", expires_at=0.0,
        raw_create_response=raw,
    )
    assert m._derive_parent_from_create_response(txn) == ""


def test_fetch_content_object_recovers_empty_parent_via_list(tmp_path: Path) -> None:
    """Comment txn with empty parent_post_id reconciles via comments list."""
    m = _load_bridge_module()
    # fetch_post returns a post without comments → falls back to list
    parent_without = {"success": True,
                      "post": {"id": "parent_post_fixture_id", "comments": []}}
    client = _MockClient(
        fetch_post_resp=parent_without,
        fetch_comments_resp=_load_fixture("comment_list_real_shape_verified.json"),
    )
    txn = m.Transaction(
        transaction_id="t1",
        content_id="comment_fixture_real_shape",
        content_type="comment",
        parent_post_id="", url="", raw_challenge_text="",
        verification_code="", challenge_instructions="", expires_at=0.0,
        raw_create_response=_load_fixture("comment_create_real_shape.json"),
    )
    content = m._fetch_content_object(client, txn)
    assert content["id"] == "comment_fixture_real_shape"
    assert content["verification_status"] == "verified"
    assert "parent_post_fixture_id" in client.fetch_comments_calls


def test_cmd_verify_reconciles_indeterminate_comment_no_resubmit(tmp_path: Path) -> None:
    """Indeterminate comment txn is recovered read-only (no POST /verify)."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")

    client = _MockClient(
        fetch_post_resp={"success": True,
                         "post": {"id": "parent_post_fixture_id", "comments": []}},
        fetch_comments_resp=_load_fixture("comment_list_real_shape_verified.json"),
    )
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(
        transaction_id="hist1",
        content_id="comment_fixture_real_shape",
        content_type="comment",
        parent_post_id="", url="", raw_challenge_text="",
        verification_code="", challenge_instructions="", expires_at=0.0,
        raw_create_response=_load_fixture("comment_create_real_shape.json"),
        state="indeterminate",
    )
    store.save(txn)

    assert m.cmd_verify(client, store, "hist1", "anything") == 0
    loaded = store.load("hist1")
    assert loaded.state == "verified"
    assert client.verify_calls == []  # no POST /verify


def test_cmd_verify_indeterminate_comment_not_verified_stays(tmp_path: Path) -> None:
    """Indeterminate comment stays indeterminate when content not verified."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")

    client = _MockClient(
        fetch_post_resp={"success": True,
                         "post": {"id": "parent_post_fixture_id", "comments": []}},
        fetch_comments_resp=_load_fixture("comment_list_verified.json"),
    )
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(
        transaction_id="hist2",
        content_id="comment_fixture_real_shape",
        content_type="comment",
        parent_post_id="", url="", raw_challenge_text="",
        verification_code="", challenge_instructions="", expires_at=0.0,
        raw_create_response=_load_fixture("comment_create_real_shape.json"),
        state="indeterminate",
    )
    store.save(txn)

    assert m.cmd_verify(client, store, "hist2", "anything") == 1
    loaded = store.load("hist2")
    assert loaded.state == "indeterminate"
    assert client.verify_calls == []


def test_cmd_verify_other_terminal_states_still_rejected(tmp_path: Path) -> None:
    """Verified/other terminal comment txns are still rejected."""
    m = _load_bridge_module()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(m, "_get_token", lambda: "tok")

    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(
        transaction_id="done1", content_id="c1", content_type="comment",
        parent_post_id="p", url="", raw_challenge_text="",
        verification_code="", challenge_instructions="", expires_at=0.0,
        raw_create_response={}, state="verified", verified_at=123.0,
    )
    store.save(txn)
    client = _MockClient()
    assert m.cmd_verify(client, store, "done1", "x") == 1
    assert client.verify_calls == []
    assert client.fetch_post_calls == []
