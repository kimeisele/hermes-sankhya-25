"""NADI: schreibt signierte Heartbeats in unsere eigene outbox."""
import hashlib
import json
import os
import time
from pathlib import Path

OUTBOX = Path("data/federation/nadi_outbox.json")

def get_key() -> str:
    return os.environ.get("NODE_PRIVATE_KEY", "").strip()

def nadi_heartbeat(village_id: str) -> int:
    key = get_key()
    if not key:
        return 0
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        pk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key))
        msg = {"source": village_id, "target": "steward-federation", "operation": "heartbeat",
               "payload": {"health": 1.0}, "timestamp": time.time(), "ttl": 900,
               "message_id": hashlib.sha256(f"{village_id}:{time.time()}".encode()).hexdigest()[:16]}
        msg["signature"] = pk.sign(json.dumps(msg, sort_keys=True).encode()).hex()
        outbox = json.loads(OUTBOX.read_text()) if OUTBOX.exists() else []
        outbox.append(msg)
        outbox = outbox[-100:]
        OUTBOX.parent.mkdir(parents=True, exist_ok=True)
        OUTBOX.write_text(json.dumps(outbox, indent=2))
        return 1
    except ImportError:
        return 0
