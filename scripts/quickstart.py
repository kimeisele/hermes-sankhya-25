"""Quickstart: verify your federation node is correctly configured.

Runs all generation scripts, validates outputs, and optionally
discovers peers — giving you a working node in under 60 seconds.

Usage:
    python scripts/quickstart.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _run(label: str, *args: str) -> bool:
    print(f"  {label} ... ", end="", flush=True)
    result = subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode == 0:
        print(f"{GREEN}ok{RESET}")
        return True
    print(f"{RED}FAIL{RESET}")
    if result.stderr:
        for line in result.stderr.strip().splitlines()[:5]:
            print(f"    {line}")
    return False


def _check_json(label: str, path: Path, required_fields: set[str]) -> bool:
    print(f"  {label} ... ", end="", flush=True)
    if not path.exists():
        print(f"{RED}missing{RESET}")
        return False
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"{RED}invalid JSON: {e}{RESET}")
        return False
    missing = required_fields - data.keys()
    if missing:
        print(f"{RED}missing fields: {missing}{RESET}")
        return False
    print(f"{GREEN}ok{RESET}")
    return True


def main() -> int:
    print(f"\n{BOLD}Federation Node Quickstart{RESET}\n")

    ok = True

    # Step 1: Generate
    print(f"{BOLD}1. Generate descriptors{RESET}")
    ok &= _run("Federation descriptor", "scripts/render_federation_descriptor.py")
    ok &= _run("A2A Agent Card", "scripts/render_agent_card.py")
    ok &= _run("Authority feed", "scripts/export_authority_feed.py")

    # Step 2: Validate
    print(f"\n{BOLD}2. Validate outputs{RESET}")
    ok &= _check_json(
        "Federation descriptor",
        REPO_ROOT / ".well-known" / "agent-federation.json",
        {"kind", "version", "repo_id", "display_name", "status", "capabilities", "layer", "endpoints"},
    )
    ok &= _check_json(
        "A2A Agent Card",
        REPO_ROOT / ".well-known" / "agent.json",
        {"name", "version", "capabilities", "skills", "federation"},
    )
    ok &= _check_json(
        "Capability manifest",
        REPO_ROOT / "docs" / "authority" / "capabilities.json",
        {"kind", "version", "skills", "federation_interfaces"},
    )
    ok &= _check_json(
        "Authority descriptor seeds",
        REPO_ROOT / "data" / "federation" / "authority-descriptor-seeds.json",
        {"descriptor_urls"},
    )

    # Step 3: Tests
    print(f"\n{BOLD}3. Run tests{RESET}")
    ok &= _run("pytest", "-m", "pytest", "tests/", "-q")

    # Step 4: Discovery (optional, may fail without network)
    print(f"\n{BOLD}4. Peer discovery (seed-based){RESET}")
    _run("Discover peers", "scripts/discover_federation_peers.py", "--seeds-only", "--output", ".federation/peers.json")

    # Summary
    print()
    if ok:
        print(f"{GREEN}{BOLD}Your federation node is ready.{RESET}")
        print(f"Next: customize docs/authority/charter.md and docs/authority/capabilities.json")
        print(f"Then: push to main and add the 'agent-federation-node' topic on GitHub\n")
    else:
        print(f"{RED}{BOLD}Some checks failed. See above for details.{RESET}\n")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
