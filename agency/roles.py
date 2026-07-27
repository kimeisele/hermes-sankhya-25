"""Agency role implementations.

Each role receives a filtered CTX view, produces a schema-validated
RoleResult, and returns it to the orchestrator. Roles never mutate
the CTX directly.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

# ---------------------------------------------------------------------------
# Role result validation
# ---------------------------------------------------------------------------

VALID_STATUSES = frozenset({
    "COMPLETE", "NOOP", "DELEGATE", "NEED_CONTEXT",
    "ESCALATE", "FAIL_CLOSED",
})


class RoleResult:
    """Schema-validated result from a single role invocation."""

    def __init__(self, role: str, status: str,
                 data: dict[str, Any] | None = None,
                 delegate_to: str = "",
                 delegate_reason: str = "",
                 escalation_reason: str = "",
                 fail_reason: str = "",
                 provenance: list[str] | None = None,
                 token_estimate: int = 0,
                 cost_estimate: float = 0.0) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid role status: {status}")
        self.role = role
        self.status = status
        self.timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.data = data or {}
        self.delegate_to = delegate_to
        self.delegate_reason = delegate_reason
        self.escalation_reason = escalation_reason
        self.fail_reason = fail_reason
        self.provenance = provenance or []
        self.token_estimate = token_estimate
        self.cost_estimate = cost_estimate

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "status": self.status,
            "timestamp": self.timestamp,
            "data": self.data,
            "delegate_to": self.delegate_to,
            "delegate_reason": self.delegate_reason,
            "escalation_reason": self.escalation_reason,
            "fail_reason": self.fail_reason,
            "provenance": self.provenance,
            "token_estimate": self.token_estimate,
            "cost_estimate": self.cost_estimate,
        }


# ---------------------------------------------------------------------------
# Role implementations
# ---------------------------------------------------------------------------

class ScoutRole:
    """DeepSeek Flash — discovers and deduplicates candidate sources."""

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        """Stub: returns NOOP when no inbox items exist."""
        inbox = ctx_view.get("inbox", [])
        if not inbox:
            return RoleResult("scout", "NOOP",
                              data={"candidates_found": 0})
        return RoleResult("scout", "COMPLETE",
                          data={"candidates_found": len(inbox),
                                "candidates": inbox})


class RecordsClerkRole:
    """DeepSeek Flash — normalizes metadata and marks untrusted."""

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        candidates = ctx_view.get("source_candidates", [])
        if not candidates:
            return RoleResult("records_clerk", "NOOP")
        normalized = []
        for c in candidates:
            normalized.append({
                "url": c.get("url", ""),
                "author_handle": c.get("author_handle", "unknown"),
                "content_type": c.get("content_type", "unknown"),
                "untrusted": True,
                "observed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            })
        return RoleResult("records_clerk", "COMPLETE",
                          data={"normalized": normalized})


class EvidenceAnalystRole:
    """DeepSeek Flash — extracts claims, classifies evidence, scores."""

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        evidence = ctx_view.get("accepted_evidence", [])
        if not evidence:
            return RoleResult("evidence_analyst", "NOOP")
        return RoleResult("evidence_analyst", "COMPLETE",
                          data={"analyzed_count": len(evidence)})


class AgencyDirectorRole:
    """DeepSeek Pro — makes routing decisions."""

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        evidence = ctx_view.get("accepted_evidence", [])
        budget = ctx_view.get("budget", {})
        proposals = ctx_view.get("engagement_proposals", [])

        # Budget check
        calls_used = budget.get("role_calls_used", 0)
        max_calls = budget.get("max_role_calls", 20)
        if calls_used >= max_calls:
            return RoleResult("agency_director", "FAIL_CLOSED",
                              fail_reason="Budget exhausted")

        # No new evidence → NOOP
        if not evidence and not proposals:
            return RoleResult("agency_director", "NOOP",
                              data={"disposition": "NOOP"})

        # Has evidence but no proposals → RECORD_ONLY
        if evidence and not proposals:
            return RoleResult("agency_director", "COMPLETE",
                              data={"disposition": "RECORD_ONLY"})

        # Has engagement proposals → PROPOSE_ENGAGEMENT
        if proposals:
            return RoleResult("agency_director", "COMPLETE",
                              data={"disposition": "PROPOSE_ENGAGEMENT"})

        return RoleResult("agency_director", "NOOP")


class EngagementLeadRole:
    """DeepSeek Pro — drafts engagement proposals. Not the transport executor."""

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        proposals = ctx_view.get("engagement_proposals", [])
        if not proposals:
            return RoleResult("engagement_lead", "NOOP")
        return RoleResult("engagement_lead", "COMPLETE",
                          data={"proposal_count": len(proposals)})


class BridgeExecutorRole:
    """Deterministic — invokes scripts/moltbook_write.py. No model discretion."""

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        # In V1 dry-run, bridge is never invoked with real credentials
        return RoleResult("bridge_executor", "NOOP",
                          data={"dry_run": True,
                                "note": "Bridge not invoked in dry-run mode"})


class AuditorRole:
    """DeepSeek Flash (default) — checks policy, receipts, budgets."""

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        findings = []
        budget = ctx_view.get("budget", {})
        if budget.get("role_calls_used", 0) >= budget.get("max_role_calls", 20):
            findings.append("budget_role_calls_exhausted")
        if budget.get("cost_estimate_used", 0) >= budget.get("max_cost_estimate", 5.0):
            findings.append("budget_cost_exhausted")
        return RoleResult("auditor", "COMPLETE",
                          data={"findings": findings,
                                "passed": len(findings) == 0})


class EngineeringPlannerRole:
    """DeepSeek Pro — converts external intelligence to engineering proposals."""

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        evidence = ctx_view.get("accepted_evidence", [])
        if not evidence:
            return RoleResult("engineering_planner", "NOOP")
        return RoleResult("engineering_planner", "COMPLETE",
                          data={"proposals_created": 0,
                                "note": "V1: proposals require explicit external input"})


# ---------------------------------------------------------------------------
# Role registry
# ---------------------------------------------------------------------------

ROLE_REGISTRY: dict[str, Any] = {
    "scout": ScoutRole(),
    "records_clerk": RecordsClerkRole(),
    "evidence_analyst": EvidenceAnalystRole(),
    "agency_director": AgencyDirectorRole(),
    "engagement_lead": EngagementLeadRole(),
    "bridge_executor": BridgeExecutorRole(),
    "auditor": AuditorRole(),
    "engineering_planner": EngineeringPlannerRole(),
}
