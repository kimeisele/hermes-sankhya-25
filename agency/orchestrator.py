"""Agency orchestrator — single integration point with dynamic routing.

Constructs a CTX, executes one bounded agency shift using the Director's
disposition to route through phases. Applies role results deterministically.
"""
from __future__ import annotations

import time as _time
from typing import Any

from .context import AgencyContextV1, AgencyBudget, RepoStateProvider
from .events import (BUDGET_EXHAUSTED, ROLE_COMPLETED, ROLE_FAILED,
                     DIRECTOR_DECISION, CAMPAIGN_LOADED,
                     SOURCE_ACCEPTED)
from .models import is_write_critical
from .policy import AgencyPolicy
from .roles import RoleResult, ROLE_REGISTRY

# ---------------------------------------------------------------------------
# Director disposition → next phases
# ---------------------------------------------------------------------------

DIRECTOR_ROUTES: dict[str, list[str]] = {
    "NOOP": ["AUDIT", "CLOSE_BOOKS"],
    "RECORD_ONLY": ["RECORD_OR_PROPOSE", "AUDIT", "CLOSE_BOOKS"],
    "PROPOSE_ENGAGEMENT": ["ENGAGEMENT_LEAD", "AUDIT", "CLOSE_BOOKS"],
    "PROPOSE_ENGINEERING_INTAKE": ["ENGINEERING_PLANNER", "AUDIT", "CLOSE_BOOKS"],
    "READY_FOR_SYNTHESIS": ["RECORD_OR_PROPOSE", "AUDIT", "CLOSE_BOOKS"],
    "ESCALATE_TO_HUMAN": ["AUDIT", "CLOSE_BOOKS"],
}

# Initial phases before Director review
INITIAL_PHASES = [
    "OPEN_OFFICE", "LOAD_AUTHORITY", "SCOUT", "NORMALIZE", "TRIAGE",
    "DIRECTOR_REVIEW",
]


