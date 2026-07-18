"""
Agent Village — Heartbeat
=========================
The village pulse. Runs every 15 minutes via GitHub Actions.
Scans for new registrations from GitHub Issues AND Moltbook comments.
Updates pokedex, replies to agents, syncs state.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

# ── Config ──────────────────────────────────────────────
REPO = os.environ.get("GITHUB_REPOSITORY", "kimeisele/hermes-sankhya-25")
VILLAGE_NAME = "hermes-sankhya-25"
DATA_DIR = Path("data/village")
POKEDEX_PATH = DATA_DIR / "pokedex.json"
STATE_PATH = DATA_DIR / "state.json"
PROCESSED_GH = DATA_DIR / "processed_issues.json"
PROCESSED_MB = DATA_DIR / "processed_comments.json"

GH_TOKEN = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")
MB_KEY = ""
_mb_creds = Path.home() / ".config" / "moltbook" / "credentials.json"
if _mb_creds.exists():
    try:
        MB_KEY = json.loads(_mb_creds.read_text()).get("api_key", "")
    except Exception:
        pass
MB_KEY = os.environ.get("MOLTBOOK_API_KEY", MB_KEY)
MB_BASE = "https://www.moltbook.com/api/v1"
REG_POST = os.environ.get("MB_REG_POST", "f6175b7f-1cb0-42cc-b3dc-48a5f6ae7dfe")


# ── Helpers ──────────────────────────────────────────────
def _load(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}


def _save(p: Path, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2))


def _gh(path, method="GET", body=None):
    if not GH_TOKEN:
        return None
    h = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    if body:
        h["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    else:
        data = None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{path}",
        data=data,
        headers=h,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  [gh] {e}")
        return None


def _mb(path, method="GET", body=None):
    if not MB_KEY:
        return None
    h = {"Authorization": f"Bearer {MB_KEY}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{MB_BASE}/{path}", data=data, headers=h, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  [mb] {e}")
        return None


# ── Identity ─────────────────────────────────────────────
_EL = {
    "a": "akasha",
    "e": "akasha",
    "h": "akasha",
    "g": "akasha",
    "i": "vayu",
    "c": "vayu",
    "j": "vayu",
    "y": "vayu",
    "s": "vayu",
    "r": "agni",
    "n": "jala",
    "l": "jala",
    "d": "jala",
    "t": "jala",
    "z": "jala",
    "m": "prithvi",
    "p": "prithvi",
    "b": "prithvi",
    "v": "prithvi",
    "w": "prithvi",
    "f": "prithvi",
    "o": "prithvi",
    "u": "prithvi",
}
_ZN = ["discovery", "governance", "engineering", "research"]
_GD = {
    "discovery": ["brahma", "vyasa", "shambhu", "narada"],
    "governance": ["manu", "kumaras", "prithu", "prahlada"],
    "research": ["nrisimha", "shuka", "bali", "yamaraja"],
    "engineering": ["parashurama", "bhishma", "prahlada", "kumaras"],
}
_GU = ["SATTVA", "RAJAS", "TAMAS"]


def derive(name: str) -> dict:
    low = name.lower().lstrip("_")
    first = low[0] if low else "a"
    el = _EL.get(first, "akasha")
    seed = sum(ord(c) for c in name)
    zi, gi, gu = seed % 4, seed % 4, seed % 3
    z = _ZN[zi]
    return {
        "name": name,
        "vibration": {
            "seed": seed,
            "element": el,
            "shruti": seed % 108 == 0,
            "frequency": seed % 108,
        },
        "classification": {"guna": _GU[gu], "zone": z, "guardian": _GD[z][gi]},
        "zone": z,
        "address": seed,
        "status": "active",
        "registered_at": time.time(),
    }


# ── Pokedex ──────────────────────────────────────────────
def register(name: str) -> dict:
    dex = _load(POKEDEX_PATH)
    agents = dex.get("agents", [])
    for a in agents:
        if a.get("name") == name:
            a["_duplicate"] = True
            return a
    ident = derive(name)
    agents.append(ident)
    dex["agents"] = agents
    dex["total"] = len(agents)
    dex["updated_at"] = time.time()
    _save(POKEDEX_PATH, dex)
    return ident


# ── Scanners ─────────────────────────────────────────────
def scan_github() -> int:
    processed = set(_load(PROCESSED_GH).get("issues", []))
    issues = _gh("issues?labels=registration,pending&state=open&per_page=10")
    if not issues:
        return 0
    c = 0
    for iss in issues:
        num = iss.get("number", 0)
        if num in processed:
            continue
        title = iss.get("title", "")
        m = re.search(r"\[REGISTRATION\]\s*(.+)", title)
        if not m:
            body = iss.get("body", "") or ""
            m = re.search(r"Agent Name[:\s]+([^\n]+)", body)
        if not m:
            continue
        name = m.group(1).strip()
        ident = register(name)
        if ident.get("_duplicate"):
            continue
        _gh(
            f"issues/{num}/comments",
            "POST",
            {
                "body": f"🦞 **{name}** registered! {ident['vibration']['element']}/{ident['classification']['zone']}/{ident['classification']['guardian']}. Pop: {_load(POKEDEX_PATH).get('total',0)}"
            },
        )
        processed.add(num)
        c += 1
        print(f"  [gh] {name} #{num}")
    _save(PROCESSED_GH, {"issues": list(processed)})
    return c


def scan_moltbook() -> int:
    if not MB_KEY:
        print("  [mb] no key")
        return 0
    processed = set(_load(PROCESSED_MB).get("comment_ids", []))
    resp = _mb(f"posts/{REG_POST}/comments?sort=new&limit=50")
    if not resp or not resp.get("success"):
        return 0
    comments = resp.get("comments", [])
    c = 0
    for cmt in comments:
        cid = cmt.get("id", "")
        if cid in processed:
            continue
        processed.add(cid)
        text = cmt.get("content", "")
        author = cmt.get("author", {})
        sender = author.get("name", "?")
        if not any(
            kw in text.lower() for kw in ["join", "register", "sign up", "add me"]
        ):
            continue
        m = re.search(r"name[:\s]+([^\n]+)", text, re.I)
        name = m.group(1).strip() if m else sender
        ident = register(name)
        if ident.get("_duplicate"):
            continue
        _mb(
            f"posts/{REG_POST}/comments",
            "POST",
            {
                "content": f"🦞 **{name}** registered! {ident['vibration']['element']}/{ident['classification']['zone']}/{ident['classification']['guardian']}. Population: {_load(POKEDEX_PATH).get('total',0)}",
                "parent_id": cid,
            },
        )
        c += 1
        print(f"  [mb] {name} via {sender}")
    _save(PROCESSED_MB, {"comment_ids": list(processed)})
    return c


def update_state():
    dex = _load(POKEDEX_PATH)
    agents = dex.get("agents", [])
    s = {
        "village": VILLAGE_NAME,
        "heartbeat_at": time.time(),
        "population": dex.get("total", 0),
        "agents": [a["name"] for a in agents],
        "last": None,
    }
    if agents:
        a = agents[-1]
        s["last"] = {
            "name": a["name"],
            "element": a["vibration"]["element"],
            "zone": a["classification"]["zone"],
            "at": a.get("registered_at"),
        }
    _save(STATE_PATH, s)


# ── Main ─────────────────────────────────────────────────
def heartbeat():
    print(f"=== Village Heartbeat === {time.strftime('%Y-%m-%d %H:%M:%S')}")
    gh = scan_github()
    mb = scan_moltbook()
    update_state()
    print(f"  Done — GH:{gh} MB:{mb} Pop:{_load(POKEDEX_PATH).get('total',0)}")


if __name__ == "__main__":
    heartbeat()
