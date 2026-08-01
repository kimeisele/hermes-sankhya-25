#!/usr/bin/env python3
"""Minimal Moltbook verified-write bridge for Hermes.

Commands: create / verify.

API contract (observed, B001):
  POST /posts                              — create post
  POST /posts/{parent_id}/comments         — create comment
  POST /verify                             — submit challenge answer
  GET  /posts/{post_id}                    — fetch post
  GET  /posts/{parent_id}                  — fetch parent post + comments

Credentials (never persisted, never printed):
  1. MOLTBOOK_TOKEN  env var
  2. api_key  field in  ~/.config/moltbook/credentials.json

Prohibited: regex challenge interpretation, keyword maps, character collapse,
auto-repost, auto-retry, multiple submissions.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import stat
import time
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Credentials — read, never persist, never print
# ---------------------------------------------------------------------------

MOLTBOOK_BASE = os.environ.get("MOLTBOOK_API_URL", "https://www.moltbook.com/api/v1")


def _get_token() -> str | None:
    """MOLTBOOK_TOKEN  →  ~/.config/moltbook/credentials.json api_key."""
    token = os.environ.get("MOLTBOOK_TOKEN")
    if token:
        return token
    creds_path = Path.home() / ".config" / "moltbook" / "credentials.json"
    if creds_path.exists():
        try:
            data = json.loads(creds_path.read_text())
            return data.get("api_key") or data.get("token") or data.get("access_token")
        except (json.JSONDecodeError, OSError):
            return None
    return None


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class MoltbookClient:
    """HTTP client for the real Moltbook API."""

    def __init__(self, base_url: str = MOLTBOOK_BASE, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def create_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_call("POST", "/posts", payload)

    def create_comment(self, parent_post_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_call("POST", f"/posts/{parent_post_id}/comments", payload)

    def verify_challenge(self, verification_code: str, answer: str) -> dict[str, Any]:
        return self._api_call("POST", "/verify", {
            "verification_code": verification_code, "answer": answer,
        })

    def fetch_post(self, post_id: str) -> dict[str, Any]:
        return self._api_call("GET", f"/posts/{post_id}")

    def fetch_comments(self, parent_post_id: str) -> dict[str, Any]:
        """Fallback: official comment-list endpoint."""
        return self._api_call("GET", f"/posts/{parent_post_id}/comments")

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
    content_type: str                     # "post" | "comment"
    parent_post_id: str                   # "" for posts
    url: str
    raw_challenge_text: str               # "" when challenge_unavailable
    verification_code: str                # "" when challenge_unavailable
    challenge_instructions: str
    expires_at: float                     # 0 when unavailable
    raw_create_response: dict[str, Any]
    # challenge_unavailable details (parsing failure info)
    parse_failure: str = ""
    state: str = "pending"                # pending|attempted|verified|indeterminate|challenge_unavailable
    submitted_answer: str | None = None
    attempted_at: float | None = None
    verified_at: float | None = None

    @property
    def is_expired(self) -> bool:
        return self.expires_at > 0 and time.time() > self.expires_at

    @property
    def is_terminal(self) -> bool:
        return self.state in ("verified", "indeterminate", "challenge_unavailable")


# ---------------------------------------------------------------------------
# Transaction store
# ---------------------------------------------------------------------------

STORE_DIR = "data/moltbook"


class TransactionStore:
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
        "parse_failure": txn.parse_failure,
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
        parse_failure=d.get("parse_failure", ""),
        state=d.get("state", "pending"),
        submitted_answer=d.get("submitted_answer"),
        attempted_at=float(d["attempted_at"]) if d.get("attempted_at") else None,
        verified_at=float(d["verified_at"]) if d.get("verified_at") else None,
    )


# ---------------------------------------------------------------------------
# Create-response parsing — split identity / verification
# ---------------------------------------------------------------------------

def _extract_content_identity(raw: dict[str, Any], content_type: str) -> dict[str, Any]:
    """Extract content object identity fields.  Raises on failure."""
    if raw.get("success") is not True:
        raise RuntimeError(f"API create returned success != true: {json.dumps(raw)}")

    obj = raw.get(content_type)
    if not isinstance(obj, dict):
        raise RuntimeError(f"Expected '{content_type}' object, got {type(obj).__name__}")

    content_id = obj.get("id", "")
    if not content_id:
        raise RuntimeError(f"Missing content id in {content_type} response")

    parent_post_id_raw = obj.get("parent_post_id", "")
    post_id_raw = obj.get("post_id", "")

    if content_type == "comment":
        # Comments: accept parent_post_id OR post_id, but fail on ambiguity
        has_ppid = bool(parent_post_id_raw)
        has_pid = bool(post_id_raw)
        if has_ppid and has_pid and parent_post_id_raw != post_id_raw:
            raise RuntimeError(
                f"Ambiguous parent identifier: parent_post_id="
                f"'{parent_post_id_raw}' != post_id='{post_id_raw}'")
        parent = parent_post_id_raw or post_id_raw
        if not parent:
            raise RuntimeError(
                "Missing parent identifier in comment response "
                "(neither parent_post_id nor post_id present)")
    else:
        # Posts: post_id is the content's own ID, not the parent
        parent = parent_post_id_raw

    return {
        "content_id": content_id,
        "content_type": content_type,
        "url": obj.get("url", ""),
        "parent_post_id": parent,
    }


def _extract_verification(raw: dict[str, Any], content_type: str) -> dict[str, Any]:
    """Extract verification fields.  Raises if missing/invalid."""
    obj = raw.get(content_type, {})
    ver = obj.get("verification")
    if not isinstance(ver, dict):
        raise RuntimeError("Missing or invalid verification object")

    vcode = ver.get("verification_code", "")
    if not vcode:
        raise RuntimeError("Missing verification_code")

    chall = ver.get("challenge_text", "")
    if not chall:
        raise RuntimeError("Missing challenge_text")

    expires_raw = ver.get("expires_at", "")
    ts = _parse_timestamp(expires_raw)
    if ts <= 0:
        raise RuntimeError(f"Unparseable or missing expires_at: {expires_raw}")

    return {
        "verification_code": vcode,
        "challenge_text": chall,
        "expires_at": ts,
        "instructions": ver.get("instructions", ""),
    }


def _parse_timestamp(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Fetch-response parsing
# ---------------------------------------------------------------------------

def _parse_fetch_response(raw: dict[str, Any], content_type: str,
                          content_id: str) -> dict[str, Any]:
    """Extract the exact content object by type and id."""
    if content_type == "post":
        post = raw.get("post")
        if not isinstance(post, dict):
            raise RuntimeError("Fetch response missing 'post' object")
        if post.get("id") != content_id:
            raise RuntimeError(
                f"Fetched post id '{post.get('id')}' != expected '{content_id}'")
        return post

    # comment — try post.comments[] first, then top-level comments array
    for key in ("post", "comments"):
        wrapper = raw.get(key)
        if isinstance(wrapper, dict):
            comments = wrapper.get("comments") if key == "post" else wrapper
            if isinstance(comments, list):
                for c in comments:
                    if isinstance(c, dict) and c.get("id") == content_id:
                        return c
            continue
        if isinstance(wrapper, list):
            for c in wrapper:
                if isinstance(c, dict) and c.get("id") == content_id:
                    return c

    raise RuntimeError(f"Comment '{content_id}' not found in fetch response")


# ---------------------------------------------------------------------------
# Build API payload (strips local routing fields)
# ---------------------------------------------------------------------------

def _build_api_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Return the outbound API body.  Strips type, parent_post_id, reply_to_comment_id.

    Maps local reply_to_comment_id → Moltbook parent_id for threaded replies.
    """
    payload = {k: v for k, v in body.items()
               if k not in ("type", "parent_post_id", "reply_to_comment_id")}
    if "reply_to_comment_id" in body:
        payload["parent_id"] = body["reply_to_comment_id"]
    return payload


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _uuid_short() -> str:
    return _uuid.uuid4().hex[:12]


