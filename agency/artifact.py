"""Artifact validation helper for Engage and Materialize workflows.

Validates cross-run CTX artifacts against binding requirements:
repository, base SHA, proposal hash, approval state, consumption.
"""
from __future__ import annotations

import json
from typing import Any


EXPECTED_REPOSITORY = "kimeisele/hermes-sankhya-25"


def validate_artifact(artifact_path: str, *,
                      expected_run_id: str = "",
                      expected_repository: str = EXPECTED_REPOSITORY,
                      expected_base_sha: str = "",
                      proposal_id: str = "",
                      proposal_hash: str = "",
                      target_content_id: str = "",
                      must_be_approved: bool = True,
                      must_not_be_consumed: bool = True) -> dict[str, Any]:
    """Validate a CTX artifact against binding requirements.

    Returns the parsed CTX dict on success. Raises ValueError on failure.
    """
    with open(artifact_path) as f:
        ctx = json.load(f)

    errors: list[str] = []

    # Repository binding
    repo = ctx.get("repository", "")
    if repo != expected_repository:
        errors.append(f"Repository mismatch: {repo} != {expected_repository}")

    # Base SHA binding
    if expected_base_sha:
        sha = ctx.get("base_sha", "")
        if sha != expected_base_sha:
            errors.append(f"Base SHA mismatch: {sha} != {expected_base_sha}")

    # Run ID binding
    if expected_run_id:
        run_id = ctx.get("run_id", "")
        if not run_id.startswith(expected_run_id):
            errors.append(f"Run ID binding failed: {run_id} does not start with {expected_run_id}")

    # Proposal validation
    if proposal_id:
        proposals = ctx.get("engagement_proposals", [])
        found = None
        for p in proposals:
            if isinstance(p, dict) and p.get("proposal_id") == proposal_id:
                found = p
                break
        if found is None:
            errors.append(f"Proposal {proposal_id} not found in artifact")

        if found and proposal_hash:
            h = found.get("content_hash", "")
            if h != proposal_hash:
                errors.append(f"Proposal hash mismatch: {h} != {proposal_hash}")

        if found and must_be_approved:
            state = found.get("approval_state", "")
            if state != "approved":
                errors.append(f"Proposal not approved: {state}")

        if found and must_not_be_consumed:
            if found.get("consumed", False):
                errors.append("Proposal already consumed")

        if found and target_content_id:
            tgt = found.get("target_content_id", "")
            if tgt != target_content_id:
                errors.append(f"Target mismatch: {tgt} != {target_content_id}")

    # Allowlist check
    if target_content_id:
        allowlist = ctx.get("policy", {}).get("target_allowlist", [])
        if allowlist and target_content_id not in allowlist:
            errors.append(f"Target {target_content_id} not in allowlist")

    # Original posts forbidden
    if ctx.get("policy", {}).get("allow_original_posts", False):
        errors.append("Original posts must remain forbidden")

    if errors:
        raise ValueError("; ".join(errors))

    return ctx
