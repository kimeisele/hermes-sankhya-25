"""Smoke tests for federation scripts."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"


def _run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_render_federation_descriptor(tmp_path: Path) -> None:
    out = tmp_path / "descriptor.json"
    result = _run_script("render_federation_descriptor.py", "--output", str(out))
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text())
    assert data["kind"] == "agent_federation_descriptor"
    assert data["status"] == "active"
    assert "capabilities" in data
    assert "layer" in data
    assert "endpoints" in data


def test_render_agent_card(tmp_path: Path) -> None:
    out = tmp_path / "agent.json"
    result = _run_script("render_agent_card.py", "--output", str(out))
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text())
    assert data["name"] == "Agent Template"
    assert "skills" in data
    assert "federation" in data


def test_export_authority_feed(tmp_path: Path) -> None:
    out_dir = tmp_path / "feed"
    result = _run_script("export_authority_feed.py", "--output-dir", str(out_dir))
    assert result.returncode == 0, result.stderr
    manifest = out_dir / "latest-authority-manifest.json"
    assert manifest.exists()
    data = json.loads(manifest.read_text())
    assert data["kind"] == "source_authority_feed_manifest"


def test_discover_peers_help() -> None:
    result = _run_script("discover_federation_peers.py", "--help")
    assert result.returncode == 0


def test_fetch_peer_authority_help() -> None:
    result = _run_script("fetch_peer_authority.py", "--help")
    assert result.returncode == 0


def test_authority_descriptor_seeds_valid() -> None:
    seeds_path = REPO_ROOT / "data" / "federation" / "authority-descriptor-seeds.json"
    assert seeds_path.exists()
    data = json.loads(seeds_path.read_text())
    assert "descriptor_urls" in data
    assert len(data["descriptor_urls"]) > 0


def test_capabilities_json_valid() -> None:
    caps_path = REPO_ROOT / "docs" / "authority" / "capabilities.json"
    assert caps_path.exists()
    data = json.loads(caps_path.read_text())
    assert data["kind"] == "agent_capability_manifest"
    assert len(data["skills"]) > 0
    assert "federation_interfaces" in data
    assert "produces" in data["federation_interfaces"]


def test_nadi_kit_import() -> None:
    """nadi_kit can be imported and exposes expected API."""
    import importlib

    # Import from installed nadi-kit package
    nadi_kit = importlib.import_module("nadi_kit")

    assert hasattr(nadi_kit, "NadiNode")
    assert hasattr(nadi_kit, "NadiMessage")
    assert hasattr(nadi_kit, "NadiTransport")
    assert hasattr(nadi_kit, "NadiHubRelay")


def test_nadi_node_from_peer_json(tmp_path: Path) -> None:
    """NadiNode can be created from a peer.json file."""
    from nadi_kit import NadiNode

    peer_data = {
        "identity": {
            "city_id": "test-node",
            "slug": "test-node",
            "repo": "kimeisele/test-node",
            "public_key": "",
        },
        "endpoint": {
            "city_id": "test-node",
            "transport": "filesystem",
            "location": "data/federation",
        },
        "capabilities": ["authority-publishing"],
        "nadi": {
            "outbox": "data/federation/nadi_outbox.json",
            "inbox": "data/federation/nadi_inbox.json",
        },
    }
    peer_json = tmp_path / "peer.json"
    peer_json.write_text(json.dumps(peer_data))

    node = NadiNode.from_peer_json(peer_json)
    assert node.agent_id == "test-node"
    assert node.repo == "kimeisele/test-node"
    assert node.capabilities == ["authority-publishing"]


def test_nadi_node_emit_and_receive(tmp_path: Path) -> None:
    """NadiNode can emit messages and read them back from transport."""
    from nadi_kit import NadiNode

    peer_data = {
        "identity": {"city_id": "emit-test"},
        "capabilities": [],
    }
    peer_json = tmp_path / "peer.json"
    peer_json.write_text(json.dumps(peer_data))

    node = NadiNode.from_peer_json(peer_json)
    node.emit("ping", {"data": "hello"}, target="steward")

    outbox = node.transport.read_outbox()
    assert len(outbox) == 1
    assert outbox[0].operation == "ping"
    assert outbox[0].target == "steward"
    assert outbox[0].payload["data"] == "hello"


def test_peer_json_exists() -> None:
    """Template ships with a peer.json in data/federation/."""
    peer_path = REPO_ROOT / "data" / "federation" / "peer.json"
    assert peer_path.exists()
    data = json.loads(peer_path.read_text())
    assert "identity" in data
    assert "nadi" in data
    assert "inbox" in data["nadi"]
    assert "outbox" in data["nadi"]


def test_nadi_inbox_exists() -> None:
    """Template ships with a nadi_inbox.json."""
    inbox_path = REPO_ROOT / "data" / "federation" / "nadi_inbox.json"
    assert inbox_path.exists()
    data = json.loads(inbox_path.read_text())
    assert isinstance(data, list)


def test_well_known_descriptor_matches_schema() -> None:
    desc_path = REPO_ROOT / ".well-known" / "agent-federation.json"
    data = json.loads(desc_path.read_text())
    required = {"kind", "version", "repo_id", "display_name", "status", "capabilities", "layer", "endpoints"}
    assert required.issubset(data.keys()), f"Missing fields: {required - data.keys()}"
