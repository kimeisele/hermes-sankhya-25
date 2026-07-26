"""Intelligence-contract validation tests.

Verifies that the node's key contracts (capabilities, templates, descriptors,
agent cards, relationship model, and audit structures) are structurally valid
and internally consistent.

These tests remain in the repository to make validation durable and reproducible.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

# ---------------------------------------------------------------------------
# Claim status vocabulary (must match AGENTS.md Claim Status Vocabulary table)
# ---------------------------------------------------------------------------
VALID_CLAIM_STATUSES = frozenset({
    "observed",
    "verified",
    "supported",
    "inferred",
    "proposed",
    "disputed",
    "unsupported",
    "unknown",
})

VALID_VERIFICATION_STATES = frozenset({"unchecked", "checked", "failed"})

# Evaluation axes from AGENTS.md Response Evaluation (all 0-3)
VALID_EVALUATION_DIMENSIONS = frozenset({
    "relevance",
    "novelty",
    "evidence",
    "falsifiability",
    "actionability",
})

# Session-report sections required by the completed audit template
REQUIRED_SESSION_SECTIONS = frozenset({
    "## Session Scope",
    "## Inquiries Touched",
    "## Moltbook Actions Performed",
    "## Published URLs",
    "## New Source Records",
    "## Relationships Updated",
    "## Claims Discovered",
    "## Evidence Inspected",
    "## Repository Files Changed",
    "## Security Events or Suspicious Instructions",
    "## Unresolved Questions",
    "## Limitations",
    "## Recommended Next Session Focus",
})

# Synthesis-report required sections (including the new Corrections section)
REQUIRED_SYNTHESIS_SECTIONS = frozenset({
    "## Executive Finding",
    "## Inquiry Question",
    "## Sources Inspected",
    "## Strongest Supported Claims",
    "## Important Disagreements",
    "## Novel Hypotheses",
    "## Rejected or Unsupported Claims",
    "## Evidence Gaps",
    "## Security Concerns",
    "## Proposed Tests",
    "## Recommended Next Action",
    "## Confidence and Limitations",
    "## Corrections and Supersession History",
})


def _run_script(name: str, *args: str,
                extra_env: dict[str, str] | None = None,
                unset_env: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if unset_env is not None:
        for key in unset_env:
            env.pop(key, None)
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Capability manifest
# ---------------------------------------------------------------------------

def test_capability_manifest_identity_and_role() -> None:
    """Capability manifest declares correct node identity and role."""
    manifest = _load_json(REPO_ROOT / "docs" / "authority" / "capabilities.json")
    assert manifest["kind"] == "agent_capability_manifest"
    assert manifest["node_id"] == "hermes-sankhya-25"
    assert manifest["display_name"] == "Hermes Sankhya-25"
    assert manifest["node_role"] == "external_intelligence_contributor"


def test_capability_manifest_has_required_skills() -> None:
    """Capability manifest includes authority-publishing (baseline) and the five
    specialized intelligence skills."""
    manifest = _load_json(REPO_ROOT / "docs" / "authority" / "capabilities.json")
    skill_ids = {s["id"] for s in manifest["skills"]}

    # Baseline
    assert "authority-publishing" in skill_ids, \
        "authority-publishing must be present as the node's baseline Federation skill"

    # Specialized intelligence skills
    required_specialized = {
        "external-signal-collection",
        "structured-inquiry",
        "evidence-triage",
        "cross-agent-synthesis",
        "public-discussion-engagement",
    }
    missing = required_specialized - skill_ids
    assert not missing, f"Missing specialized intelligence skills: {missing}"


# ---------------------------------------------------------------------------
# Generated descriptor identity
# ---------------------------------------------------------------------------

def test_generated_descriptor_identity() -> None:
    """Descriptor rendered with default identity must be Hermes, not Agent Template."""
    out = Path("/tmp") / "test-intel-descriptor.json"
    result = _run_script(
        "render_federation_descriptor.py", "--output", str(out),
        unset_env=["GITHUB_REPOSITORY"],
    )
    assert result.returncode == 0, result.stderr
    data = _load_json(out)
    assert data["repo_id"] == "hermes-sankhya-25"
    assert data["display_name"] == "Hermes Sankhya 25"
    assert "hermes_sankhya_25_surface" in data["owner_boundary"]


def test_generated_agent_card_identity() -> None:
    """Agent card rendered with default identity must be Hermes, not Agent Template."""
    out = Path("/tmp") / "test-intel-agent.json"
    result = _run_script(
        "render_agent_card.py", "--output", str(out),
        unset_env=["GITHUB_REPOSITORY"],
    )
    assert result.returncode == 0, result.stderr
    data = _load_json(out)
    assert data["name"] == "Hermes Sankhya 25"
    assert "kimeisele/hermes-sankhya-25" in data["url"]


# ---------------------------------------------------------------------------
# Relationship seed structure
# ---------------------------------------------------------------------------

def test_relationship_seed_structure() -> None:
    """relationships/agents.json has a valid structure."""
    path = REPO_ROOT / "relationships" / "agents.json"
    data = _load_json(path)
    assert "updated" in data, "agents.json must have an 'updated' field"
    assert "agents" in data, "agents.json must have an 'agents' object"
    assert isinstance(data["agents"], dict), "agents must be a dict (keyed by handle)"


# ---------------------------------------------------------------------------
# Source-record structure
# ---------------------------------------------------------------------------

def test_source_record_structure() -> None:
    """source-record.json template uses structured claim objects, not plain strings."""
    path = REPO_ROOT / "templates" / "source-record.json"
    data = _load_json(path)

    # Top-level fields
    required_top = {
        "source_id", "url", "author_handle", "observed_at", "content_type",
        "paraphrase", "claims", "evidence_refs", "inquiry_ids",
        "relationship_id", "security_flags", "relevance_score", "evaluation",
    }
    missing_top = required_top - set(data.keys())
    assert not missing_top, f"Missing top-level fields: {missing_top}"

    # Claims must be a list and the first entry must be a dict, not a string
    claims = data["claims"]
    assert isinstance(claims, list), "claims must be a list"
    assert len(claims) > 0, "claims template must have at least one example entry"

    first_claim = claims[0]
    assert isinstance(first_claim, dict), \
        f"claims entries must be objects, got {type(first_claim).__name__}"

    claim_required = {
        "claim_id", "text", "status", "evidence_refs",
        "verification_state", "notes",
    }
    missing_claim = claim_required - set(first_claim.keys())
    assert not missing_claim, f"Claim object missing fields: {missing_claim}"


def test_claim_status_vocabulary() -> None:
    """Every claim in the source-record template must use a valid claim status."""
    path = REPO_ROOT / "templates" / "source-record.json"
    data = _load_json(path)

    for claim in data["claims"]:
        assert claim["status"] in VALID_CLAIM_STATUSES, \
            f"Claim status '{claim['status']}' not in valid vocabulary: {sorted(VALID_CLAIM_STATUSES)}"

    assert len(VALID_CLAIM_STATUSES) == 8


def test_claim_verification_state_vocabulary() -> None:
    """Every claim in the source-record template must use a valid verification_state."""
    path = REPO_ROOT / "templates" / "source-record.json"
    data = _load_json(path)

    for claim in data["claims"]:
        assert claim["verification_state"] in VALID_VERIFICATION_STATES, \
            f"Claim verification_state '{claim['verification_state']}' not in valid vocabulary: {sorted(VALID_VERIFICATION_STATES)}"

    assert len(VALID_VERIFICATION_STATES) == 3


# ---------------------------------------------------------------------------
# Evaluation dimensions and score range
# ---------------------------------------------------------------------------

def test_evaluation_dimensions() -> None:
    """Source-record template has the five AGENTS.md evaluation dimensions."""
    path = REPO_ROOT / "templates" / "source-record.json"
    data = _load_json(path)

    eval_obj = data.get("evaluation", {})
    actual_dims = set(eval_obj.keys()) - {"notes"}
    missing = VALID_EVALUATION_DIMENSIONS - actual_dims
    assert not missing, f"Missing evaluation dimensions: {missing}"


def test_evaluation_score_range() -> None:
    """Evaluation template values are within valid 0-3 range."""
    path = REPO_ROOT / "templates" / "source-record.json"
    data = _load_json(path)

    eval_obj = data.get("evaluation", {})
    for dim in VALID_EVALUATION_DIMENSIONS:
        val = eval_obj.get(dim)
        if isinstance(val, (int, float)):
            assert 0 <= val <= 3, f"{dim} score {val} out of valid 0-3 range"


# ---------------------------------------------------------------------------
# Session-report audit sections
# ---------------------------------------------------------------------------

def test_session_report_required_sections() -> None:
    """session-report.md contains all required audit sections."""
    path = REPO_ROOT / "templates" / "session-report.md"
    content = path.read_text()

    for section in REQUIRED_SESSION_SECTIONS:
        assert section in content, f"Missing required session-report section: {section}"


# ---------------------------------------------------------------------------
# Synthesis-report corrections/supersession section
# ---------------------------------------------------------------------------

def test_synthesis_report_required_sections() -> None:
    """synthesis-report.md contains all required sections, including Corrections."""
    path = REPO_ROOT / "templates" / "synthesis-report.md"
    content = path.read_text()

    for section in REQUIRED_SYNTHESIS_SECTIONS:
        assert section in content, f"Missing required synthesis-report section: {section}"


def test_synthesis_report_has_corrections_section() -> None:
    """Synthesis report explicitly has a Corrections and Supersession History section."""
    path = REPO_ROOT / "templates" / "synthesis-report.md"
    content = path.read_text()
    assert "## Corrections and Supersession History" in content, \
        "Synthesis report must include a durable Corrections and Supersession History section"


# ---------------------------------------------------------------------------
# Federation descriptor feeds
# ---------------------------------------------------------------------------

def test_descriptor_includes_authority_feed() -> None:
    """Generated federation descriptor includes a valid authority feed URL."""
    out = Path("/tmp") / "test-intel-feed-descriptor.json"
    result = _run_script(
        "render_federation_descriptor.py", "--output", str(out),
        unset_env=["GITHUB_REPOSITORY"],
    )
    assert result.returncode == 0, result.stderr
    data = _load_json(out)
    assert "authority_feed_manifest_url" in data
    assert "kimeisele/hermes-sankhya-25/authority-feed/" in data["authority_feed_manifest_url"]


def test_agent_card_includes_federation_interfaces() -> None:
    """Generated agent card exposes capability surface consistently."""
    out = Path("/tmp") / "test-intel-card.json"
    result = _run_script(
        "render_agent_card.py", "--output", str(out),
        unset_env=["GITHUB_REPOSITORY"],
    )
    assert result.returncode == 0, result.stderr
    data = _load_json(out)

    assert "federation" in data
    fed = data["federation"]
    assert "authority_feed_branch" in fed
    assert "interfaces" in fed

    # Capability surface: must be consistent with capabilities.json
    manifest = _load_json(REPO_ROOT / "docs" / "authority" / "capabilities.json")
    manifest_skills = {s["id"] for s in manifest["skills"]}
    card_skills = {s["id"] for s in data["skills"]}
    assert "authority-publishing" in card_skills, \
        "Agent card must advertise authority-publishing (matches capabilities.json)"
    assert manifest_skills == card_skills, \
        f"Agent card skills must match capabilities.json. Extra: {card_skills - manifest_skills}, Missing: {manifest_skills - card_skills}"
