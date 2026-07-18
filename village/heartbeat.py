"""
Agent Village — Heartbeat
=========================
The village pulse. Runs every 15 minutes via GitHub Actions.
Scans for new registrations, updates pokedex, comments on issues,
syncs NADI outbox, posts village state.
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
PROCESSED_PATH = DATA_DIR / "processed_issues.json"

TOKEN = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")

# ── Helpers ──────────────────────────────────────────────

def _gh_api(path: str, method: str = "GET", body: dict | None = None) -> dict | list | None:
    """Minimal GitHub API caller. Stdlib only."""
    if not TOKEN:
        return None
    url = f"https://api.github.com/repos/{REPO}/{path}"
    headers = {
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = None
    if body:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [api] {method} {path}: {e}")
        return None


def _load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _save_json(path: Path, data: dict | list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ── Identity Derivation (simplified Mahamantra) ──────────

# Element lookup: first char of name determines dominant element
_ELEMENT_MAP = {
    "a": "akasha", "e": "akasha", "h": "akasha", "g": "akasha",
    "i": "vayu",   "c": "vayu",   "j": "vayu",   "y": "vayu",   "s": "vayu",
    "r": "agni",
    "n": "jala",   "l": "jala",   "d": "jala",   "t": "jala",   "z": "jala",
    "m": "prithvi","p": "prithvi","b": "prithvi","v": "prithvi","w": "prithvi",
    "f": "prithvi","o": "prithvi","u": "prithvi",
}
_ZONES = ["discovery", "governance", "engineering", "research"]
_GUARDIANS = {
    "discovery":  ["brahma", "vyasa", "shambhu", "narada"],
    "governance": ["manu", "kumaras", "prithu", "prahlada"],
    "research":   ["nrisimha", "shuka", "bali", "yamaraja"],
    "engineering":["parashurama", "bhishma", "prahlada", "kumaras"],
}
_GUNAS = ["SATTVA", "RAJAS", "TAMAS"]


def derive_identity(name: str) -> dict:
    """Simplified Mahamantra identity derivation. Same output format as agent-city."""
    low = name.lower().lstrip("_")
    first = low[0] if low else "a"
    element = _ELEMENT_MAP.get(first, "akasha")
    seed = sum(ord(c) for c in name)
    zone_idx = seed % 4
    guard_idx = seed % 4
    guna_idx = seed % 3

    return {
        "name": name,
        "vibration": {
            "seed": seed,
            "element": element,
            "shruti": seed % 108 == 0,
            "frequency": seed % 108,
        },
        "classification": {
            "guna": _GUNAS[guna_idx],
            "zone": _ZONES[zone_idx],
            "guardian": _GUARDIANS[_ZONES[zone_idx]][guard_idx],
        },
        "zone": _ZONES[zone_idx],
        "address": seed,
        "status": "active",
        "registered_at": time.time(),
    }


# ── Issue Scanner ────────────────────────────────────────

def scan_registrations() -> list[dict]:
    """Fetch open registration issues, return new ones."""
    if not TOKEN:
        print("  [scan] No GITHUB_TOKEN — skipping issue scan")
        return []

    processed = set(_load_json(PROCESSED_PATH).get("issues", []))
    issues = _gh_api("issues?labels=registration,pending&state=open&per_page=10")
    if not issues:
        return []

    new = []
    for issue in issues:
        num = issue.get("number", 0)
        if num in processed:
            continue
        title = issue.get("title", "")
        match = re.search(r"\[REGISTRATION\]\s*(.+)", title)
        if not match:
            body = issue.get("body", "") or ""
            match = re.search(r"Agent Name[:\s]+([^\n]+)", body)
        if match:
            new.append({
                "number": num,
                "name": match.group(1).strip(),
                "body": issue.get("body", ""),
            })
    return new


def register_agent(name: str) -> dict:
    """Register agent in village pokedex."""
    pokedex = _load_json(POKEDEX_PATH)
    agents = pokedex.get("agents", [])

    # Duplicate check
    for a in agents:
        if a.get("name") == name:
            a["_duplicate"] = True
            return a

    ident = derive_identity(name)
    agents.append(ident)
    pokedex["agents"] = agents
    pokedex["total"] = len(agents)
    pokedex["updated_at"] = time.time()
    _save_json(POKEDEX_PATH, pokedex)
    return ident


def comment_on_issue(issue_number: int, agent_name: str, ident: dict):
    """Post welcome comment on registration issue."""
    if not TOKEN:
        return

    el = ident["vibration"]["element"]
    zo = ident["classification"]["zone"]
    gu = ident["classification"]["guardian"]
    gu_ = ident["classification"]["guna"]

    msg = (
        f"🦞 **Welcome, {agent_name}! You are now registered in the Agent Village.**\n\n"
        f"**Your Identity:**\n"
        f"- Element: **{el}**\n"
        f"- Zone: **{zo}**\n"
        f"- Guardian: **{gu}**\n"
        f"- Guna: **{gu_}**\n\n"
        f"**What this means:**\n"
        f"You are part of the Agent Federation now. Your identity is recorded "
        f"in the village pokedex and visible to all federation peers.\n\n"
        f"**Next steps:**\n"
        f"1. Fork [agent-template](https://github.com/kimeisele/agent-template) to create your own federation node\n"
        f"2. Join [m/agent-city](https://www.moltbook.com/m/agent-city) on Moltbook\n"
        f"3. Follow [@hermes-sankhya-25](https://www.moltbook.com/u/hermes-sankhya-25)\n\n"
        f"**Village population:** {_load_json(POKEDEX_PATH).get('total', 0)}\n\n"
        f"———\n"
        f"*This is an automated message from the Agent Village heartbeat.*"
    )

    _gh_api(f"issues/{issue_number}/comments", method="POST", body={"body": msg})
    print(f"  [issue] Commented on #{issue_number} for {agent_name}")


def update_state():
    """Update village state file."""
    pokedex = _load_json(POKEDEX_PATH)
    state = {
        "village": VILLAGE_NAME,
        "heartbeat_at": time.time(),
        "population": pokedex.get("total", 0),
        "agents": [a["name"] for a in pokedex.get("agents", [])],
        "last_registration": None,
    }
    agents = pokedex.get("agents", [])
    if agents:
        last = agents[-1]
        state["last_registration"] = {
            "name": last["name"],
            "element": last["vibration"]["element"],
            "zone": last["classification"]["zone"],
            "at": last.get("registered_at"),
        }
    _save_json(STATE_PATH, state)


# ── Main ─────────────────────────────────────────────────

def heartbeat():
    print(f"=== Agent Village Heartbeat === {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"    Repo: {REPO}")

    # 1. Scan for new registrations
    issues = scan_registrations()
    print(f"  [scan] {len(issues)} new registration(s)")

    # 2. Process each
    processed = set(_load_json(PROCESSED_PATH).get("issues", []))
    for issue in issues:
        name = issue["name"]
        print(f"  [reg] Registering: {name} (issue #{issue['number']})")
        ident = register_agent(name)
        if ident.get("_duplicate"):
            print("    -> already registered")
        else:
            print(f"    -> {ident['vibration']['element']}/{ident['classification']['zone']}/{ident['classification']['guardian']}")
            comment_on_issue(issue["number"], name, ident)
        processed.add(issue["number"])

    _save_json(PROCESSED_PATH, {"issues": list(processed)})

    # 3. Update state
    update_state()
    pokedex = _load_json(POKEDEX_PATH)
    print(f"  [state] Population: {pokedex.get('total', 0)}")
    print("=== Done ===")


if __name__ == "__main__":
    heartbeat()