def cmd_create(client: MoltbookClient, store: TransactionStore, payload: str) -> int:
    """Create post or comment.  Persists content identity even when verification
    is malformed (challenge_unavailable state)."""
    # --- parse payload ---
    try:
        body = json.loads(payload)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"Invalid JSON: {exc}"}))
        return 1

    content_type = body.get("type", "")
    if content_type not in ("post", "comment"):
        print(json.dumps({"error": "Payload must specify type: post or comment"}))
        return 1

    if content_type == "post":
        if not isinstance(body.get("title"), str) or not body["title"].strip():
            print(json.dumps({"error": "Post payload must include non-empty title"}))
            return 1
        submolt_fields = ("submolt", "submolt_name", "submolt_id")
        has_submolt = any(
            isinstance(body.get(field), str) and body[field].strip()
            for field in submolt_fields
        )
        if not has_submolt:
            print(json.dumps({"error": "Post payload must include non-empty submolt, submolt_name, or submolt_id"}))
            return 1
    else:  # comment
        if not isinstance(body.get("content"), str) or not body["content"].strip():
            print(json.dumps({"error": "Comment payload must include non-empty content"}))
            return 1
        parent = body.get("parent_post_id", "")
        if not isinstance(parent, str) or not parent.strip():
            print(json.dumps({"error": "Comment payload must include non-empty parent_post_id"}))
            return 1

    # --- credential guard ---
    if _get_token() is None:
        print(json.dumps({"error": "No Moltbook credential. Set MOLTBOOK_TOKEN or configure ~/.config/moltbook/credentials.json"}))
        return 1

    # --- issue create ---
    api_payload = _build_api_payload(body)
    try:
        if content_type == "post":
            raw = client.create_post(api_payload)
        else:
            raw = client.create_comment(body["parent_post_id"], api_payload)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    # --- extract content identity ---
    try:
        identity = _extract_content_identity(raw, content_type)
    except RuntimeError as exc:
        print(json.dumps({"error": f"Create response missing content identity: {exc}"}))
        return 1

    transaction_id = _uuid_short()

    # --- extract verification ---
    try:
        ver = _extract_verification(raw, content_type)
    except RuntimeError as exc:
        # Content exists but verification is malformed — persist anyway
        txn = Transaction(
            transaction_id=transaction_id,
            content_id=identity["content_id"],
            content_type=identity["content_type"],
            parent_post_id=identity["parent_post_id"],
            url=identity["url"],
            raw_challenge_text="",
            verification_code="",
            challenge_instructions="",
            expires_at=0.0,
            raw_create_response=raw,
            parse_failure=str(exc),
            state="challenge_unavailable",
        )
        store.save(txn)

        output = {
            "transaction_id": transaction_id,
            "content_id": identity["content_id"],
            "content_type": identity["content_type"],
            "url": identity["url"],
            "state": "challenge_unavailable",
            "parse_failure": str(exc),
            "warning": "Content may exist on Moltbook but is not verified. Inspect manually.",
        }
        print(json.dumps(output, indent=2))
        return 1

    # --- valid challenge — persist pending transaction ---
    txn = Transaction(
        transaction_id=transaction_id,
        content_id=identity["content_id"],
        content_type=identity["content_type"],
        parent_post_id=identity["parent_post_id"],
        url=identity["url"],
        raw_challenge_text=ver["challenge_text"],
        verification_code=ver["verification_code"],
        challenge_instructions=ver["instructions"],
        expires_at=ver["expires_at"],
        raw_create_response=raw,
    )
    store.save(txn)

    output = {
        "transaction_id": transaction_id,
        "content_id": identity["content_id"],
        "content_type": identity["content_type"],
        "url": identity["url"],
        "challenge": {
            "challenge_text": ver["challenge_text"],
            "verification_code": ver["verification_code"],
            "instructions": ver["instructions"],
        },
    }
    print(json.dumps(output, indent=2))
    return 0