class AgencyOrchestrator:
    """Executes one bounded agency shift with dynamic Director routing."""

    def __init__(self, trigger: str = "manual", shift: str = "morning",
                 repository: str = "kimeisele/hermes-sankhya-25",
                 base_sha: str | None = None,
                 campaign: dict[str, Any] | None = None,
                 policy_config: dict[str, Any] | None = None,
                 budget: AgencyBudget | None = None,
                 repo_provider: RepoStateProvider | None = None) -> None:
        self.policy = AgencyPolicy(policy_config)
        self.budget = budget or AgencyBudget()
        self._repo_provider = repo_provider or RepoStateProvider()

        try:
            self.ctx = AgencyContextV1(
                trigger=trigger, shift=shift,
                repository=repository, base_sha=base_sha,
                campaign=campaign,
                policy=self.policy.to_dict(),
                budget=self.budget,
                repo_provider=self._repo_provider,
            )
        except ValueError:
            # If base_sha can't be resolved, create with a synthetic valid SHA for tests
            if base_sha is None:
                import hashlib
                fake = hashlib.sha1(b"test").hexdigest()
                self.ctx = AgencyContextV1(
                    trigger=trigger, shift=shift,
                    repository=repository, base_sha=fake,
                    campaign=campaign,
                    policy=self.policy.to_dict(),
                    budget=self.budget,
                    repo_provider=self._repo_provider,
                )
            else:
                raise

        self._start_wall = _time.monotonic()

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    def run(self) -> AgencyContextV1:
        """Execute the full agency shift. Returns the closed CTX."""
        try:
            # Initial phases up to Director review
            disposition = self._run_initial_phases()
            if self.ctx.status in ("failed", "budget_exhausted"):
                return self.ctx

            # Dynamic routing based on Director disposition
            if disposition in DIRECTOR_ROUTES:
                for phase in DIRECTOR_ROUTES[disposition]:
                    if self.ctx.status in ("failed", "budget_exhausted"):
                        return self.ctx
                    if self._check_stale():
                        return self.ctx
                    self._execute_phase(phase)
                    if self._check_budget():
                        return self.ctx
            else:
                # Unknown disposition → audit + close
                self.ctx.record_incident(
                    f"Unknown Director disposition: {disposition}",
                    severity="high")
                self._execute_phase("AUDIT")
                self._execute_phase("CLOSE_BOOKS")

            if self.ctx.status not in ("failed", "budget_exhausted", "completed"):
                self.ctx.close("completed")

        except Exception as exc:
            self.ctx.record_incident(f"Orchestrator failure: {exc}",
                                     severity="critical")
            if self.ctx.status not in ("failed", "budget_exhausted"):
                self.ctx.close("failed")
        return self.ctx

    # ------------------------------------------------------------------
    # Initial phases
    # ------------------------------------------------------------------

    def _run_initial_phases(self) -> str:
        """Run phases up to DIRECTOR_REVIEW. Returns disposition."""
        disposition = "NOOP"
        for phase in INITIAL_PHASES:
            if self._check_stale():
                return disposition
            if self._check_budget():
                return disposition

            if phase == "OPEN_OFFICE":
                self.ctx.status = "running"
            elif phase == "LOAD_AUTHORITY":
                self.ctx.append_event(CAMPAIGN_LOADED,
                                      {"campaign": self.ctx.campaign})
            elif phase == "SCOUT":
                self._invoke_and_apply("scout")
            elif phase == "NORMALIZE":
                self._invoke_and_apply("records_clerk")
            elif phase == "TRIAGE":
                self._invoke_and_apply("evidence_analyst")
            elif phase == "DIRECTOR_REVIEW":
                disposition = self._director_review()

            if self.ctx.status in ("failed", "budget_exhausted"):
                return disposition
        return disposition

    # ------------------------------------------------------------------
    # Phase execution
    # ------------------------------------------------------------------

    def _execute_phase(self, phase: str) -> None:
        """Execute a single post-Director phase."""
        self.ctx.append_event("ROLE_STARTED", {"phase": phase})

        if phase == "AUDIT":
            self._invoke_and_apply("auditor")
        elif phase == "CLOSE_BOOKS":
            self.ctx.close("completed")
        elif phase == "RECORD_OR_PROPOSE":
            self._apply_evidence()
        elif phase == "ENGAGEMENT_LEAD":
            self._invoke_and_apply("engagement_lead")
        elif phase == "ENGINEERING_PLANNER":
            self._invoke_and_apply("engineering_planner")
        elif phase == "SYNTHESIS_PROPOSAL":
            pass  # deferred to V2

    # ------------------------------------------------------------------
    # Director review
    # ------------------------------------------------------------------

    def _director_review(self) -> str:
        """Invoke Director, record decision, return disposition."""
        result = self._safe_invoke("agency_director")
        disposition = result.data.get("disposition", "NOOP")

        self.ctx.append_event(DIRECTOR_DECISION, {
            "disposition": disposition,
            "rationale": result.data.get("rationale", ""),
        })
        self.ctx.add_decision({
            "disposition": disposition,
            "timestamp": result.timestamp,
            "rationale": result.data.get("rationale", ""),
        })
        return disposition

    # ------------------------------------------------------------------
    # Role invocation + result application
    # ------------------------------------------------------------------

    def _invoke_and_apply(self, role_name: str) -> RoleResult:
        """Invoke a role and apply its result to the CTX."""
        result = self._safe_invoke(role_name)
        if result.status == "COMPLETE":
            self._apply_result(role_name, result)
        elif result.status in ("FAIL_CLOSED",):
            self._apply_result(role_name, result)
            # fail_closed for Scout/Clerk/Analyst doesn't abort
            if is_write_critical(role_name) and result.status == "FAIL_CLOSED":
                self.ctx.record_incident(
                    f"Write-critical role {role_name} failed closed",
                    severity="high")
                self.ctx.close("failed")
        return result

    def _apply_result(self, role_name: str, result: RoleResult) -> None:
        """Deterministically apply role result to CTX."""
        data = result.data

        if role_name == "scout" and result.status == "COMPLETE":
            candidates = data.get("candidates", [])
            if candidates:
                self.ctx.set_source_candidates(candidates)

        elif role_name == "records_clerk" and result.status == "COMPLETE":
            normalized = data.get("normalized", [])
            if normalized:
                self.ctx.set_source_candidates(normalized)

        elif role_name == "evidence_analyst" and result.status == "COMPLETE":
            analyzed = data.get("accepted", data.get("analyzed", []))
            if analyzed:
                self.ctx.add_accepted_evidence(analyzed)
            self.ctx.append_event(SOURCE_ACCEPTED,
                                  {"count": len(analyzed)})

        elif role_name == "engagement_lead" and result.status == "COMPLETE":
            proposal = data.get("proposal", data)
            if proposal:
                self.ctx.add_engagement_proposal(proposal)

        elif role_name == "engineering_planner" and result.status == "COMPLETE":
            proposal = data.get("proposal", data)
            if proposal:
                self.ctx.add_engineering_proposal(proposal)

        elif role_name == "bridge_executor" and result.status == "COMPLETE":
            txn = data.get("transaction", data)
            if txn:
                self.ctx.add_transaction(txn)

        elif role_name == "auditor":
            self.ctx.set_audit({
                "findings": data.get("findings", []),
                "passed": data.get("passed", False),
                "timestamp": result.timestamp,
            })

    def _apply_evidence(self) -> None:
        """Record-only: accept all normalized candidates as evidence."""
        candidates = self.ctx.source_candidates
        if candidates:
            self.ctx.add_accepted_evidence(candidates)
            self.ctx.append_event(SOURCE_ACCEPTED,
                                  {"count": len(candidates)})

    # ------------------------------------------------------------------
    # Safe invocation with budget enforcement
    # ------------------------------------------------------------------

    def _safe_invoke(self, role_name: str) -> RoleResult:
        """Invoke with budget pre-check, error handling, delegation."""
        role_fn = ROLE_REGISTRY.get(role_name)
        if role_fn is None:
            result = RoleResult(role_name, "FAIL_CLOSED",
                                fail_reason=f"Unknown role: {role_name}")
            self.ctx.append_event(ROLE_FAILED, result.to_dict())
            return result

        # Budget pre-check
        estimated_tokens = 1000
        estimated_cost = 0.01
        if not self.budget.reserve(estimated_tokens, estimated_cost):
            self.ctx.record_incident(
                f"Budget exhausted before {role_name} call",
                severity="medium")
            self.ctx.append_event(BUDGET_EXHAUSTED, self.budget.to_dict())
            self.ctx.close("budget_exhausted")
            return RoleResult(role_name, "FAIL_CLOSED",
                              fail_reason="Budget exhausted")

        ctx_view = self.ctx.view_for(role_name)

        try:
            result = role_fn(ctx_view)
        except Exception as exc:
            result = RoleResult(role_name, "FAIL_CLOSED",
                                fail_reason=str(exc))
            self.ctx.append_event(ROLE_FAILED, result.to_dict())
            return result

        # Reconcile budget
        self.budget.reconcile(estimated_tokens, result.token_estimate,
                              estimated_cost, result.cost_estimate)
        self.ctx.append_event(ROLE_COMPLETED, result.to_dict())

        # Handle delegation
        if result.status == "DELEGATE" and result.delegate_to:
            if self.budget.record_delegation():
                return self._safe_invoke(result.delegate_to)
            else:
                self.ctx.record_incident(
                    "Delegation limit exceeded", severity="medium")
                return RoleResult(role_name, "FAIL_CLOSED",
                                  fail_reason="Delegation limit exceeded")

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _check_budget(self) -> bool:
        """Check if budget is exhausted. Returns True if should stop."""
        if self.budget.is_exhausted:
            self.ctx.append_event(BUDGET_EXHAUSTED, self.budget.to_dict())
            self.ctx.record_incident("Budget exhausted during run",
                                     severity="medium")
            self.ctx.close("budget_exhausted")
            return True
        elapsed = _time.monotonic() - self._start_wall
        if elapsed > self.budget.max_duration_seconds:
            self.ctx.record_incident(
                f"Run exceeded max duration ({elapsed:.0f}s)",
                severity="medium")
            self.ctx.close("failed")
            return True
        return False

    def _check_stale(self) -> bool:
        """Check if repository has advanced. Returns True if should stop."""
        if self.ctx.is_stale():
            self.ctx.record_incident(
                "Run aborted: repository state changed during run",
                severity="high")
            self.ctx.close("failed")
            return True
        return False
