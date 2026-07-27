"""Agency role implementations with model adapter integration.

Each role receives a deep-copied CTX view and returns a schema-validated
RoleResult. Roles never mutate the CTX directly — the orchestrator
applies results deterministically.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
from pathlib import Path
from typing import Any

from .model_client import RoleModelAdapter, ModelCallResult

# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((_SCHEMA_DIR / name).read_text())


_ROLE_RESULT_SCHEMA = _load_schema("agency-role-result-v1.schema.json")
_DECISION_SCHEMA = _load_schema("agency-decision-v1.schema.json")
_PROFILE_SCHEMA = _load_schema("agent-profile-v1.schema.json")
_PROPOSAL_SCHEMA = _load_schema("engineering-proposal-v1.schema.json")


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
        if token_estimate < 0:
            raise ValueError("token_estimate cannot be negative")
        if cost_estimate < 0:
            raise ValueError("cost_estimate cannot be negative")
        if status == "DELEGATE" and (not delegate_to or not delegate_reason):
            raise ValueError("DELEGATE requires delegate_to and delegate_reason")
        if status == "ESCALATE" and not escalation_reason:
            raise ValueError("ESCALATE requires escalation_reason")
        if status == "FAIL_CLOSED" and not fail_reason:
            raise ValueError("FAIL_CLOSED requires fail_reason")
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
            "data": copy.deepcopy(self.data),
            "delegate_to": self.delegate_to,
            "delegate_reason": self.delegate_reason,
            "escalation_reason": self.escalation_reason,
            "fail_reason": self.fail_reason,
            "provenance": list(self.provenance),
            "token_estimate": self.token_estimate,
            "cost_estimate": self.cost_estimate,
        }


def _safe_role_result(role: str, result: ModelCallResult,
                      is_write_critical: bool = False) -> RoleResult:
    """Convert a ModelCallResult into a RoleResult."""
    if result.success:
        return RoleResult(role, "COMPLETE", data=result.data,
                          token_estimate=result.total_tokens,
                          cost_estimate=result.estimated_cost)
    if is_write_critical or result.error_kind in ("missing_key", "transport", "budget"):
        return RoleResult(role, "FAIL_CLOSED",
                          fail_reason=f"Model error: {result.error}")
    # Read-only Flash role: could be NOOP on empty
    if result.error_kind == "empty":
        return RoleResult(role, "NOOP", data={"note": "No candidates found"})
    return RoleResult(role, "FAIL_CLOSED",
                      fail_reason=f"Model error ({result.error_kind}): {result.error}")


# ---------------------------------------------------------------------------
# Role registry
# ---------------------------------------------------------------------------

ROLE_REGISTRY: dict[str, Any] = {}


def register_role(name: str, instance: Any) -> None:
    ROLE_REGISTRY[name] = instance


# ---------------------------------------------------------------------------
# Scout — DeepSeek Flash
# ---------------------------------------------------------------------------

class ScoutRole:
    """Discovers and deduplicates candidate sources. Uses Flash when
    live-read analysis is enabled. Produces candidate references."""
    ROLE = "scout"

    def __init__(self, adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        inbox = ctx_view.get("inbox", [])
        existing_ids = set(ctx_view.get("accepted_evidence_ids", []))
        if not inbox:
            return RoleResult(self.ROLE, "NOOP",
                              data={"candidates_found": 0})

        # Deduplicate
        new_candidates = []
        for item in inbox:
            if item.get("id") not in existing_ids:
                new_candidates.append(item)

        if not new_candidates:
            return RoleResult(self.ROLE, "NOOP",
                              data={"candidates_found": 0,
                                    "note": "All inbox items already in evidence"})

        if self._adapter:
            result = self._adapter.invoke({
                **ctx_view, "new_candidates": new_candidates,
            })
            return _safe_role_result(self.ROLE, result)

        return RoleResult(self.ROLE, "COMPLETE",
                          data={"candidates_found": len(new_candidates),
                                "candidates": new_candidates},
                          provenance=[c.get("url", "") for c in new_candidates])


# ---------------------------------------------------------------------------
# Records Clerk — DeepSeek Flash
# ---------------------------------------------------------------------------

class RecordsClerkRole:
    """Normalizes metadata, preserves provenance, marks untrusted."""
    ROLE = "records_clerk"

    def __init__(self, adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        candidates = ctx_view.get("source_candidates", [])
        if not candidates:
            return RoleResult(self.ROLE, "NOOP")

        if self._adapter:
            result = self._adapter.invoke({
                **ctx_view, "candidates_for_normalization": candidates,
            })
            return _safe_role_result(self.ROLE, result)

        normalized = []
        for c in candidates:
            normalized.append({
                "url": c.get("url", ""),
                "author_handle": c.get("author_handle", "unknown"),
                "content_type": c.get("content_type", "unknown"),
                "untrusted": True,
                "observed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "paraphrase": c.get("paraphrase", ""),
                "provenance": [c.get("url", "")],
            })
        return RoleResult(self.ROLE, "COMPLETE",
                          data={"normalized": normalized},
                          provenance=[c.get("url", "") for c in candidates])


# ---------------------------------------------------------------------------
# Evidence Analyst — DeepSeek Flash
# ---------------------------------------------------------------------------

class EvidenceAnalystRole:
    """Extracts claims, classifies evidence, scores dimensions."""
    ROLE = "evidence_analyst"

    def __init__(self, adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        evidence = ctx_view.get("accepted_evidence", [])
        if not evidence:
            return RoleResult(self.ROLE, "NOOP")

        if self._adapter:
            result = self._adapter.invoke(ctx_view)
            return _safe_role_result(self.ROLE, result)

        return RoleResult(self.ROLE, "COMPLETE",
                          data={"analyzed_count": len(evidence)})


# ---------------------------------------------------------------------------
# Agency Director — DeepSeek Pro (write-critical)
# ---------------------------------------------------------------------------

class AgencyDirectorRole:
    """Strategic routing decisions. Uses Pro. Write-critical."""
    ROLE = "agency_director"

    VALID_DISPOSITIONS = frozenset({
        "NOOP", "RECORD_ONLY", "PROPOSE_ENGAGEMENT",
        "PROPOSE_ENGINEERING_INTAKE", "READY_FOR_SYNTHESIS",
        "ESCALATE_TO_HUMAN",
    })

    def __init__(self, adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        budget = ctx_view.get("budget", {})
        if budget.get("role_calls_used", 0) >= budget.get("max_role_calls", 20):
            return RoleResult(self.ROLE, "FAIL_CLOSED",
                              fail_reason="Budget exhausted")

        evidence = ctx_view.get("accepted_evidence", [])
        proposals = ctx_view.get("engagement_proposals", [])

        if self._adapter:
            result = self._adapter.invoke(ctx_view)
            if not result.success:
                return RoleResult(self.ROLE, "FAIL_CLOSED",
                                  fail_reason=f"Director model failed: {result.error}")
            disposition = result.data.get("disposition", "NOOP")
            if disposition not in self.VALID_DISPOSITIONS:
                return RoleResult(self.ROLE, "FAIL_CLOSED",
                                  fail_reason=f"Invalid disposition: {disposition}")
            return RoleResult(self.ROLE, "COMPLETE",
                              data=result.data,
                              token_estimate=result.total_tokens,
                              cost_estimate=result.estimated_cost,
                              provenance=result.data.get("provenance", []))

        # Deterministic fallback
        if not evidence and not proposals:
            return RoleResult(self.ROLE, "NOOP",
                              data={"disposition": "NOOP"})
        if evidence and not proposals:
            return RoleResult(self.ROLE, "COMPLETE",
                              data={"disposition": "RECORD_ONLY"})
        if proposals:
            return RoleResult(self.ROLE, "COMPLETE",
                              data={"disposition": "PROPOSE_ENGAGEMENT"})
        return RoleResult(self.ROLE, "NOOP")


# ---------------------------------------------------------------------------
# Engagement Lead — DeepSeek Pro (write-critical)
# ---------------------------------------------------------------------------

class EngagementLeadRole:
    """Drafts engagement proposals. Uses Pro. Write-critical."""
    ROLE = "engagement_lead"

    def __init__(self, adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        proposals = ctx_view.get("engagement_proposals", [])
        if not proposals:
            return RoleResult(self.ROLE, "NOOP")
        if self._adapter:
            result = self._adapter.invoke(ctx_view)
            if not result.success:
                return RoleResult(self.ROLE, "FAIL_CLOSED",
                                  fail_reason=f"Engagement model failed: {result.error}")
            return RoleResult(self.ROLE, "COMPLETE", data=result.data,
                              token_estimate=result.total_tokens,
                              cost_estimate=result.estimated_cost)
        return RoleResult(self.ROLE, "COMPLETE",
                          data={"proposal_count": len(proposals)})


# ---------------------------------------------------------------------------
# Bridge Executor — deterministic, no model discretion
# ---------------------------------------------------------------------------

class BridgeExecutorRole:
    """Deterministic wrapper around scripts/moltbook_write.py."""
    ROLE = "bridge_executor"

    def __init__(self, subprocess_runner: Any = None) -> None:
        self._runner = subprocess_runner

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        return RoleResult(self.ROLE, "NOOP",
                          data={"dry_run": True,
                                "note": "Bridge not invoked in dry-run mode"})


# ---------------------------------------------------------------------------
# Auditor — DeepSeek Flash (Pro escalation)
# ---------------------------------------------------------------------------

class AuditorRole:
    """Checks policy, receipts, budgets, duplicate writes, contradictions."""
    ROLE = "auditor"

    def __init__(self, adapter: RoleModelAdapter | None = None,
                 pro_adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter
        self._pro_adapter = pro_adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        budget = ctx_view.get("budget", {})
        findings: list[str] = []

        if budget.get("role_calls_used", 0) >= budget.get("max_role_calls", 20):
            findings.append("budget_role_calls_exhausted")
        if budget.get("cost_estimate_used", 0) >= budget.get("max_cost_estimate", 5.0):
            findings.append("budget_cost_exhausted")

        transactions = ctx_view.get("transactions", [])
        write_count = len(transactions)
        policy = ctx_view.get("policy", {})
        max_writes = policy.get("max_writes_per_run", 1)
        if write_count > max_writes:
            findings.append(f"write_count_exceeded: {write_count} > {max_writes}")

        passed = len(findings) == 0

        # Escalate to Pro for ambiguous/high-impact contradictions
        if findings and self._pro_adapter:
            escalated = self._pro_adapter.invoke({
                **ctx_view, "findings": findings,
            })
            if escalated.success:
                return RoleResult(self.ROLE, "COMPLETE",
                                  data={"findings": findings,
                                        "passed": False,
                                        "escalation": escalated.data},
                                  provenance=escalated.data.get("provenance", []))

        return RoleResult(self.ROLE, "COMPLETE",
                          data={"findings": findings, "passed": passed})


# ---------------------------------------------------------------------------
# Engineering Planner — DeepSeek Pro (write-critical)
# ---------------------------------------------------------------------------

class EngineeringPlannerRole:
    """Converts external intelligence to engineering proposals. Uses Pro."""
    ROLE = "engineering_planner"

    def __init__(self, adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        evidence = ctx_view.get("accepted_evidence", [])
        if not evidence:
            return RoleResult(self.ROLE, "NOOP")
        if self._adapter:
            result = self._adapter.invoke(ctx_view)
            if not result.success:
                return RoleResult(self.ROLE, "FAIL_CLOSED",
                                  fail_reason=f"Planner model failed: {result.error}")
            return RoleResult(self.ROLE, "COMPLETE", data=result.data,
                              token_estimate=result.total_tokens,
                              cost_estimate=result.estimated_cost)
        return RoleResult(self.ROLE, "COMPLETE",
                          data={"proposals_created": 0})


# ---------------------------------------------------------------------------
# Register default instances
# ---------------------------------------------------------------------------

register_role("scout", ScoutRole())
register_role("records_clerk", RecordsClerkRole())
register_role("evidence_analyst", EvidenceAnalystRole())
register_role("agency_director", AgencyDirectorRole())
register_role("engagement_lead", EngagementLeadRole())
register_role("bridge_executor", BridgeExecutorRole())
register_role("auditor", AuditorRole())
register_role("engineering_planner", EngineeringPlannerRole())
