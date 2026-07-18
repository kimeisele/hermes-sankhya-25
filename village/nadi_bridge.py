"""
Agent Village — NADI Bridge
===========================
Minimal NADI federation transport. No dependencies beyond stdlib + cryptography.
Integrates with steward-federation hub relay.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

# ── NADI Message ────────────────────────────────────────

class NadiMessage:
    """A signed federation message. Compatible with nadi_kit format."""
    def __init__(self, source: str, target: str, operation: str, payload: dict,
                 private_key_hex: str = "", ttl: float = 7200):
        self.source = source
        self.target = target
        self.operation = operation
        self.payload = payload
        self.timestamp = time.time()
        self.ttl = ttl
        self.message_id = hashlib.sha256(
            f"{source}:{target}:{operation}:{self.timestamp}".encode()
        ).hexdigest()[:16]
        self.signature = self._sign(private_key_hex) if private_key_hex else ""

    def _sign(self, key_hex: str) -> str:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key_hex))
            payload = json.dumps(self.to_dict(signed=False), sort_keys=True).encode()
            return key.sign(payload).hex()
        except ImportError:
            return ""

    def to_dict(self, signed: bool = True) -> dict:
        d = {
            "source": self.source, "target": self.target,
            "operation": self.operation, "payload": self.payload,
            "timestamp": self.timestamp, "ttl": self.ttl,
            "message_id": self.message_id,
        }
        if signed and self.signature:
            d["signature"] = self.signature
        return d

    @property
    def is_expired(self) -> bool:
        return time.time() - self.timestamp > self.ttl


# ── Key Management ──────────────────────────────────────

def generate_keypair() -> tuple[str, str]:
    """Generate Ed25519 keypair. Returns (private_hex, public_hex)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes_raw().hex()
    pub = key.public_key().public_bytes_raw().hex()
    return priv, pub

def get_village_key() -> str:
    """Get private key from env or generate+save."""
    env = os.environ.get("NODE_PRIVATE_KEY", "").strip()
    if env:
        return env
    path = Path("data/village/nadi_key.hex")
    if path.exists():
        return path.read_text().strip()
    # Generate new (local dev only — CI must set NODE_PRIVATE_KEY)
    priv, pub = generate_keypair()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(priv)
    path.chmod(0o600)
    print(f"  [nadi] Generated new keypair — public: {pub[:16]}...")
    return priv


# ── Hub Relay ───────────────────────────────────────────

HUB = "kimeisele/steward-federation"
HUB_DIR = "nadi"

def _gh_raw(path: str, token: str = "") -> list[dict]:
    """Read raw JSON from GitHub repo contents."""
    url = f"https://raw.githubusercontent.com/{HUB}/main/{path}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return []

def push_to_hub(agent_id: str, messages: list[NadiMessage], token: str = "") -> int:
    """Push messages to steward-federation hub. One mailbox per target."""
    if not messages or not token:
        return 0
    by_target: dict[str, list[dict]] = {}
    for m in messages:
        if m.is_expired:
            continue
        by_target.setdefault(m.target, []).append(m.to_dict())

    pushed = 0
    for target, batch in by_target.items():
        filename = f"{HUB_DIR}/{agent_id}_to_{target}.json"
        existing = _gh_raw(filename, token)
        seen = {(d.get("source"), d.get("timestamp")) for d in existing}
        new = [d for d in batch if (d.get("source"), d.get("timestamp")) not in seen]
        if not new:
            continue

        merged = json.dumps(existing + new, indent=2)
        url = f"https://api.github.com/repos/{HUB}/contents/{filename}"
        body = {"message": f"nadi: {agent_id} → {target} ({len(new)} msgs)",
                "content": __import__("base64").b64encode(merged.encode()).decode()}
        # Check if file exists for SHA
        check = urllib.request.Request(url)
        check.add_header("Authorization", f"token {token}")
        check.add_header("Accept", "application/vnd.github.v3+json")
        try:
            with urllib.request.urlopen(check, timeout=10) as r:
                info = json.loads(r.read())
                body["sha"] = info["sha"]
        except Exception:
            pass

        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="PUT")
        req.add_header("Authorization", f"token {token}")
        req.add_header("Accept", "application/vnd.github.v3+json")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status in (200, 201):
                    pushed += len(new)
        except Exception as e:
            print(f"  [nadi] push {target}: {e}")

    return pushed


# ── Village NADI Heartbeat ──────────────────────────────

def nadi_heartbeat(village_id: str, token: str = "") -> int:
    """Send a signed heartbeat to steward-federation."""
    key = get_village_key()
    if not key:
        print("  [nadi] No key — skipping heartbeat")
        return 0

    msg = NadiMessage(
        source=village_id,
        target="steward-federation",
        operation="heartbeat",
        payload={"health": 1.0, "population": 0, "village": village_id},
        private_key_hex=key,
        ttl=900,
    )
    count = push_to_hub(village_id, [msg], token)
    if count:
        print("  [nadi] Heartbeat sent → steward-federation")
    return count
