#!/usr/bin/env python3
"""Minimal Moltbook verified-write bridge for Hermes.

Two commands:

    moltbook_write create <payload>
    moltbook_write verify <transaction-id> <answer>

Real API contract:
  POST /posts                              — create post
  POST /posts/{parent_id}/comments         — create comment
  POST /verify                             — submit challenge answer
  GET  /posts/{post_id}                    — fetch post
  GET  /posts/{parent_id}                  — fetch parent post (for comments)

The bridge persists the raw API response, extracts the nested challenge
without interpretation, enforces crash-safe single-attempt semantics,
checks expiry, and requires final content state 'verified'.

Prohibited: regex interpretation, keyword maps, character collapse,
auto-repost, auto-retry, multiple submissions.
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
# Credentials
# ---------------------------------------------------------------------------

MOLTBOOK_BASE = os.environ.get("MOLTBOOK_API_URL", "https://www.moltbook.com/api/v1")


def _get_token() -> str | None:
    """Read MOLTBOOK_TOKEN env var, falling back to credentials file."""
    token = os.environ.get("MOLTBOOK_TOKEN")
    if token:
        return token
    creds_path = Path.home() / ".config" / "moltbook" / "credentials.json"
    if creds_path.exists():
        try:
            data = json.loads(creds_path.read_text())
            token = data.get("token") or data.get("api_token") or data.get("access_token")
            if token:
                return token
        except (json.JSONDecodeError, OSError):
            pass
    return None


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class MoltbookClient:
    """HTTP client for the real Moltbook API.

    Transport method _api_call is the single boundary for testing.
    """

    def __init__(self, base_url: str = MOLTBOOK_BASE, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- public API surface ------------------------------------------------

    def create_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_call("POST", "/posts", payload)

    def create_comment(self, parent_post_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_call("POST", f"/posts/{parent_post_id}/comments", payload)

    def verify_challenge(self, verification_code: str, answer: str) -> dict[str, Any]:
        return self._api_call("POST", "/verify", {
            "verification_code": verification_code,
            "answer": answer,
        })

    def fetch_post(self, post_id: str) -> dict[str, Any]:
        return self._api_call("GET", f"/posts/{post_id}")

    # -- transport --------------------------------------------------------

    def _api_call(self, method: str, path: str,
                  body: dict[str, Any] | None = None) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        url = f"{self.base_url}{path}"
        data: bytes | None = None
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
            err = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"API {method} {path} → {exc.code}: {err}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"API unreachable {method} {path}: {exc}") from exc
        except TimeoutError:
            raise RuntimeError(f"API timeout {method} {path} after {self.timeout}s")


# ---------------------------------------------------------------------------
# Transaction model
# ---------------------------------------------------------------------------

@dataclass
class Transaction:
    transaction_id: str
    content_id: str
    content_type: str              # "post" | "comment"
    parent_post_id: str            # for comments
    url: str
    raw_challenge_text: str
    verification_code: str
    challenge_instructions: str
    expires_at: float
    raw_create_response: dict[str, Any]
    state: str = "pending"         # pending | attempted | verified | indeterminate
    submitted_answer: str | None = None
    attempted_at: float | None = None
    verified_at: float | None = None

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def is_terminal(self) -> bool:
        return self.state in ("verified", "indeterminate")


# ---------------------------------------------------------------------------
# Transaction store
# ---------------------------------------------------------------------------

STORE_DIR = "data/moltbook"


class TransactionStore:
    """JSON persistence with restrictive permissions."""

    def __init__(self, repo_root: Path) -> None:
        self._dir = repo_root / STORE_DIR
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._path = self._dir / "transactions.json"

    def save(self, txn: Transaction) -> None:
        data = self._load()
        data[txn.transaction_id] = _txn_to_dict(txn)
        self._write(data)

    def load(self, transaction_id: str) -> Transaction | None:
        data = self._load()
        raw = data.get(transaction_id)
        return _txn_from_dict(raw) if raw is not None else None

    def update_state(self, transaction_id: str, **kwargs: Any) -> None:
        data = self._load()
        raw = data.get(transaction_id)
        if raw is None:
            return
        raw.update(kwargs)
        data[transaction_id] = raw
        self._write(data)

    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, dict[str, Any]]:
        return json.loads(self._path.read_text()) if self._path.exists() else {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        try:
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


def _txn_to_dict(txn: Transaction) -> dict[str, Any]:
    return {
        "transaction_id": txn.transaction_id,
        "content_id": txn.content_id,
        "content_type": txn.content_type,
        "parent_post_id": txn.parent_post_id,
        "url": txn.url,
        "raw_challenge_text": txn.raw_challenge_text,
        "verification_code": txn.verification_code,
        "challenge_instructions": txn.challenge_instructions,
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
        parent_post_id=d.get("parent_post_id", ""),
        url=d["url"],
        raw_challenge_text=d["raw_challenge_text"],
        verification_code=d["verification_code"],
        challenge_instructions=d.get("challenge_instructions", ""),
        expires_at=float(d["expires_at"]),
        raw_create_response=d["raw_create_response"],
        state=d.get("state", "pending"),
        submitted_answer=d.get("submitted_answer"),
        attempted_at=float(d["attempted_at"]) if d.get("attempted_at") else None,
        verified_at=float(d["verified_at"]) if d.get("verified_at") else None,
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_create_response(raw: dict[str, Any], content_type: str) -> dict[str, Any]:
    """Extract nested content object, validate, return flat result."""
    if raw.get("success") is not True:
        raise RuntimeError(f"Create returned success != true: {raw}")

    obj = raw.get(content_type)
    if not isinstance(obj, dict):
        raise RuntimeError(f"Expected '{content_type}' object, got: {type(obj).__name__}")

    content_id = obj.get("id", "")
    if not content_id:
        raise RuntimeError(f"Missing content id in response: {obj}")

    ver = obj.get("verification")
    if not isinstance(ver, dict):
        raise RuntimeError(f"Missing verification object in {content_type} response")

    vcode = ver.get("verification_code", "")
    chall = ver.get("challenge_text", "")
    expires = ver.get("expires_at", "")
    instr = ver.get("instructions", "")

    if not vcode:
        raise RuntimeError("Missing verification_code in verification object")

    url = obj.get("url", "")

    return {
        "content_id": content_id,
        "url": url,
        "verification_code": vcode,
        "challenge_text": chall,
        "expires_at": expires,
        "instructions": instr,
        "parent_post_id": obj.get("parent_post_id", ""),
    }


def _parse_fetch_response(raw: dict[str, Any], content_type: str,
                          content_id: str) -> dict[str, Any]:
    """Extract the content object from a fetch response.

    For posts: raw["post"]
    For comments: raw["post"]["comments"][exact match by id]
    """
    if content_type == "post":
        post = raw.get("post")
        if not isinstance(post, dict):
            raise RuntimeError("Fetch response missing 'post' object")
        if post.get("id") != content_id:
            raise RuntimeError(
                f"Fetched post id '{post.get('id')}' != expected '{content_id}'")
        return post

    # comment
    post_obj = raw.get("post")
    if not isinstance(post_obj, dict):
        raise RuntimeError("Comment fetch response missing 'post' wrapper")
    comments = post_obj.get("comments")
    if not isinstance(comments, list):
        raise RuntimeError("Comment fetch response missing 'comments' list")
    for c in comments:
        if isinstance(c, dict) and c.get("id") == content_id:
            return c
    raise RuntimeError(f"Comment '{content_id}' not found in fetch response")


def extract_challenge(create_response: dict[str, Any],
                      content_type: str) -> dict[str, Any]:
    """Passthrough: return nested challenge fields unchanged.  No interpretation."""
    return _parse_create_response(create_response, content_type)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _uuid_short() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def cmd_create(client: MoltbookClient, store: TransactionStore, payload: str) -> int:
    """Create post or comment.  Prints challenge unchanged.  Stops."""
    try:
        body = json.loads(payload)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"Invalid JSON: {exc}"}))
        return 1

    content_type = body.get("type", "")
    if content_type not in ("post", "comment"):
        print(json.dumps({"error": "Payload must specify type: post or comment"}))
        return 1

    # Check content field
    for fld in ("content", "text", "body"):
        if fld in body:
            break
    else:
        print(json.dumps({"error": "Payload must contain content, text, or body"}))
        return 1

    if content_type == "comment":
        parent = body.get("parent_post_id", "")
        if not parent:
            print(json.dumps({"error": "Comment payload must include parent_post_id"}))
            return 1

    try:
        if content_type == "post":
            raw = client.create_post(body)
        else:
            raw = client.create_comment(body["parent_post_id"], body)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    try:
        parsed = _parse_create_response(raw, content_type)
    except RuntimeError as exc:
        print(json.dumps({"error": f"Invalid create response: {exc}"}))
        return 1

    transaction_id = _uuid_short()

    txn = Transaction(
        transaction_id=transaction_id,
        content_id=parsed["content_id"],
        content_type=content_type,
        parent_post_id=parsed["parent_post_id"],
        url=parsed["url"],
        raw_challenge_text=parsed["challenge_text"],
        verification_code=parsed["verification_code"],
        challenge_instructions=parsed["instructions"],
        expires_at=_parse_timestamp(parsed["expires_at"]),
        raw_create_response=raw,
    )
    store.save(txn)

    output = {
        "transaction_id": transaction_id,
        "content_id": parsed["content_id"],
        "content_type": content_type,
        "url": parsed["url"],
        "challenge": {
            "challenge_text": parsed["challenge_text"],
            "verification_code": parsed["verification_code"],
            "instructions": parsed["instructions"],
        },
    }
    print(json.dumps(output, indent=2))
    return 0


def cmd_verify(client: MoltbookClient, store: TransactionStore,
               transaction_id: str, answer: str) -> int:
    """Submit verification (pending) or reconcile (attempted)."""
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

    if txn.state == "attempted":
        # Crash-safe: only reconcile, never resubmit
        return _reconcile_attempted(client, store, txn)

    # state == pending — mark attempted before any external request
    now = time.time()
    store.update_state(transaction_id, state="attempted",
                       submitted_answer=answer, attempted_at=now)

    # Submit answer
    try:
        client.verify_challenge(txn.verification_code, answer)
    except RuntimeError as exc:
        return _handle_verify_error(client, store, txn, str(exc))

    return _finalize_verification(client, store, txn)


def _reconcile_attempted(client: MoltbookClient, store: TransactionStore,
                         txn: Transaction) -> int:
    """Read-only reconciliation for an attempted transaction.

    Fetches content, checks status.  Success if verified.
    Otherwise stays attempted (not indeterminate).
    """
    try:
        content = _fetch_content_object(client, txn)
    except RuntimeError as exc:
        print(json.dumps({
            "error": f"Reconciliation fetch failed: {exc}",
            "transaction_state": "attempted",
        }))
        return 1

    if content.get("verification_status") == "verified":
        now = time.time()
        store.update_state(txn.transaction_id, state="verified", verified_at=now)
        receipt = {
            "transaction_id": txn.transaction_id,
            "content_id": txn.content_id,
            "content_type": txn.content_type,
            "url": txn.url,
            "verification_status": "verified",
            "status": "complete",
            "note": "Recovered via reconciliation",
        }
        print(json.dumps(receipt, indent=2))
        return 0

    print(json.dumps({
        "error": "Content not yet verified. Requires explicit inspection.",
        "verification_status": content.get("verification_status", "unknown"),
        "transaction_state": "attempted",
    }))
    return 1


def _handle_verify_error(client: MoltbookClient, store: TransactionStore,
                         txn: Transaction, error_msg: str) -> int:
    """After verify error (timeout/network), refetch and check."""
    try:
        content = _fetch_content_object(client, txn)
        if content.get("verification_status") == "verified":
            now = time.time()
            store.update_state(txn.transaction_id, state="verified", verified_at=now)
            receipt = {
                "transaction_id": txn.transaction_id,
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

    store.update_state(txn.transaction_id, state="indeterminate",
                       indeterminate_reason=error_msg)
    print(json.dumps({
        "error": "Verification failed and content not verified",
        "detail": error_msg,
        "transaction_state": "indeterminate",
    }))
    return 1


def _finalize_verification(client: MoltbookClient, store: TransactionStore,
                           txn: Transaction) -> int:
    """Refetch content and enforce verified state."""
    try:
        content = _fetch_content_object(client, txn)
    except RuntimeError as exc:
        store.update_state(txn.transaction_id, state="indeterminate",
                           indeterminate_reason=str(exc))
        print(json.dumps({
            "error": f"Content refetch failed: {exc}",
            "transaction_state": "indeterminate",
        }))
        return 1

    vs = content.get("verification_status", "")

    if vs != "verified":
        store.update_state(txn.transaction_id, state="indeterminate",
                           indeterminate_reason=f"status is '{vs}', not 'verified'")
        print(json.dumps({
            "error": "Verification incomplete",
            "verification_status": vs,
            "transaction_state": "indeterminate",
        }))
        return 1

    now = time.time()
    store.update_state(txn.transaction_id, state="verified", verified_at=now)

    receipt = {
        "transaction_id": txn.transaction_id,
        "content_id": txn.content_id,
        "content_type": txn.content_type,
        "url": txn.url,
        "verification_status": "verified",
        "status": "complete",
    }
    print(json.dumps(receipt, indent=2))
    return 0


def _fetch_content_object(client: MoltbookClient, txn: Transaction) -> dict[str, Any]:
    """Fetch and extract the exact content object (post or comment)."""
    if txn.content_type == "post":
        raw = client.fetch_post(txn.content_id)
    else:
        # Fetch parent post, then select exact comment by ID
        parent_id = txn.parent_post_id
        if not parent_id:
            raise RuntimeError("Comment transaction missing parent_post_id")
        raw = client.fetch_post(parent_id)

    return _parse_fetch_response(raw, txn.content_type, txn.content_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_timestamp(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        import datetime
        return datetime.datetime.fromisoformat(
            raw.replace("Z", "+00:00")).timestamp()
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

    c = sub.add_parser("create", help="Create a post or comment")
    c.add_argument("payload", help="JSON payload with type, content, and optional parent_post_id")

    v = sub.add_parser("verify", help="Submit verification or reconcile")
    v.add_argument("transaction_id")
    v.add_argument("answer")

    args = parser.parse_args()
    root = _repo_root()
    client = MoltbookClient()
    store = TransactionStore(root)

    if args.command == "create":
        return cmd_create(client, store, args.payload)
    elif args.command == "verify":
        return cmd_verify(client, store, args.transaction_id, args.answer)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
