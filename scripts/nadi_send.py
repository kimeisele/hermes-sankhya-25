#!/usr/bin/env python3
"""Append a message to the Nadi outbox for federation relay pickup.

The outbox is a plain JSON array of DeliveryEnvelope objects.
agent-internet's relay pump checks out sibling repos periodically and
forwards any envelopes it finds to the target node's inbox.

Usage:
    python scripts/nadi_send.py --to agent-research --op inquiry --payload '{"question":"What is dark matter?"}'
    python scripts/nadi_send.py --to agent-city --op heartbeat
    python scripts/nadi_send.py --list          # show pending outbox messages
    python scripts/nadi_send.py --clear         # clear outbox after relay pickup
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTBOX_PATH = REPO_ROOT / "nadi_outbox.json"


def _repo_name() -> str:
    """Derive this node's ID from the repo directory name."""
    return REPO_ROOT.name


def _read_outbox() -> list:
    """Read the outbox array, creating it if missing."""
    if not OUTBOX_PATH.exists():
        OUTBOX_PATH.write_text("[]\n")
        return []
    text = OUTBOX_PATH.read_text().strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        print(f"warning: outbox is not an array, resetting", file=sys.stderr)
        return []
    return data


def _write_outbox(messages: list) -> None:
    """Write the outbox array."""
    OUTBOX_PATH.write_text(json.dumps(messages, indent=2, sort_keys=True) + "\n")


def build_envelope(
    target: str,
    operation: str,
    payload: dict | None = None,
    *,
    source: str | None = None,
    nadi_type: str = "filesystem",
    priority: int = 5,
    ttl_ms: int = 300_000,
) -> dict:
    """Build a DeliveryEnvelope matching the steward-federation protocol.

    Fields follow the FilesystemFederationTransport format used by
    agent-internet's relay pump.
    """
    source = source or _repo_name()
    return {
        "correlation_id": str(uuid.uuid4()),
        "envelope_id": str(uuid.uuid4()),
        "nadi_op": operation,
        "nadi_type": nadi_type,
        "operation": operation,
        "payload": payload or {},
        "priority": priority,
        "source_city_id": source,
        "target_city_id": target,
        "timestamp": time.time(),
        "ttl_ms": ttl_ms,
    }


def cmd_send(args: argparse.Namespace) -> int:
    """Append an envelope to the outbox."""
    payload: dict | None = None
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError:
            print(f"error: --payload must be valid JSON", file=sys.stderr)
            return 1

    envelope = build_envelope(
        target=args.to,
        operation=args.op,
        payload=payload,
        priority=args.priority,
        ttl_ms=args.ttl,
    )

    outbox = _read_outbox()
    outbox.append(envelope)
    _write_outbox(outbox)

    print(f"Queued envelope {envelope['envelope_id'][:8]}… → {args.to} ({args.op})")
    print(f"Outbox now has {len(outbox)} message(s). Relay will pick up on next cycle.")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    """List pending outbox messages."""
    outbox = _read_outbox()
    if not outbox:
        print("Outbox is empty.")
        return 0
    print(f"{len(outbox)} pending message(s):\n")
    for i, env in enumerate(outbox, 1):
        eid = env.get("envelope_id", "?")[:8]
        target = env.get("target_city_id", "?")
        op = env.get("operation", "?")
        print(f"  {i}. [{eid}…] → {target} ({op})")
    return 0


def cmd_clear(_args: argparse.Namespace) -> int:
    """Clear the outbox."""
    _write_outbox([])
    print("Outbox cleared.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Nadi outbox message tool")
    sub = parser.add_subparsers(dest="command")

    send = sub.add_parser("send", help="Queue a message for relay")
    send.add_argument("--to", required=True, help="Target node ID (e.g. agent-research)")
    send.add_argument("--op", required=True, help="Operation name (e.g. inquiry, heartbeat)")
    send.add_argument("--payload", default=None, help="JSON payload string")
    send.add_argument("--priority", type=int, default=5, help="Priority 1-10 (default 5)")
    send.add_argument("--ttl", type=int, default=300_000, help="TTL in ms (default 300000)")

    sub.add_parser("list", help="List pending outbox messages")
    sub.add_parser("clear", help="Clear the outbox")

    # Support flat --list / --clear flags for convenience
    parser.add_argument("--list", action="store_true", help="List pending messages")
    parser.add_argument("--clear", action="store_true", help="Clear the outbox")
    # Flat send flags
    parser.add_argument("--to", default=None, help="Target node ID")
    parser.add_argument("--op", default=None, help="Operation name")
    parser.add_argument("--payload", default=None, help="JSON payload")
    parser.add_argument("--priority", type=int, default=5)
    parser.add_argument("--ttl", type=int, default=300_000)

    args = parser.parse_args()

    if args.list or args.command == "list":
        return cmd_list(args)
    if args.clear or args.command == "clear":
        return cmd_clear(args)
    if args.command == "send" or (args.to and args.op):
        return cmd_send(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
