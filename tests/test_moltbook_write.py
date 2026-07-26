"""Tests for the Moltbook verified-write bridge.

All tests use captured fixture payloads.  No live API calls.
No public content is generated.

Covers: challenge extraction, transaction persistence, expiry,
single-attempt enforcement, timeout/indeterminate recovery,
verified-state enforcement, and CLI reachability.
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
        "moltbook_write", SCRIPTS / "moltbook_write.py"
    )
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
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------

@dataclass
class _MockClient:
    create_response: dict[str, Any]
    verify_response: dict[str, Any]
    fetch_response: dict[str, Any]
    create_calls: list[dict] = field(default_factory=list)
    verify_calls: list[dict] = field(default_factory=list)
    fetch_calls: list[dict] = field(default_factory=list)
    _verify_raises: RuntimeError | None = None
    _fetch_raises: RuntimeError | None = None

    def create_content(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.create_calls.append(payload)
        return self.create_response

    def verify_challenge(self, verification_code: str, answer: str) -> dict[str, Any]:
        self.verify_calls.append({"code": verification_code, "answer": answer})
        if self._verify_raises:
            raise self._verify_raises
        return self.verify_response

    def fetch_content(self, content_id: str) -> dict[str, Any]:
        self.fetch_calls.append(content_id)
        if self._fetch_raises:
            raise self._fetch_raises
        return self.fetch_response


# ---------------------------------------------------------------------------
# Challenge extraction
# ---------------------------------------------------------------------------

def test_challenge_extraction_preserves_raw_text() -> None:
    m = _load_bridge_module()
    result = m.extract_challenge(_load_fixture("create_response_post.json"))
    assert result["challenge_text"] == "What is 7 + 4?"
    assert result["verification_code"] == "ch_4a9f2b1c"
    assert result["expires_at"] == "2026-07-26T18:05:00Z"


def test_challenge_extraction_numeric_only() -> None:
    m = _load_bridge_module()
    result = m.extract_challenge(_load_fixture("create_response_numeric_challenge.json"))
    assert result["challenge_text"] == "12"
    assert result["verification_code"] == "ch_num_01"


def test_challenge_extraction_comment() -> None:
    m = _load_bridge_module()
    result = m.extract_challenge(_load_fixture("create_response_comment.json"))
    assert result["challenge_text"] == "How many letters are in the word MOLT?"
    assert result["verification_code"] == "ch_7d3e5f6a"


# ---------------------------------------------------------------------------
# Transaction store
# ---------------------------------------------------------------------------

def test_store_roundtrip(tmp_path: Path) -> None:
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(
        transaction_id="txn_01", content_id="p1", content_type="post",
        url="https://m.example.com/p1", raw_challenge_text="3+3?",
        verification_code="ch_x", expires_at=time.time() + 300,
        raw_create_response={"id": "p1"},
    )
    store.save(txn)
    loaded = store.load("txn_01")
    assert loaded is not None
    assert loaded.content_id == "p1"
    assert loaded.state == "pending"


def test_store_mark_verified(tmp_path: Path) -> None:
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(
        transaction_id="txn_02", content_id="p2", content_type="post",
        url="https://m.example.com/p2", raw_challenge_text="q",
        verification_code="ch_y", expires_at=time.time() + 600,
        raw_create_response={"id": "p2"},
    )
    store.save(txn)
    store.mark_verified("txn_02")
    loaded = store.load("txn_02")
    assert loaded is not None
    assert loaded.state == "verified"
    assert loaded.verified_at is not None


def test_store_mark_indeterminate(tmp_path: Path) -> None:
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(
        transaction_id="txn_03", content_id="p3", content_type="post",
        url="https://m.example.com/p3", raw_challenge_text="q",
        verification_code="ch_z", expires_at=time.time() + 600,
        raw_create_response={"id": "p3"},
    )
    store.save(txn)
    store.mark_indeterminate("txn_03", "timeout")
    loaded = store.load("txn_03")
    assert loaded is not None
    assert loaded.state == "indeterminate"


def test_store_load_nonexistent(tmp_path: Path) -> None:
    m = _load_bridge_module()
    assert m.TransactionStore(tmp_path).load("no_such") is None


def test_store_permissions_restrictive(tmp_path: Path) -> None:
    """Transaction file is created with owner-only permissions."""
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(
        transaction_id="txn_p", content_id="pp", content_type="post",
        url="https://m.example.com/pp", raw_challenge_text="?",
        verification_code="ch_p", expires_at=time.time() + 300,
        raw_create_response={"id": "pp"},
    )
    store.save(txn)
    st = store.path().stat()
    mode = st.st_mode & 0o777
    # Should not be world/group readable
    assert mode & 0o077 == 0, f"permissions too open: {oct(mode)}"


# ---------------------------------------------------------------------------
# Create command
# ---------------------------------------------------------------------------

def _make_client(create=None, verify=None, fetch=None) -> _MockClient:
    return _MockClient(
        create_response=create or {}, verify_response=verify or {},
        fetch_response=fetch or {},
    )


def test_create_post_persists(tmp_path: Path) -> None:
    m = _load_bridge_module()
    resp = _load_fixture("create_response_post.json")
    client = _make_client(create=resp)
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, json.dumps({"content": "test"}))
    assert exit_code == 0

    txn_path = tmp_path / "data" / "moltbook" / "transactions.json"
    stored = json.loads(txn_path.read_text())
    txn_data = stored[next(iter(stored))]
    assert txn_data["content_id"] == "post_8f3a_20260726"
    assert txn_data["raw_challenge_text"] == "What is 7 + 4?"
    assert txn_data["raw_create_response"] == resp
    assert txn_data["state"] == "pending"


def test_create_comment_persists(tmp_path: Path) -> None:
    m = _load_bridge_module()
    resp = _load_fixture("create_response_comment.json")
    client = _make_client(create=resp)
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, json.dumps({"content": "c", "type": "comment"}))
    assert exit_code == 0

    txn_path = tmp_path / "data" / "moltbook" / "transactions.json"
    stored = json.loads(txn_path.read_text())
    assert stored[next(iter(stored))]["content_type"] == "comment"


def test_create_invalid_json(tmp_path: Path) -> None:
    m = _load_bridge_module()
    assert m.cmd_create(_make_client(), m.TransactionStore(tmp_path), "bad{") == 1


def test_create_missing_content(tmp_path: Path) -> None:
    m = _load_bridge_module()
    assert m.cmd_create(_make_client(), m.TransactionStore(tmp_path),
                        json.dumps({"type": "post"})) == 1


# ---------------------------------------------------------------------------
# Verify — expiry
# ---------------------------------------------------------------------------

def test_verify_rejects_expired(tmp_path: Path) -> None:
    m = _load_bridge_module()
    resp = _load_fixture("create_response_expired.json")
    client = _make_client(create=resp)
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, json.dumps({"content": "x"}))
    assert exit_code == 0

    txn_path = tmp_path / "data" / "moltbook" / "transactions.json"
    txn_id = next(iter(json.loads(txn_path.read_text())))

    exit_code = m.cmd_verify(client, store, txn_id, "8")
    assert exit_code == 1


# ---------------------------------------------------------------------------
# Verify — single-attempt
# ---------------------------------------------------------------------------

def test_verify_rejects_second_attempt(tmp_path: Path) -> None:
    m = _load_bridge_module()
    create_resp = dict(_load_fixture("create_response_post.json"))
    create_resp["challenge"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _make_client(
        create=create_resp,
        verify=_load_fixture("verify_response_ok.json"),
        fetch=_load_fixture("fetch_response_verified.json"),
    )
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text()
    )))

    # First — succeeds
    assert m.cmd_verify(client, store, txn_id, "11") == 0

    # Second — rejected (terminal verified state)
    assert m.cmd_verify(client, store, txn_id, "11") == 1


# ---------------------------------------------------------------------------
# Verify — happy path
# ---------------------------------------------------------------------------

def test_verify_succeeds_when_verified(tmp_path: Path) -> None:
    m = _load_bridge_module()
    create_resp = dict(_load_fixture("create_response_post.json"))
    create_resp["challenge"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _make_client(
        create=create_resp,
        verify=_load_fixture("verify_response_ok.json"),
        fetch=_load_fixture("fetch_response_verified.json"),
    )
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text()
    )))

    assert m.cmd_verify(client, store, txn_id, "11") == 0
    loaded = store.load(txn_id)
    assert loaded is not None
    assert loaded.state == "verified"


# ---------------------------------------------------------------------------
# Verify — unverified after submit → indeterminate
# ---------------------------------------------------------------------------

def test_verify_fails_when_content_unverified(tmp_path: Path) -> None:
    m = _load_bridge_module()
    create_resp = dict(_load_fixture("create_response_post.json"))
    create_resp["challenge"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _make_client(
        create=create_resp,
        verify=_load_fixture("verify_response_ok.json"),
        fetch=_load_fixture("fetch_response_unverified.json"),
    )
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text()
    )))

    exit_code = m.cmd_verify(client, store, txn_id, "11")
    assert exit_code == 1
    loaded = store.load(txn_id)
    assert loaded is not None
    assert loaded.state == "indeterminate"


# ---------------------------------------------------------------------------
# Verify — timeout recovery
# ---------------------------------------------------------------------------

def test_verify_timeout_recovers_when_content_is_verified(tmp_path: Path) -> None:
    """If the verify call raises (timeout) but refetch shows verified, succeed."""
    m = _load_bridge_module()
    create_resp = dict(_load_fixture("create_response_post.json"))
    create_resp["challenge"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _make_client(
        create=create_resp,
        verify={},
        fetch=_load_fixture("fetch_response_verified.json"),
    )
    client._verify_raises = RuntimeError("timed out")
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text()
    )))

    exit_code = m.cmd_verify(client, store, txn_id, "11")
    assert exit_code == 0
    loaded = store.load(txn_id)
    assert loaded is not None
    assert loaded.state == "verified"


def test_verify_timeout_marks_indeterminate_when_not_verified(tmp_path: Path) -> None:
    """Timeout + refetch shows unverified → indeterminate, not retried."""
    m = _load_bridge_module()
    create_resp = dict(_load_fixture("create_response_post.json"))
    create_resp["challenge"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _make_client(
        create=create_resp,
        verify={},
        fetch=_load_fixture("fetch_response_unverified.json"),
    )
    client._verify_raises = RuntimeError("timed out")
    store = m.TransactionStore(tmp_path)

    assert m.cmd_create(client, store, json.dumps({"content": "x"})) == 0
    txn_id = next(iter(json.loads(
        (tmp_path / "data" / "moltbook" / "transactions.json").read_text()
    )))

    exit_code = m.cmd_verify(client, store, txn_id, "11")
    assert exit_code == 1
    loaded = store.load(txn_id)
    assert loaded is not None
    assert loaded.state == "indeterminate"

    # A second attempt must still be rejected
    client._verify_raises = None
    client.verify_response = _load_fixture("verify_response_ok.json")
    client.fetch_response = _load_fixture("fetch_response_verified.json")
    assert m.cmd_verify(client, store, txn_id, "11") == 1


# ---------------------------------------------------------------------------
# Verify — nonexistent
# ---------------------------------------------------------------------------

def test_verify_nonexistent(tmp_path: Path) -> None:
    m = _load_bridge_module()
    assert m.cmd_verify(_make_client(), m.TransactionStore(tmp_path), "no", "42") == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_create_help() -> None:
    r = _cli_run("create", "--help")
    assert r.returncode in (0, 2)


def test_cli_verify_help() -> None:
    r = _cli_run("verify", "--help")
    assert r.returncode in (0, 2)


def test_cli_no_subcommand_fails() -> None:
    assert _cli_run().returncode != 0


# ---------------------------------------------------------------------------
# Security: no tokens in fixture payloads
# ---------------------------------------------------------------------------

def test_fixtures_contain_no_tokens() -> None:
    """No fixture payload contains a Bearer token or Authorization header."""
    for name in FIXTURES.iterdir():
        if name.suffix == ".json":
            text = name.read_text()
            assert "Bearer" not in text, f"Fixture {name.name} contains 'Bearer'"
            assert "authorization" not in text.lower(), \
                f"Fixture {name.name} contains 'authorization'"


# ---------------------------------------------------------------------------
# Dry-run transcript
# ---------------------------------------------------------------------------

def test_dry_run_transcript(tmp_path: Path) -> None:
    """Full create → verify fixture-based dry run."""
    m = _load_bridge_module()
    create_resp = dict(_load_fixture("create_response_post.json"))
    create_resp["challenge"]["expires_at"] = "2099-07-26T18:05:00Z"

    client = _make_client(
        create=create_resp,
        verify=_load_fixture("verify_response_ok.json"),
        fetch=_load_fixture("fetch_response_verified.json"),
    )
    store = m.TransactionStore(tmp_path)

    # CREATE
    sys.stdout.write("=== CREATE ===\n")
    assert m.cmd_create(client, store, json.dumps({
        "content": "What is the smallest practical receipt?",
        "type": "post",
    })) == 0

    stored = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_id = next(iter(stored))
    txn = stored[txn_id]

    print(f"TRANSACTION_ID: {txn_id}")
    print(f"CONTENT_ID:     {txn['content_id']}")
    print(f"CONTENT_TYPE:   {txn['content_type']}")
    print(f"URL:            {txn['url']}")
    print(f"CHALLENGE:      {txn['raw_challenge_text']}")
    print(f"CODE:           {txn['verification_code']}")
    print(f"EXPIRES:        {txn['expires_at']}")
    print(f"STATE:          {txn['state']}")

    assert txn["raw_challenge_text"] == "What is 7 + 4?"
    assert txn["state"] == "pending"

    # VERIFY
    sys.stdout.write("\n=== VERIFY (answer: 11) ===\n")
    assert m.cmd_verify(client, store, txn_id, "11") == 0

    loaded = store.load(txn_id)
    assert loaded is not None
    print(f"STATE:          {loaded.state}")
    print(f"ANSWER:         {loaded.submitted_answer}")
    print(f"VERIFIED_AT:    {loaded.verified_at}")

    assert loaded.state == "verified"
    assert loaded.submitted_answer == "11"

    # SECOND ATTEMPT (rejected)
    sys.stdout.write("\n=== VERIFY (second attempt — rejected) ===\n")
    assert m.cmd_verify(client, store, txn_id, "11") == 1

    print("FINAL STATE:    verified (unchanged)")
    print("COMPLETE")