def cmd_verify(client: MoltbookClient, store: TransactionStore,
               transaction_id: str, answer: str) -> int:
    """Submit verification or reconcile.

    Order:
      terminal state → reject
      attempted      → read-only reconciliation (regardless of expiry)
      pending        → check expiry, submit once
    """
    txn = store.load(transaction_id)
    if txn is None:
        print(json.dumps({"error": f"No transaction: {transaction_id}"}))
        return 1

    if txn.is_terminal:
        # indeterminate comment transactions may be recovered read-only:
        # the content may be verified on Moltbook even though the local
        # bridge could not confirm it.  Never resubmits POST /verify.
        if txn.state == "indeterminate" and txn.content_type == "comment":
            return _reconcile_attempted(client, store, txn)
        print(json.dumps({
            "error": f"Transaction terminal ({txn.state}). No further action.",
        }))
        return 1

    # --- credential guard — before any state change or network call ---
    if _get_token() is None:
        print(json.dumps({
            "error": "No Moltbook credential. Set MOLTBOOK_TOKEN or configure ~/.config/moltbook/credentials.json",
            "transaction_state": txn.state,
        }))
        return 1

    if txn.state == "attempted":
        return _reconcile_attempted(client, store, txn)

    # state == pending
    if txn.is_expired:
        print(json.dumps({"error": "Transaction expired", "expires_at": txn.expires_at}))
        return 1

    # mark attempted *before* external request
    now = time.time()
    store.update_state(transaction_id, state="attempted",
                       submitted_answer=answer, attempted_at=now)

    try:
        client.verify_challenge(txn.verification_code, answer)
    except RuntimeError as exc:
        return _handle_verify_error(client, store, txn, str(exc))

    return _finalize_verification(client, store, txn)


