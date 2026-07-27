"""Artifact validation and canonical hashing for Engage/Materialize.

CLI entry point: python -m agency.artifact validate-engagement ...
"""
from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

EXPECTED_REPOSITORY = "kimeisele/hermes-sankhya-25"

# Fields excluded from canonical proposal hashing (mutable lifecycle)
_HASH_EXCLUDED = frozenset({
    "content_hash", "approval_state", "consumed", "consumed_at", "receipt",
})


def canonical_hash(proposal: dict[str, Any]) -> str:
    """Compute canonical SHA-256 hash of a proposal, excluding lifecycle fields."""
    canonical = {k: v for k, v in proposal.items() if k not in _HASH_EXCLUDED}
    payload = json.dumps(canonical, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def validate_inputs(*, workflow_run_id: str, proposal_id: str,
                    target_content_id: str, proposal_hash: str,
                    confirm: str) -> list[str]:
    """Validate workflow inputs. Returns list of errors (empty = valid)."""
    errors: list[str] = []
    import re
    if not re.fullmatch(r"[0-9]+", workflow_run_id):
        errors.append("workflow_run_id must be numeric")
    if not proposal_id or "\n" in proposal_id or "'" in proposal_id or '"' in proposal_id:
        errors.append("proposal_id invalid")
    if not re.fullmatch(r"[a-f0-9-]+", target_content_id):
        errors.append("target_content_id must be UUID-like hex")
    if not re.fullmatch(r"[a-f0-9]{64}", proposal_hash):
        errors.append("proposal_hash must be 64 lowercase hex")
    if confirm != "YES":
        errors.append("confirm must be YES")
    return errors


def validate_artifact(artifact_path: str, *,
                      workflow_run_id: str = "",
                      expected_repository: str = EXPECTED_REPOSITORY,
                      expected_base_sha: str = "",
                      proposal_id: str = "",
                      proposal_hash: str = "",
                      target_content_id: str = "",
                      must_be_approved: bool = True,
                      must_not_be_consumed: bool = True) -> dict[str, Any]:
    """Validate a CTX artifact. Returns parsed CTX dict or raises ValueError."""
    with open(artifact_path) as f:
        ctx = json.load(f)

    errors: list[str] = []

    if ctx.get("repository") != expected_repository:
        errors.append(f"Repository mismatch: {ctx.get('repository')} != {expected_repository}")

    if expected_base_sha and ctx.get("base_sha") != expected_base_sha:
        errors.append("Base SHA mismatch")

    if workflow_run_id:
        wf_id = ctx.get("workflow_run_id")
        if str(wf_id) != str(workflow_run_id):
            errors.append(f"Workflow run ID mismatch: {wf_id} != {workflow_run_id}")

    if proposal_id:
        proposals = ctx.get("engagement_proposals", [])
        found = None
        for p in proposals:
            if isinstance(p, dict) and p.get("proposal_id") == proposal_id:
                found = p
                break
        if found is None:
            errors.append(f"Proposal {proposal_id} not found")
        else:
            if proposal_hash:
                computed = canonical_hash(found)
                if computed != proposal_hash:
                    errors.append(f"Canonical hash mismatch: {computed} != {proposal_hash}")
                if found.get("content_hash") != proposal_hash:
                    errors.append("Stored content_hash differs from canonical hash")
            if must_be_approved and found.get("approval_state") != "approved":
                errors.append("Proposal not approved")
            if must_not_be_consumed and found.get("consumed"):
                errors.append("Proposal already consumed")
            if target_content_id and found.get("target_content_id") != target_content_id:
                errors.append("Target mismatch")

    if errors:
        raise ValueError("; ".join(errors))
    return ctx


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m agency.artifact <command> ...", file=sys.stderr)
        return 1

    cmd = sys.argv[1]

    if cmd == "validate-inputs":
        if len(sys.argv) != 7:
            print("Usage: ... validate-inputs <run_id> <prop_id> <target> <hash> <confirm>", file=sys.stderr)
            return 1
        errors = validate_inputs(workflow_run_id=sys.argv[2], proposal_id=sys.argv[3],
                                 target_content_id=sys.argv[4], proposal_hash=sys.argv[5],
                                 confirm=sys.argv[6])
        for e in errors:
            print(e, file=sys.stderr)
        return 1 if errors else 0

    if cmd == "validate-engagement":
        if len(sys.argv) != 8:
            print("Usage: ... validate-engagement <artifact> <run_id> <sha> <prop_id> <hash> <target>", file=sys.stderr)
            return 1
        try:
            validate_artifact(sys.argv[2], workflow_run_id=sys.argv[3],
                              expected_base_sha=sys.argv[4], proposal_id=sys.argv[5],
                              proposal_hash=sys.argv[6], target_content_id=sys.argv[7])
            return 0
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1

    if cmd == "validate-materialization":
        if len(sys.argv) != 5:
            print("Usage: ... validate-materialization <artifact> <run_id> <sha>", file=sys.stderr)
            return 1
        try:
            validate_artifact(sys.argv[2], workflow_run_id=sys.argv[3],
                              expected_base_sha=sys.argv[4])
            return 0
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
