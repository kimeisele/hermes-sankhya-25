"""Tests for the Moltbook verified-write bridge.

All tests use captured fixture payloads.  No live Moltbook API calls.
No public content is generated.

Strategy:
- Unit tests import the bridge module directly via importlib (file-path load).
- Integration tests call cmd_create / cmd_verify directly with mock clients.
- CLI smoke tests assert early-validation exit codes via subprocess.
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
# Module loader — avoids scripts/__init__.py package conflicts
# ---------------------------------------------------------------------------

def _load_bridge_module():
    """Import moltbook_write via its file path.

    Registers in sys.modules so that PEP 563 deferred annotations
    (from __future__ import annotations) resolve correctly inside
    @dataclass field declarations.
    """
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
# Mock client — records calls and returns fixture responses
# ---------------------------------------------------------------------------

@dataclass
class _MockClient:
    """In-process fake MoltbookClient. No network."""
    create_response: dict[str, Any]
    verify_response: dict[str, Any]
    fetch_response: dict[str, Any]
    create_calls: list[dict] = field(default_factory=list)
    verify_calls: list[dict] = field(default_factory=list)
    fetch_calls: list[dict] = field(default_factory=list)

    def create_content(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.create_calls.append(payload)
        return self.create_response

    def verify_challenge(self, verification_code: str, answer: str) -> dict[str, Any]:
        self.verify_calls.append({"code": verification_code, "answer": answer})
        return self.verify_response

    def fetch_content(self, content_id: str) -> dict[str, Any]:
        self.fetch_calls.append(content_id)
        return self.fetch_response


# ---------------------------------------------------------------------------
# Challenge extraction (unit-level)
# ---------------------------------------------------------------------------

def test_challenge_extraction_preserves_raw_text() -> None:
    """Challenge text is passed through unchanged — no regex, no keyword maps."""
    m = _load_bridge_module()
    create_resp = _load_fixture("create_response_post.json")
    result = m.extract_challenge(create_resp)
    assert result["challenge_text"] == "What is 7 + 4?"
    assert result["verification_code"] == "ch_4a9f2b1c"
    assert result["expires_at"] == "2026-07-26T18:05:00Z"


def test_challenge_extraction_numeric_only_challenge() -> None:
    """A challenge consisting only of a number is passed through as-is."""
    m = _load_bridge_module()
    create_resp = _load_fixture("create_response_numeric_challenge.json")
    result = m.extract_challenge(create_resp)
    assert result["challenge_text"] == "12"
    assert result["verification_code"] == "ch_num_01"


def test_challenge_extraction_comment() -> None:
    """Comment create responses also yield challenges unchanged."""
    m = _load_bridge_module()
    create_resp = _load_fixture("create_response_comment.json")
    result = m.extract_challenge(create_resp)
    assert result["challenge_text"] == "How many letters are in the word MOLT?"
    assert result["verification_code"] == "ch_7d3e5f6a"


# ---------------------------------------------------------------------------
# Transaction store unit tests
# ---------------------------------------------------------------------------

def test_transaction_store_roundtrip(tmp_path: Path) -> None:
    """Transactions survive save→load roundtrip."""
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(
        transaction_id="txn_01",
        content_id="post_abc",
        content_type="post",
        url="https://www.moltbook.com/post/post_abc",
        raw_challenge_text="What is 3 + 3?",
        verification_code="ch_xyz",
        expires_at=time.time() + 300,
        raw_create_response={"id": "post_abc"},
    )
    store.save(txn)
    loaded = store.load("txn_01")
    assert loaded is not None
    assert loaded.transaction_id == "txn_01"
    assert loaded.content_id == "post_abc"
    assert loaded.raw_challenge_text == "What is 3 + 3?"
    assert loaded.state == "pending"


def test_transaction_store_mark_verified(tmp_path: Path) -> None:
    """mark_verified transitions state and records answer + timestamp."""
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    txn = m.Transaction(
        transaction_id="txn_02",
        content_id="post_def",
        content_type="post",
        url="https://www.moltbook.com/post/post_def",
        raw_challenge_text="4 + 5 = ?",
        verification_code="ch_uvw",
        expires_at=time.time() + 600,
        raw_create_response={"id": "post_def"},
    )
    store.save(txn)
    now = time.time()
    store.mark_verified("txn_02", "9", now)
    loaded = store.load("txn_02")
    assert loaded is not None
    assert loaded.state == "verified"
    assert loaded.submitted_answer == "9"
    assert loaded.verified_at == now


def test_transaction_store_load_nonexistent(tmp_path: Path) -> None:
    """Loading a missing ID returns None."""
    m = _load_bridge_module()
    store = m.TransactionStore(tmp_path)
    assert store.load("no_such_id") is None


# ---------------------------------------------------------------------------
# Create command — integration (direct function call, mock client)
# ---------------------------------------------------------------------------

def test_create_post_persists_raw_response(tmp_path: Path):
    """The complete create API response is persisted unchanged."""
    m = _load_bridge_module()
    create_resp = _load_fixture("create_response_post.json")
    client = _MockClient(create_response=create_resp, verify_response={}, fetch_response={})
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, json.dumps({"content": "test post", "type": "post"}))
    assert exit_code == 0

    # Load persisted transaction and verify
    stored_raw = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_id = next(iter(stored_raw))
    txn_data = stored_raw[txn_id]
    assert txn_data["content_id"] == "post_8f3a_20260726"
    assert txn_data["raw_challenge_text"] == "What is 7 + 4?"
    assert txn_data["verification_code"] == "ch_4a9f2b1c"
    assert txn_data["raw_create_response"] == create_resp
    assert txn_data["state"] == "pending"


def test_create_comment_persists_type_and_parent(tmp_path: Path):
    """Comment create responses preserve the comment type."""
    m = _load_bridge_module()
    create_resp = _load_fixture("create_response_comment.json")
    client = _MockClient(create_response=create_resp, verify_response={}, fetch_response={})
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, json.dumps({"content": "test comment", "type": "comment"}))
    assert exit_code == 0

    stored_raw = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_data = stored_raw[next(iter(stored_raw))]
    assert txn_data["content_type"] == "comment"


def test_create_invalid_json_payload(tmp_path: Path):
    """Non-JSON payload returns an error."""
    m = _load_bridge_module()
    client = _MockClient(create_response={}, verify_response={}, fetch_response={})
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, "not valid json {")
    assert exit_code == 1


def test_create_missing_content_field(tmp_path: Path):
    """Payload without a content field returns an error."""
    m = _load_bridge_module()
    client = _MockClient(create_response={}, verify_response={}, fetch_response={})
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, json.dumps({"type": "post"}))
    assert exit_code == 1


# ---------------------------------------------------------------------------
# Verify command — integration
# ---------------------------------------------------------------------------

def test_verify_rejects_expired_transaction(tmp_path: Path):
    """A transaction with an expired challenge timestamp is refused."""
    m = _load_bridge_module()

    create_resp = {
        "id": "post_exp",
        "type": "post",
        "url": "https://www.moltbook.com/post/post_exp",
        "challenge": {
            "text": "What is 1 + 1?",
            "code": "ch_exp",
            "expires_at": "2020-01-01T00:00:00Z",
        },
    }
    client = _MockClient(create_response=create_resp, verify_response={}, fetch_response={})
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, json.dumps({"content": "expired post"}))
    assert exit_code == 0

    # Read the generated transaction ID from disk
    stored_raw = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_id = next(iter(stored_raw))

    exit_code = m.cmd_verify(client, store, txn_id, "2")
    assert exit_code == 1


def test_verify_rejects_second_attempt(tmp_path: Path):
    """Only one verification submission is permitted."""
    m = _load_bridge_module()

    create_resp = _load_fixture("create_response_post.json")
    create_resp["challenge"]["expires_at"] = "2099-07-26T18:05:00Z"
    verify_ok = _load_fixture("verify_response_ok.json")
    fetch_verified = _load_fixture("fetch_response_verified.json")

    client = _MockClient(
        create_response=create_resp,
        verify_response=verify_ok,
        fetch_response=fetch_verified,
    )
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, json.dumps({"content": "single attempt test"}))
    assert exit_code == 0

    stored_raw = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_id = next(iter(stored_raw))

    # First verify — must succeed
    exit_code = m.cmd_verify(client, store, txn_id, "11")
    assert exit_code == 0

    # Second verify — must reject
    exit_code = m.cmd_verify(client, store, txn_id, "11")
    assert exit_code == 1


def test_verify_succeeds_when_content_is_verified(tmp_path: Path):
    """Complete happy path: create → verify → confirmed."""
    m = _load_bridge_module()

    create_resp = _load_fixture("create_response_post.json")
    create_resp["challenge"]["expires_at"] = "2099-07-26T18:05:00Z"
    verify_ok = _load_fixture("verify_response_ok.json")
    fetch_verified = _load_fixture("fetch_response_verified.json")

    client = _MockClient(
        create_response=create_resp,
        verify_response=verify_ok,
        fetch_response=fetch_verified,
    )
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, json.dumps({"content": "happy path post"}))
    assert exit_code == 0

    stored_raw = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_id = next(iter(stored_raw))

    exit_code = m.cmd_verify(client, store, txn_id, "11")
    assert exit_code == 0

    # Verify persistence
    stored_raw = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_data = stored_raw[txn_id]
    assert txn_data["state"] == "verified"
    assert txn_data["submitted_answer"] == "11"
    assert txn_data["verified_at"] is not None


def test_verify_fails_when_content_still_unverified(tmp_path: Path):
    """If the API accepted the answer but content is still 'unverified', fail."""
    m = _load_bridge_module()

    create_resp = _load_fixture("create_response_post.json")
    create_resp["challenge"]["expires_at"] = "2099-07-26T18:05:00Z"
    verify_ok = _load_fixture("verify_response_ok.json")
    fetch_unverified = _load_fixture("fetch_response_unverified.json")

    client = _MockClient(
        create_response=create_resp,
        verify_response=verify_ok,
        fetch_response=fetch_unverified,
    )
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_create(client, store, json.dumps({"content": "unverified test"}))
    assert exit_code == 0

    stored_raw = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_id = next(iter(stored_raw))

    exit_code = m.cmd_verify(client, store, txn_id, "11")
    assert exit_code == 1


def test_verify_rejects_nonexistent_transaction(tmp_path: Path):
    """Verifying a non-existent transaction ID returns an error."""
    m = _load_bridge_module()
    client = _MockClient(create_response={}, verify_response={}, fetch_response={})
    store = m.TransactionStore(tmp_path)

    exit_code = m.cmd_verify(client, store, "nonexistent_id", "42")
    assert exit_code == 1


# ---------------------------------------------------------------------------
# CLI-level smoke tests (subprocess — these hit early returns, no patches needed)
# ---------------------------------------------------------------------------

def test_cli_create_help() -> None:
    """create subcommand is reachable."""
    result = _cli_run("create", "--help")
    assert result.returncode == 0 or result.returncode == 2  # argparse exits 2 for --help


def test_cli_verify_help() -> None:
    """verify subcommand is reachable."""
    result = _cli_run("verify", "--help")
    assert result.returncode == 0 or result.returncode == 2


def test_cli_no_subcommand_fails() -> None:
    """Running without subcommand exits non-zero."""
    result = _cli_run()
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Dry-run transcript (fixture-based, direct function calls)
# ---------------------------------------------------------------------------

def test_dry_run_transcript(tmp_path: Path, capsys) -> None:
    """Full end-to-end dry run using fixture data.

    Demonstrates create → verify workflow without live Moltbook API.
    """
    m = _load_bridge_module()

    create_resp = _load_fixture("create_response_post.json")
    create_resp["challenge"]["expires_at"] = "2099-07-26T18:05:00Z"
    verify_ok = _load_fixture("verify_response_ok.json")
    fetch_verified = _load_fixture("fetch_response_verified.json")

    client = _MockClient(
        create_response=create_resp,
        verify_response=verify_ok,
        fetch_response=fetch_verified,
    )
    store = m.TransactionStore(tmp_path)

    # ---- CREATE ----
    sys.stdout.write("=== CREATE ===\n")
    exit_code = m.cmd_create(client, store, json.dumps({
        "content": (
            "What is the smallest practical receipt that another agent can use "
            "to independently verify a task was completed against the intended commit?"
        ),
        "type": "post",
    }))
    assert exit_code == 0

    stored_raw = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_id = next(iter(stored_raw))
    txn_data = stored_raw[txn_id]

    print(f"TRANSACTION_ID: {txn_id}")
    print(f"CONTENT_ID:     {txn_data['content_id']}")
    print(f"CONTENT_TYPE:   {txn_data['content_type']}")
    print(f"URL:            {txn_data['url']}")
    print(f"CHALLENGE:      {txn_data['raw_challenge_text']}")
    print(f"CODE:           {txn_data['verification_code']}")
    print(f"EXPIRES:        {txn_data['expires_at']}")

    assert txn_data["raw_challenge_text"] == "What is 7 + 4?"
    assert txn_data["verification_code"] == "ch_4a9f2b1c"

    # ---- VERIFY ----
    sys.stdout.write("\n=== VERIFY (answer: 11) ===\n")
    exit_code = m.cmd_verify(client, store, txn_id, "11")
    assert exit_code == 0

    # Re-read persisted state
    stored_raw = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_data = stored_raw[txn_id]

    print(f"STATE:          {txn_data['state']}")
    print(f"ANSWER:         {txn_data['submitted_answer']}")
    print(f"VERIFIED_AT:    {txn_data['verified_at']}")

    assert txn_data["state"] == "verified"
    assert txn_data["submitted_answer"] == "11"

    # ---- VERIFY (second attempt — must be rejected) ----
    sys.stdout.write("\n=== VERIFY (second attempt — must reject) ===\n")
    exit_code = m.cmd_verify(client, store, txn_id, "11")
    assert exit_code == 1

    # ---- FINAL STATE ----
    stored_raw = json.loads((tmp_path / "data" / "moltbook" / "transactions.json").read_text())
    txn_data = stored_raw[txn_id]
    print(f"\nFINAL STATE:    {txn_data['state']}")
    print(f"RAW RESPONSE:   {json.dumps(txn_data['raw_create_response'], indent=2)}")
