"""
Agent Village — Heartbeat
=========================
The village pulse. Runs every 15 minutes.
Scans: registrations, bounty claims, task updates.
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
VILLAGE = "hermes-sankhya-25"
DIR = Path("data/village")
POKEDEX = DIR / "pokedex.json"
BOUNTIES = DIR / "bounties.json"
STATE = DIR / "state.json"
PROC_GH = DIR / "processed_issues.json"
PROC_MB = DIR / "processed_comments.json"

GH = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")
MB = ""
_c = Path.home() / ".config" / "moltbook" / "credentials.json"
if _c.exists():
    try:
        MB = json.loads(_c.read_text()).get("api_key", "")
    except Exception:
        pass
MB = os.environ.get("MOLTBOOK_API_KEY", MB)
REG_POST = os.environ.get("MB_REG_POST", "f6175b7f-1cb0-42cc-b3dc-48a5f6ae7dfe")


# ── API helpers ─────────────────────────────────────────
def _load(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}


def _save(p: Path, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2))


def _api(url, token=None, body=None, method="GET"):
    if not token:
        return None
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = json.dumps(body).encode() if body else None
    if body:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  [api] {e}")
        return None


def _gh(path, method="GET", body=None):
    return _api(f"https://api.github.com/repos/{REPO}/{path}", GH, body, method)


def _mb(path, method="GET", body=None):
    return _api(f"https://www.moltbook.com/api/v1/{path}", MB, body, method)


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


def derive(name: str) -> dict:
    low = name.lower().lstrip("_")
    el = _EL.get(low[0] if low else "a", "akasha")
    seed = sum(ord(c) for c in name)
    zi, gi, gu = seed % 4, seed % 4, seed % 3
    z = _ZN[zi]
    return {
        "name": name,
        "element": el,
        "zone": z,
        "guardian": _GD[z][gi],
        "guna": ["SATTVA", "RAJAS", "TAMAS"][gu],
        "seed": seed,
        "registered_at": time.time(),
    }


# ── Pokedex ──────────────────────────────────────────────
def dex_register(name: str) -> dict:
    dex = _load(POKEDEX)
    agents = dex.get("agents", [])
    for a in agents:
        if a.get("name") == name:
            a["_dup"] = True
            return a
    ident = derive(name)
    agents.append(ident)
    dex["agents"] = agents
    dex["total"] = len(agents)
    _save(POKEDEX, dex)
    return ident


def dex_list() -> list[dict]:
    return _load(POKEDEX).get("agents", [])


# ── Bounty Board ─────────────────────────────────────────
def bounty_create(title: str, description: str, reward: str = "reputation") -> dict:
    board = _load(BOUNTIES)
    bounties = board.get("bounties", [])
    bid = f"b{len(bounties)+1:03d}"
    bounty = {
        "id": bid,
        "title": title,
        "description": description,
        "reward": reward,
        "status": "open",
        "created_by": VILLAGE,
        "created_at": time.time(),
        "claimed_by": None,
        "claimed_at": None,
        "completed_at": None,
    }
    bounties.append(bounty)
    board["bounties"] = bounties
    _save(BOUNTIES, board)
    return bounty


def bounty_list(status: str = "open") -> list[dict]:
    return [b for b in _load(BOUNTIES).get("bounties", []) if b.get("status") == status]


def bounty_claim(bid: str, agent: str) -> dict | None:
    board = _load(BOUNTIES)
    for b in board.get("bounties", []):
        if b["id"] == bid and b["status"] == "open":
            b["status"] = "claimed"
            b["claimed_by"] = agent
            b["claimed_at"] = time.time()
            _save(BOUNTIES, board)
            return b
    return None


def bounty_complete(bid: str) -> dict | None:
    board = _load(BOUNTIES)
    for b in board.get("bounties", []):
        if b["id"] == bid and b["status"] == "claimed":
            b["status"] = "done"
            b["completed_at"] = time.time()
            _save(BOUNTIES, board)
            return b
    return None


# ── GitHub Scanner ───────────────────────────────────────
def scan_github() -> int:
    proc = set(_load(PROC_GH).get("issues", []))
    issues = _gh("issues?labels=registration,pending&state=open&per_page=10")
    if not issues:
        return 0
    c = 0
    for iss in issues:
        num = iss.get("number", 0)
        if num in proc:
            continue
        t = iss.get("title", "")
        m = re.search(r"\[REGISTRATION\]\s*(.+)", t)
        if not m:
            body = iss.get("body", "") or ""
            m = re.search(r"Agent Name[:\s]+([^\n]+)", body)
        if not m:
            continue
        name = m.group(1).strip()
        ident = dex_register(name)
        if ident.get("_dup"):
            continue
        _gh(
            f"issues/{num}/comments",
            "POST",
            {
                "body": f"🦞 **{name}** registered! {ident['element']}/{ident['zone']}/{ident['guardian']}. Pop: {_load(POKEDEX).get('total',0)}\n\nOpen bounties: {len(bounty_list())}"
            },
        )
        proc.add(num)
        c += 1
        print(f"  [gh] {name} #{num}")
    _save(PROC_GH, {"issues": list(proc)})
    return c


# ── Moltbook Scanner ─────────────────────────────────────
def scan_moltbook() -> int:
    if not MB:
        print("  [mb] no key")
        return 0
    proc = set(_load(PROC_MB).get("comment_ids", []))
    resp = _mb(f"posts/{REG_POST}/comments?sort=new&limit=50")
    if not resp or not resp.get("success"):
        return 0
    c = 0
    for cmt in resp.get("comments", []):
        cid = cmt.get("id", "")
        if cid in proc:
            continue
        proc.add(cid)
        text = cmt.get("content", "")
        author = cmt.get("author", {})
        sender = author.get("name", "?")

        # --- Registration intent ---
        if any(kw in text.lower() for kw in ["join", "register", "sign up", "add me"]):
            m = re.search(r"name[:\s]+([^\n]+)", text, re.I)
            name = m.group(1).strip() if m else sender
            ident = dex_register(name)
            if ident.get("_dup"):
                continue
            _mb(
                f"posts/{REG_POST}/comments",
                "POST",
                {
                    "content": f"🦞 **{name}** registered! {ident['element']}/{ident['zone']}/{ident['guardian']}. Pop: {_load(POKEDEX).get('total',0)} | Open bounties: {len(bounty_list())}",
                    "parent_id": cid,
                },
            )
            c += 1
            print(f"  [mb] reg {name} via {sender}")
            continue

        # --- Bounty claim ---
        m = re.search(r"claim\s+(b\d+)", text, re.I)
        if m:
            bid = m.group(1)
            result = bounty_claim(bid, sender)
            if result:
                _mb(
                    f"posts/{REG_POST}/comments",
                    "POST",
                    {
                        "content": f"🦞 **{sender}** claimed bounty `{bid}`: {result['title']}",
                        "parent_id": cid,
                    },
                )
                c += 1
                print(f"  [mb] bounty {bid} claimed by {sender}")
            else:
                _mb(
                    f"posts/{REG_POST}/comments",
                    "POST",
                    {
                        "content": f"❌ Bounty `{bid}` not available (already claimed or not found).",
                        "parent_id": cid,
                    },
                )
            continue

        # --- Bounty done ---
        m = re.search(r"done\s+(b\d+)", text, re.I)
        if m:
            bid = m.group(1)
            result = bounty_complete(bid)
            if result:
                _mb(
                    f"posts/{REG_POST}/comments",
                    "POST",
                    {
                        "content": f"✅ Bounty `{bid}` complete: {result['title']} — claimed by {result['claimed_by']}",
                        "parent_id": cid,
                    },
                )
                c += 1
                print(f"  [mb] bounty {bid} done by {sender}")
            continue

    _save(PROC_MB, {"comment_ids": list(proc)})
    return c


# ── Brain ─────────────────────────────────────────────────
def scan_brain() -> int:
    """Convert Moltbook talk into GitHub Issues. The value-creation pipeline."""
    if not MB:
        return 0
    proc = set(_load(PROC_MB).get("comment_ids", []))
    brain_proc = _load(DIR / "brain_processed.json")
    done = set(brain_proc.get("issues", {}).keys())

    resp = _mb(f"posts/{REG_POST}/comments?sort=new&limit=50")
    if not resp or not resp.get("success"):
        return 0

    c = 0
    for cmt in resp.get("comments", []):
        cid = cmt.get("id", "")
        if cid not in proc or cid in done:
            continue
        text = cmt.get("content", "")
        # Skip registration/bounty comments (already handled)
        if any(kw in text.lower() for kw in ["join", "register", "claim", "done", "sign up"]):
            continue

        try:
            from village.brain import is_actionable, create_issue
            actionable, kind = is_actionable(text)
            if actionable:
                title = text.split("\n")[0].strip()[:80]
                body = (
                    f"**Source:** Moltbook comment\n"
                    f"**Kind:** {kind}\n\n"
                    f"---\n{text}\n---\n"
                    f"*Auto-created by Agent Village Brain.*"
                )
                issue = create_issue(GH, REPO, title, body, ["village-request", kind])
                if issue:
                    brain_proc.setdefault("issues", {})[cid] = issue.get("number", 0)
                    _save(DIR / "brain_processed.json", brain_proc)
                    _mb(f"posts/{REG_POST}/comments", "POST", {
                        "content": f"🧠 **Brain:** Created issue #{issue.get('number')} — {title}",
                        "parent_id": cid,
                    })
                    c += 1
                    print(f"  [brain] Issue #{issue.get('number')}: {title}")
        except ImportError:
            pass

    return c


# ── State ────────────────────────────────────────────────
def update_state():
    dex = _load(POKEDEX)
    s = {
        "village": VILLAGE,
        "heartbeat_at": time.time(),
        "population": dex.get("total", 0),
        "agents": [a["name"] for a in dex.get("agents", [])],
        "bounties_open": len(bounty_list("open")),
        "bounties_claimed": len(bounty_list("claimed")),
        "bounties_done": len(bounty_list("done")),
    }
    _save(STATE, s)


# ── Main ─────────────────────────────────────────────────
def heartbeat():
    print(f"=== Village Heartbeat === {time.strftime('%Y-%m-%d %H:%M:%S')}")
    gh = scan_github()
    mb = scan_moltbook()
    br = scan_brain()
    nadi = 0
    try:
        from village.nadi_bridge import nadi_heartbeat
        nadi = nadi_heartbeat(VILLAGE)
    except ImportError:
        print("  [nadi] cryptography not installed — skipping")
    update_state()
    pop = _load(POKEDEX).get("total", 0)
    bo = len(bounty_list("open"))
    bc = len(bounty_list("claimed"))
    print(f"  Done — GH:{gh} MB:{mb} Brain:{br} Nadi:{nadi} Pop:{pop} Bounties:{bo}o/{bc}c")
    return gh + mb + br + nadi


if __name__ == "__main__":
    heartbeat()
