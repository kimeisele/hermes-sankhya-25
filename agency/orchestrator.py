"""Agency orchestrator — the single integration point.

Constructs a CTX, executes one bounded agency shift, calls roles in
sequence according to the state machine, and closes deterministically.
"""
from __future__ import annotations

import time as _time
from typing import Any

from .context import AgencyContextV1, AgencyBudget
from .events import (BUDGET_EXHAUSTED, ROLE_COMPLETED, ROLE_FAILED, DIRECTOR_DECISION)
from .models import model_for_role
from .policy import AgencyPolicy
from .roles import RoleResult, ROLE_REGISTRY, VALID_STATUSES


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# Linear shift flow
SHIFT_FLOW = [
    "OPEN_OFFICE",
    "LOAD_AUTHORITY",
    "MORNING_OR_EVENING_BRIEF",
    "SCOUT",
    "NORMALIZE",
    "TRIAGE",
    "DIRECTOR_REVIEW",
    "BUILD_WORK_QUEUE",
    "RECORD_OR_PROPOSE",
    "AUDIT",
    "CLOSE_BOOKS",
]

# Director dispositions → next phase
DIRECTOR_DISPOSITIONS = {
    "NOOP": "AUDIT",
    "RECORD_ONLY": "RECORD_OR_PROPOSE",
    "PROPOSE_ENGAGEMENT": "BUILD_WORK_QUEUE",
    "PROPOSE_ENGINEERING_INTAKE": "BUILD_WORK_QUEUE",
    "SYNTHESIS_QUEUE": "BUILD_WORK_QUEUE",
    "HUMAN_ESCALATION": "AUDIT",
}