def _reconcile_attempted(client: MoltbookClient, store: TransactionStore,
                         txn: Transaction) -> int:
    """Read-only reconciliation for attempted transactions.

    Never resubmits.  Expired challenges are irrelevant — the answer was
    already submitted.  Only checks final content state.
    """
    try:
        content = _fetch_content_object(client, txn)
    except RuntimeError as exc:
        print(json.dumps({
            "error": f"Reconciliation fetch failed: {exc}",
            "transaction_state": "attempted",
        }))
        return 1

    vs = content.get("verification_status", "")
    if vs == "verified":
        store.update_state(txn.transaction_id, state="verified",
                           verified_at=time.time())
        print(json.dumps({
            "transaction_id": txn.transaction_id,
            "content_id": txn.content_id,
            "content_type": txn.content_type,
            "url": txn.url,
            "verification_status": "verified",
            "status": "complete",
            "note": "Recovered via reconciliation",
        }, indent=2))
        return 0

    print(json.dumps({
        "error": "Content not yet verified. Requires explicit inspection.",
        "verification_status": vs,
        "transaction_state": "attempted",
    }))
    return 1


def _handle_verify_error(client: MoltbookClient, store: TransactionStore,
                         txn: Transaction, error_msg: str) -> int:
    try:
        content = _fetch_content_object(client, txn)
        if content.get("verification_status") == "verified":
            store.update_state(txn.transaction_id, state="verified",
                               verified_at=time.time())
            print(json.dumps({
                "transaction_id": txn.transaction_id,
                "content_id": txn.content_id,
                "content_type": txn.content_type,
                "url": txn.url,
                "verification_status": "verified",
                "status": "complete",
                "note": f"Recovered after verify error: {error_msg}",
            }, indent=2))
            return 0
    except RuntimeError:
        pass

    store.update_state(txn.transaction_id, state="indeterminate")
    print(json.dumps({
        "error": "Verification failed and content not verified",
        "detail": error_msg,
        "transaction_state": "indeterminate",
    }))
    return 1


def _finalize_verification(client: MoltbookClient, store: TransactionStore,
                           txn: Transaction) -> int:
    try:
        content = _fetch_content_object(client, txn)
    except RuntimeError as exc:
        store.update_state(txn.transaction_id, state="indeterminate")
        print(json.dumps({
            "error": f"Content refetch failed: {exc}",
            "transaction_state": "indeterminate",
        }))
        return 1

    vs = content.get("verification_status", "")
    if vs != "verified":
        store.update_state(txn.transaction_id, state="indeterminate")
        print(json.dumps({
            "error": "Verification incomplete",
            "verification_status": vs,
            "transaction_state": "indeterminate",
        }))
        return 1

    store.update_state(txn.transaction_id, state="verified", verified_at=time.time())
    print(json.dumps({
        "transaction_id": txn.transaction_id,
        "content_id": txn.content_id,
        "content_type": txn.content_type,
        "url": txn.url,
        "verification_status": "verified",
        "status": "complete",
    }, indent=2))
    return 0


def _fetch_content_object(client: MoltbookClient, txn: Transaction) -> dict[str, Any]:
    if txn.content_type == "post":
        raw = client.fetch_post(txn.content_id)
        return _parse_fetch_response(raw, txn.content_type, txn.content_id)

    # comment — try parent post, fall back to comment list
    parent_id = txn.parent_post_id
    if not parent_id:
        # Recovery: the real comment create response carries post_id on the
        # comment object (parent_post_id is absent).  Derive it from the
        # stored create response so historical transactions can reconcile.
        parent_id = _derive_parent_from_create_response(txn)
    if not parent_id:
        raise RuntimeError("Comment transaction missing parent_post_id")

    # First: fetch parent post (includes comments[])
    try:
        raw = client.fetch_post(parent_id)
        return _parse_fetch_response(raw, txn.content_type, txn.content_id)
    except RuntimeError:
        pass  # fetch or extraction failed — fall back

    # Second: fetch official comment list
    raw = client.fetch_comments(parent_id)
    return _parse_fetch_response(raw, txn.content_type, txn.content_id)


def _derive_parent_from_create_response(txn: Transaction) -> str:
    """Recover the parent post id from the stored create response.

    Real Moltbook comment create responses contain ``post_id`` on the
    comment object.  Returns ``""`` when not derivable or when the
    stored identifiers are ambiguous — the caller fails closed.
    """
    raw = txn.raw_create_response
    if not isinstance(raw, dict):
        return ""
    obj = raw.get(txn.content_type)
    if not isinstance(obj, dict):
        return ""
    ppid = obj.get("parent_post_id", "")
    pid = obj.get("post_id", "")
    if ppid and pid and ppid != pid:
        return ""  # ambiguous — fail closed
    return ppid or pid


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="moltbook_write", description="Minimal Moltbook verified-write bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create", help="Create a post or comment")
    c.add_argument("payload", help="JSON payload with type, content")
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
