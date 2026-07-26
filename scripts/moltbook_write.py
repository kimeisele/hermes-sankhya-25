#!/usr/bin/env python3
"""Minimal Moltbook verified-write bridge for Hermes.

Two commands only:

    moltbook_write create <payload>
    moltbook_write verify <transaction-id> <answer>

The bridge persists the raw create response unchanged, extracts the challenge
without interpretation, enforces single-attempt verification, checks expiry,
and requires the final content state to be 'verified'.

Prohibited: regex interpretation, operation-keyword maps, character-collapse,
auto-repost, auto-retry, multiple submissions.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Moltbook API client
# ---------------------------------------------------------------------------

MOLTBOOK_BASE = os.environ.get("MOLTBOOK_API_URL", "https://www.moltbook.com/api/v1")


class MoltbookClient:
    """Minimal Moltbook HTTP client with injectable transport for testing."""

    def __init__(self, base_url: str = MOLTBOOK_BASE) -> None:
        self.base_url = base_url

    def create_content(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to create a post or comment. Returns the raw API response."""
        return self._api_call("POST", "/content", payload)

    def verify_challenge(self, verification_code: str, answer: str) -> dict[str, Any]:
        """POST to submit a verification answer."""
        return self._api_call("POST", "/verify", {
            "verification_code": verification_code,
            "answer": answer,
        })

    def fetch_content(self, content_id: str) -> dict[str, Any]:
        """GET the current state of created content."""
        return self._api_call("GET", f"/content/{content_id}")

    # -- transport (overridden in tests) ------------------------------------

    def _api_call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Real HTTP transport. Overridden in tests via monkeypatch."""
        import urllib.error
        import urllib.request

        url = f"{self.base_url}{path}"
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        token = os.environ.get("MOLTBOOK_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Moltbook API {method} {path} returned {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Moltbook API unreachable: {exc}") from exc


# ---------------------------------------------------------------------------
# Transaction model
# ---------------------------------------------------------------------------

@dataclass
class Transaction:
    transaction_id: str
    content_id: str
    content_type: str
    url: str
    raw_challenge_text: str
    verification_code: str
    expires_at: float  # Unix timestamp
    raw_create_response: dict[str, Any]
    state: str = "pending"       # pending | verified | expired | failed
    submitted_answer: str | None = None
    submitted_at: float | None = None
    verified_at: float | None = None


# ---------------------------------------------------------------------------
# Transaction store (JSON file on disk)
# ---------------------------------------------------------------------------

class TransactionStore:
    """Persist transactions to a JSON file under data/moltbook/."""

    def __init__(self, repo_root: Path) -> None:
        self._dir = repo_root / "data" / "moltbook"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "transactions.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    def save(self, txn: Transaction) -> None:
        data = self._load()
        data[txn.transaction_id] = _txn_to_dict(txn)
        self._save(data)

    def load(self, transaction_id: str) -> Transaction | None:
        data = self._load()
        raw = data.get(transaction_id)
        if raw is None:
            return None
        return _txn_from_dict(raw)

    def mark_verified(self, transaction_id: str, answer: str, verified_at: float) -> None:
        data = self._load()
        raw = data.get(transaction_id)
        if raw is None:
            return
        raw["state"] = "verified"
        raw["submitted_answer"] = answer
        raw["submitted_at"] = time.time()
        raw["verified_at"] = verified_at
        data[transaction_id] = raw
        self._save(data)


def _txn_to_dict(txn: Transaction) -> dict[str, Any]:
    return {
        "transaction_id": txn.transaction_id,
        "content_id": txn.content_id,
        "content_type": txn.content_type,
        "url": txn.url,
        "raw_challenge_text": txn.raw_challenge_text,
        "verification_code": txn.verification_code,
        "expires_at": txn.expires_at,
        "raw_create_response": txn.raw_create_response,
        "state": txn.state,
        "submitted_answer": txn.submitted_answer,
        "submitted_at": txn.submitted_at,
        "verified_at": txn.verified_at,
    }


def _txn_from_dict(d: dict[str, Any]) -> Transaction:
    return Transaction(
        transaction_id=d["transaction_id"],
        content_id=d["content_id"],
        content_type=d["content_type"],
        url=d["url"],
        raw_challenge_text=d["raw_challenge_text"],
        verification_code=d["verification_code"],
        expires_at=float(d["expires_at"]),
        raw_create_response=d["raw_create_response"],
        state=d.get("state", "pending"),
        submitted_answer=d.get("submitted_answer"),
        submitted_at=float(d["submitted_at"]) if d.get("submitted_at") else None,
        verified_at=float(d["verified_at"]) if d.get("verified_at") else None,
    )


# ---------------------------------------------------------------------------
# Challenge extraction (passthrough only — no interpretation)
# ---------------------------------------------------------------------------

def extract_challenge(create_response: dict[str, Any]) -> dict[str, str]:
    """Extract the raw challenge fields from the create response.

    Returns exactly what the API returned.  No regex.  No keyword maps.
    No character collapse.  No normalization.
    """
    challenge = create_response.get("challenge", {})
    return {
        "challenge_text": challenge.get("text", ""),
        "verification_code": challenge.get("code", ""),
        "expires_at": challenge.get("expires_at", ""),
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _uuid_short() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def cmd_create(client: MoltbookClient, store: TransactionStore, payload: str) -> int:
    """Create a post or comment and persist the raw response.

    Parses *payload* as JSON and POSTs it to the Moltbook content endpoint.
    The complete API response is saved unchanged.  Only the challenge fields
    are extracted (as-is) for printing to Hermes.  No answer is submitted.
    """
    try:
        body = json.loads(payload)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"Invalid JSON payload: {exc}"}))
        return 1

    # Ensure required fields
    if "content" not in body and "text" not in body and "body" not in body:
        print(json.dumps({"error": "Payload must contain a content field (content, text, or body)"}))
        return 1

    try:
        raw = client.create_content(body)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    transaction_id = _uuid_short()
    content_id = raw.get("id", "")
    content_type = raw.get("type", body.get("type", "post"))
    content_url = raw.get("url", "")

    chall = extract_challenge(raw)

    txn = Transaction(
        transaction_id=transaction_id,
        content_id=content_id,
        content_type=content_type,
        url=content_url,
        raw_challenge_text=chall["challenge_text"],
        verification_code=chall["verification_code"],
        expires_at=_parse_timestamp(chall["expires_at"]),
        raw_create_response=raw,
    )
    store.save(txn)

    # Output the challenge unchanged for Hermes
    output = {
        "transaction_id": transaction_id,
        "content_id": content_id,
        "content_type": content_type,
        "url": content_url,
        "challenge": chall,
    }
    print(json.dumps(output, indent=2))
    return 0


def cmd_verify(client: MoltbookClient, store: TransactionStore,
               transaction_id: str, answer: str) -> int:
    """Submit a verification answer for a pending transaction.

    Enforces:
    - Transaction must exist
    - Must not be expired
    - Must be in 'pending' state (exactly one submission)
    - Final content state must be 'verified'
    """
    txn = store.load(transaction_id)
    if txn is None:
        print(json.dumps({"error": f"No transaction found for id: {transaction_id}"}))
        return 1

    if time.time() > txn.expires_at:
        print(json.dumps({"error": "Transaction expired", "expires_at": txn.expires_at}))
        return 1

    if txn.state != "pending":
        print(json.dumps({
            "error": f"Transaction already in state '{txn.state}'. Only one verification permitted.",
        }))
        return 1

    # Submit the answer
    try:
        verify_resp = client.verify_challenge(txn.verification_code, answer)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    # Refetch content to confirm verified state
    try:
        content = client.fetch_content(txn.content_id)
    except RuntimeError as exc:
        print(json.dumps({"error": f"Content refetch failed: {exc}"}))
        return 1

    verification_status = content.get("verification_status", "")

    if verification_status != "verified":
        print(json.dumps({
            "error": f"Verification incomplete. Status: '{verification_status}'",
            "verification_response": verify_resp,
            "content": content,
        }))
        return 1

    # Success — persist and report
    verified_at = time.time()
    store.mark_verified(transaction_id, answer, verified_at)

    receipt = {
        "transaction_id": transaction_id,
        "content_id": txn.content_id,
        "content_type": txn.content_type,
        "url": txn.url,
        "verification_status": "verified",
        "verified_at": verified_at,
        "status": "complete",
    }
    print(json.dumps(receipt, indent=2))
    return 0


def _parse_timestamp(raw: str) -> float:
    """Parse an ISO-8601 timestamp string to a Unix float.

    Returns 0.0 on parse failure so that an empty/missing expiry field
    leaves the transaction effectively expired (epoch < now).
    """
    if not raw:
        return 0.0
    try:
        import datetime
        # Handle 'Z' suffix
        s = raw.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="moltbook_write",
        description="Minimal Moltbook verified-write bridge for Hermes",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create", help="Create a post or comment")
    create_parser.add_argument("payload", help="JSON payload string")

    verify_parser = sub.add_parser("verify", help="Submit a verification answer")
    verify_parser.add_argument("transaction_id", help="Transaction ID from create")
    verify_parser.add_argument("answer", help="Numeric verification answer")

    args = parser.parse_args()
    repo_root = _repo_root()
    client = MoltbookClient()
    store = TransactionStore(repo_root)

    if args.command == "create":
        return cmd_create(client, store, args.payload)
    elif args.command == "verify":
        return cmd_verify(client, store, args.transaction_id, args.answer)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