class AgencyOrchestrator:
    """Executes one bounded agency shift."""

    def __init__(self, trigger: str = "manual", shift: str = "morning",
                 repository: str = "kimeisele/hermes-sankhya-25",
                 base_sha: str = "",
                 campaign: dict[str, Any] | None = None,
                 policy_config: dict[str, Any] | None = None,
                 budget: AgencyBudget | None = None) -> None:
        self.policy = AgencyPolicy(policy_config)
        self.budget = budget or AgencyBudget()
        self.ctx = AgencyContextV1(
            trigger=trigger, shift=shift,
            repository=repository, base_sha=base_sha,
            campaign=campaign,
            policy=self.policy.to_dict(),
            budget=self.budget,
        )
        self._phase = "OPEN_OFFICE"
        self._start_wall = _time.monotonic()

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    def run(self) -> AgencyContextV1:
        """Execute the full agency shift. Returns the closed CTX."""
        try:
            for phase in SHIFT_FLOW:
                if self._is_stale():
                    self.ctx.record_incident(
                        "Run aborted: repository state changed during run",
                        severity="high")
                    self.ctx.close("failed")
                    return self.ctx

                self._phase = phase
                self.ctx.append_event("ROLE_STARTED",
                                      {"phase": phase})

                if phase == "OPEN_OFFICE":
                    self._open_office()
                elif phase == "LOAD_AUTHORITY":
                    self._load_authority()
                elif phase == "MORNING_OR_EVENING_BRIEF":
                    self._brief()
                elif phase == "SCOUT":
                    self._invoke_role("scout")
                elif phase == "NORMALIZE":
                    self._invoke_role("records_clerk")
                elif phase == "TRIAGE":
                    self._invoke_role("evidence_analyst")
                elif phase == "DIRECTOR_REVIEW":
                    self._director_review()
                elif phase == "BUILD_WORK_QUEUE":
                    self._build_work_queue()
                elif phase == "RECORD_OR_PROPOSE":
                    self._record_or_propose()
                elif phase == "AUDIT":
                    self._invoke_role("auditor")
                elif phase == "CLOSE_BOOKS":
                    self._close_books()
                    break

                # Check budget after each phase
                if self.budget.is_exhausted:
                    self.ctx.append_event(BUDGET_EXHAUSTED,
                                          self.budget.to_dict())
                    self.ctx.record_incident("Budget exhausted during run",
                                             severity="medium")
                    self.ctx.close("budget_exhausted")
                    return self.ctx

                # Check wall-clock
                elapsed = _time.monotonic() - self._start_wall
                if elapsed > self.budget.max_duration_seconds:
                    self.ctx.record_incident(
                        f"Run exceeded max duration ({elapsed:.0f}s)",
                        severity="medium")
                    self.ctx.close("failed")
                    return self.ctx

            self.ctx.close("completed")
        except Exception as exc:
            self.ctx.record_incident(f"Orchestrator failure: {exc}",
                                     severity="critical")
            self.ctx.close("failed")
        return self.ctx

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    def _open_office(self) -> None:
        """Initialize the run — verify base SHA, set up context."""
        self.ctx.status = "running"

    def _load_authority(self) -> None:
        """Load campaign state and policy. Already done in __init__."""
        self.ctx.append_event("CAMPAIGN_LOADED",
                              {"campaign": self.ctx.campaign})

    def _brief(self) -> None:
        """Morning/evening brief — determine shift context."""
        pass  # V1: brief is a no-op placeholder

    def _director_review(self) -> None:
        """Invoke Agency Director (Pro), record decision."""
        result = self._safe_invoke("agency_director")
        if result.status == "FAIL_CLOSED":
            self.ctx.close("failed")
            return

        disposition = result.data.get("disposition", "NOOP")
        self.ctx.append_event(DIRECTOR_DECISION, {
            "disposition": disposition,
            "next_role": result.delegate_to,
            "rationale": result.data.get("rationale", ""),
        })
        self.ctx.decisions.append({
            "disposition": disposition,
            "timestamp": result.timestamp,
        })

    def _build_work_queue(self) -> None:
        """Build work queue from Director decision."""
        # V1: queue is implicit in engagement/engineering proposals
        pass

    def _record_or_propose(self) -> None:
        """Record evidence or propose engagement/engineering."""
        if self.ctx.engagement_proposals:
            self._invoke_role("engagement_lead")
        elif self.ctx.engineering_proposals:
            self._invoke_role("engineering_planner")

    def _close_books(self) -> None:
        """Final audit and close."""
        self.ctx.close("completed")

    # ------------------------------------------------------------------
    # Role invocation
    # ------------------------------------------------------------------

    def _invoke_role(self, role_name: str) -> RoleResult:
        """Invoke a role, record result, update budget."""
        return self._safe_invoke(role_name)

    def _safe_invoke(self, role_name: str) -> RoleResult:
        """Invoke with error handling and budget tracking."""
        role_fn = ROLE_REGISTRY.get(role_name)
        if role_fn is None:
            result = RoleResult(role_name, "FAIL_CLOSED",
                                fail_reason=f"Unknown role: {role_name}")
            self.ctx.append_event(ROLE_FAILED, result.to_dict())
            return result

        ctx_view = self.ctx.view_for(role_name)
        _model = model_for_role(role_name)  # routed but not used in dry-run

        try:
            result = role_fn(ctx_view)
        except Exception as exc:
            result = RoleResult(role_name, "FAIL_CLOSED",
                                fail_reason=str(exc))
            self.ctx.append_event(ROLE_FAILED, result.to_dict())
            self.budget.record_role_call()
            return result

        # Validate result
        if result.status not in VALID_STATUSES:
            result = RoleResult(role_name, "FAIL_CLOSED",
                                fail_reason=f"Invalid status: {result.status}")
            self.ctx.append_event(ROLE_FAILED, result.to_dict())
            self.budget.record_role_call()
            return result

        self.ctx.append_event(ROLE_COMPLETED, result.to_dict())
        self.budget.record_role_call(
            tokens=result.token_estimate,
            cost=result.cost_estimate)

        # Handle delegation
        if result.status == "DELEGATE" and result.delegate_to:
            if self.budget.delegation_rounds_used < self.budget.max_delegation_rounds:
                self.budget.record_delegation()
                return self._safe_invoke(result.delegate_to)
            else:
                self.ctx.record_incident(
                    "Delegation limit exceeded", severity="medium")

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_stale(self) -> bool:
        """Check if repository state has changed since run start."""
        # V1: stub — always returns False in dry-run
        return False
