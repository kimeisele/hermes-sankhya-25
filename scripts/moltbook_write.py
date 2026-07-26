#!/usr/bin/env python3
"""Minimal Moltbook verified-write bridge for Hermes.

Two commands only:

    moltbook_write create <payload>
    moltbook_write verify <transaction-id> <answer>

The bridge persists the raw API response unchanged, extracts the challenge
without interpretation, marks transactions as attempted before external
requests, enforces single-attempt semantics, handles timeouts, and requires
the final content state to be 'verified'.

Prohibited: regex interpretation of challenge language, operation-keyword
maps, character-collapse algorithms, automatic reposting, automatic
retries, multiple verification submissions.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Credentials — never persisted, never printed
# ---------------------------------------------------------------------------

MOLTBOOK_BASE = os.environ.get("MOLTBOOK_API_URL", "https://www.moltbook.com/api/v1")


def _get_token() -> str | None:
    return os.environ.get("MOLTBOOK_TOKEN")


# ---------------------------------------------------------------------------
# Moltbook API client (injectable for testing)
# ---------------------------------------------------------------------------

class MoltbookClient:
    """HTTP client for the Moltbook API.  The _api_call method is the
    single transport boundary; tests replace it via monkeypatch."""

    def __init__(self, base_url: str = MOLTBOOK_BASE, timeout: float = 30.0) -> None:
        self.base_url = base_url
        self.timeout = timeout

    # -- public API surface ------------------------------------------------

    def create_content(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_call("POST", "/content", payload)

    def verify_challenge(self, verification_code: str, answer: str) -> dict[str, Any]:
        return self._api_call("POST", "/verify", {
            "verification_code": verification_code,
            "answer": answer,
        })

    def fetch_content(self, content_id: str) -> dict[str, Any]:
        return self._api_call("GET", f"/content/{content_id}")

    # -- transport (overridden in tests) ----------------------------------

    def _api_call(self, method: str, path: str,
                  body: dict[str, Any] | None = None) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        url = f"{self.base_url}{path}"
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        token = _get_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Moltbook API {method} {path} returned {exc.code}: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Moltbook API unreachable: {exc}") from exc
        except TimeoutError:
            raise RuntimeError(f"Moltbook API {method} {path} timed out after {self.timeout}s")


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
    expires_at: float                        # Unix timestamp
    raw_create_response: dict[str, Any]
    state: str = "pending"                   # pending | attempted | verified | expired | indeterminate
    submitted_answer: str | None = None
    attempted_at: float | None = None        # when verification was first attempted
    verified_at: float | None = None

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def is_terminal(self) -> bool:
        return self.state in ("verified", "expired", "indeterminate")


# ---------------------------------------------------------------------------
# Transaction store
# ---------------------------------------------------------------------------

STORE_DIR_NAME = "data/moltbook"


class TransactionStore:
    """Persist transactions to a JSON file.

    The file is created with restrictive permissions (owner read/write only).
    API tokens and Authorization headers are never stored.
    """

    def __init__(self, repo_root: Path) -> None:
        self._dir = repo_root / STORE_DIR_NAME
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._path = self._dir / "transactions.json"

    # -- public API --------------------------------------------------------

    def save(self, txn: Transaction) -> None:
        data = self._load()
        data[txn.transaction_id] = _txn_to_dict(txn)
        self._write(data)

    def load(self, transaction_id: str) -> Transaction | None:
        data = self._load()
        raw = data.get(transaction_id)
        return _txn_from_dict(raw) if raw is not None else None

    def mark_attempted(self, transaction_id: str, answer: str) -> None:
        data = self._load()
        raw = data.get(transaction_id)
        if raw is None:
            return
        raw["state"] = "attempted"
        raw["submitted_answer"] = answer
        raw["attempted_at"] = time.time()
        data[transaction_id] = raw
        self._write(data)

    def mark_verified(self, transaction_id: str) -> None:
        data = self._load()
        raw = data.get(transaction_id)
        if raw is None:
            return
        raw["state"] = "verified"
        raw["verified_at"] = time.time()
        data[transaction_id] = raw
        self._write(data)

    def mark_indeterminate(self, transaction_id: str, reason: str) -> None:
        data = self._load()
        raw = data.get(transaction_id)
        if raw is None:
            return
        raw["state"] = "indeterminate"
        raw["indeterminate_reason"] = reason
        data[transaction_id] = raw
        self._write(data)

    def path(self) -> Path:
        return self._path

    # -- internal ----------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        # Restrictive permissions after write
        try:
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # best effort on platforms without chmod


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
        "attempted_at": txn.attempted_at,
        "verified_at": txn.verified_at,
    }


def _txn_from_dict(d: dict[str, Any]) -> Transaction | None:
    if not d:
        return None
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
        attempted_at=float(d["attempted_at"]) if d.get("attempted_at") else None,
        verified_at=float(d["verified_at"]) if d.get("verified_at") else None,
    )


# ---------------------------------------------------------------------------
# Challenge extraction (passthrough only)
# ---------------------------------------------------------------------------

def extract_challenge(create_response: dict[str, Any]) -> dict[str, Any]:
    """Return raw challenge fields unchanged.  No interpretation."""
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
    """Create content and persist the raw response.  Prints challenge unchanged."""
    try:
        body = json.loads(payload)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"Invalid JSON payload: {exc}"}))
        return 1

    # Content field required
    for field in ("content", "text", "body"):
        if field in body:
            break
    else:
        print(json.dumps({"error": "Payload must contain content, text, or body"}))
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
    """Submit verification answer.  Enforces single-attempt + final-state check."""
    txn = store.load(transaction_id)
    if txn is None:
        print(json.dumps({"error": f"No transaction: {transaction_id}"}))
        return 1

    if txn.is_expired:
        print(json.dumps({"error": "Transaction expired", "expires_at": txn.expires_at}))
        return 1

    if txn.is_terminal:
        print(json.dumps({
            "error": f"Transaction terminal ({txn.state}). No further attempts.",
        }))
        return 1

    # -- Mark attempted *before* external request (attempt semantics) ------
    store.mark_attempted(transaction_id, answer)

    # -- Submit answer ----------------------------------------------------
    try:
        client.verify_challenge(txn.verification_code, answer)
    except RuntimeError as exc:
        # Timeout or network error — refetch and check
        return _handle_verify_error(client, store, transaction_id, str(exc))

    # -- Refetch content --------------------------------------------------
    try:
        content = client.fetch_content(txn.content_id)
    except RuntimeError as exc:
        print(json.dumps({"error": f"Content refetch failed: {exc}"}))
        return 1

    verification_status = content.get("verification_status", "")

    if verification_status != "verified":
        store.mark_indeterminate(
            transaction_id,
            f"content verification_status is '{verification_status}', not 'verified'",
        )
        print(json.dumps({
            "error": "Verification incomplete",
            "verification_status": verification_status,
            "transaction_state": "indeterminate",
        }))
        return 1

    # -- Success ----------------------------------------------------------
    store.mark_verified(transaction_id)

    receipt = {
        "transaction_id": transaction_id,
        "content_id": txn.content_id,
        "content_type": txn.content_type,
        "url": txn.url,
        "verification_status": "verified",
        "status": "complete",
    }
    print(json.dumps(receipt, indent=2))
    return 0


def _handle_verify_error(client: MoltbookClient, store: TransactionStore,
                         transaction_id: str, error_msg: str) -> int:
    """Refetch content after a verification error (timeout/network).

    If the content is already verified, treat as success.
    Otherwise record indeterminate state.
    """
    txn = store.load(transaction_id)
    if txn is None:
        return 1

    try:
        content = client.fetch_content(txn.content_id)
        if content.get("verification_status") == "verified":
            store.mark_verified(transaction_id)
            receipt = {
                "transaction_id": transaction_id,
                "content_id": txn.content_id,
                "content_type": txn.content_type,
                "url": txn.url,
                "verification_status": "verified",
                "status": "complete",
                "note": f"Recovered after verify error: {error_msg}",
            }
            print(json.dumps(receipt, indent=2))
            return 0
    except RuntimeError:
        pass

    store.mark_indeterminate(transaction_id, error_msg)
    print(json.dumps({
        "error": "Verification failed and content not verified",
        "detail": error_msg,
        "transaction_state": "indeterminate",
    }))
    return 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_timestamp(raw: str) -> float:
    """Parse ISO-8601 → Unix float.  Returns 0 on failure (effectively expired)."""
    if not raw:
        return 0.0
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="moltbook_write",
        description="Minimal Moltbook verified-write bridge for Hermes",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create", help="Create a post or comment")
    create_parser.add_argument("payload", help="JSON content payload")

    verify_parser = sub.add_parser("verify", help="Submit a verification answer")
    verify_parser.add_argument("transaction_id")
    verify_parser.add_argument("answer")

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
