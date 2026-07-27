"""Agency orchestrator — per-run role factory, FAIL_CLOSED termination,
dynamic Director routing, and evidence lifecycle.
"""
from __future__ import annotations

import time as _time
from typing import Any

from .context import AgencyContextV1, AgencyBudget, RepoStateProvider
from .events import (BUDGET_EXHAUSTED, ROLE_COMPLETED, ROLE_FAILED,
                     DIRECTOR_DECISION, CAMPAIGN_LOADED, SOURCE_ACCEPTED)
from .policy import AgencyPolicy
from .roles import (RoleResult, ScoutRole, RecordsClerkRole, EvidenceAnalystRole,
                    AgencyDirectorRole, EngagementLeadRole, BridgeExecutorRole,
                    AuditorRole, EngineeringPlannerRole)
from .model_client import DeepSeekClient, RoleModelAdapter

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

INITIAL_PHASES = [
    "OPEN_OFFICE", "LOAD_AUTHORITY", "SCOUT", "NORMALIZE", "TRIAGE",
    "DIRECTOR_REVIEW",
]


# ---------------------------------------------------------------------------
# Per-run role factory
# ---------------------------------------------------------------------------

def build_role_registry(client: DeepSeekClient | None = None,
                        moltbook_reader: Any = None,
                        subprocess_runner: Any = None,
                        flash_system: str = "",
                        pro_system: str = "") -> dict[str, Any]:
    """Build a fresh role registry for one run. Never returns a mutable global."""
    registry: dict[str, Any] = {}

    if client:
        from pathlib import Path
        import json
        sd = Path(__file__).resolve().parents[1] / "schemas"
        role_schema = json.loads((sd / "agency-role-result-v1.schema.json").read_text())
        decision_schema = json.loads((sd / "agency-decision-v1.schema.json").read_text())
        proposal_schema = json.loads((sd / "engineering-proposal-v1.schema.json").read_text())

        flash_adapter = RoleModelAdapter(client, client.flash_model,
                                         flash_system or "You are a read-only intelligence analyst.",
                                         role_schema, is_write_critical=False)
        pro_adapter = RoleModelAdapter(client, client.pro_model,
                                       pro_system or "You are a strategic agency director.",
                                       decision_schema, is_write_critical=True)
        engagement_adapter = RoleModelAdapter(client, client.pro_model,
                                              pro_system or "You draft engagement proposals.",
                                              role_schema, is_write_critical=True)
        planner_adapter = RoleModelAdapter(client, client.pro_model,
                                           pro_system or "You create engineering proposals.",
                                           proposal_schema, is_write_critical=True)

        registry["scout"] = ScoutRole(adapter=flash_adapter)
        registry["records_clerk"] = RecordsClerkRole(adapter=flash_adapter)
        registry["evidence_analyst"] = EvidenceAnalystRole(adapter=flash_adapter)
        registry["agency_director"] = AgencyDirectorRole(adapter=pro_adapter)
        registry["engagement_lead"] = EngagementLeadRole(adapter=engagement_adapter)
        registry["auditor"] = AuditorRole(adapter=flash_adapter, pro_adapter=pro_adapter)
        registry["engineering_planner"] = EngineeringPlannerRole(adapter=planner_adapter)
    else:
        registry["scout"] = ScoutRole()
        registry["records_clerk"] = RecordsClerkRole()
        registry["evidence_analyst"] = EvidenceAnalystRole()
        registry["agency_director"] = AgencyDirectorRole()
        registry["engagement_lead"] = EngagementLeadRole()
        registry["auditor"] = AuditorRole()
        registry["engineering_planner"] = EngineeringPlannerRole()

    registry["bridge_executor"] = BridgeExecutorRole(subprocess_runner=subprocess_runner)
    return registry


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class AgencyOrchestrator:
    def __init__(self, trigger: str = "manual", shift: str = "morning",
                 repository: str = "kimeisele/hermes-sankhya-25",
                 base_sha: str | None = None,
                 campaign: dict[str, Any] | None = None,
                 policy_config: dict[str, Any] | None = None,
                 budget: AgencyBudget | None = None,
                 repo_provider: RepoStateProvider | None = None,
                 role_registry: dict[str, Any] | None = None,
                 moltbook_reader: Any = None) -> None:
        self.policy = AgencyPolicy(policy_config)
        self.budget = budget or AgencyBudget()
        self._repo_provider = repo_provider or RepoStateProvider()
        self._role_registry = role_registry or build_role_registry()
        self._moltbook_reader = moltbook_reader

        sha = base_sha
        if sha is None:
            sha = self._repo_provider.current_sha()
            if not sha or len(sha) != 40:
                raise ValueError(f"Cannot resolve valid base SHA: got '{sha}'")

        self.ctx = AgencyContextV1(
            trigger=trigger, shift=shift, repository=repository, base_sha=sha,
            campaign=campaign, policy=self.policy.to_dict(), budget=self.budget,
            repo_provider=self._repo_provider)
        self._start_wall = _time.monotonic()

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    def run(self) -> AgencyContextV1:
        try:
            disposition = self._run_initial_phases()
            if self.ctx.status in ("failed", "budget_exhausted"):
                return self.ctx

            if disposition not in DIRECTOR_ROUTES:
                self.ctx.record_incident(
                    f"Unknown Director disposition: {disposition}", severity="high")
                self.ctx.close("failed")
                return self.ctx

            for phase in DIRECTOR_ROUTES[disposition]:
                if self.ctx.status in ("failed", "budget_exhausted"):
                    return self.ctx
                if self._check_stale():
                    return self.ctx
                self._execute_phase(phase)
                if self._check_budget():
                    return self.ctx

            if self.ctx.status not in ("failed", "budget_exhausted", "completed"):
                self.ctx.close("completed")
        except Exception as exc:
            self.ctx.record_incident(f"Orchestrator failure: {exc}", severity="critical")
            if self.ctx.status not in ("failed", "budget_exhausted"):
                self.ctx.close("failed")
        return self.ctx

    # ------------------------------------------------------------------
    # Initial phases
    # ------------------------------------------------------------------

    def _run_initial_phases(self) -> str:
        disposition = "NOOP"
        for phase in INITIAL_PHASES:
            if self._check_stale():
                return disposition
            if self._check_budget():
                return disposition

            if phase == "OPEN_OFFICE":
                self.ctx.status = "running"
            elif phase == "LOAD_AUTHORITY":
                self.ctx.append_event(CAMPAIGN_LOADED, {"campaign": self.ctx.campaign})
            elif phase == "SCOUT":
                self._invoke_and_apply("scout")
            elif phase == "NORMALIZE":
                self._invoke_and_apply("records_clerk")
            elif phase == "TRIAGE":
                # Evidence Analyst receives normalized candidates → produces accepted/rejected
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
        self.ctx.append_event("ROLE_STARTED", {"phase": phase})
        if phase == "AUDIT":
            self._invoke_and_apply("auditor")
        elif phase == "CLOSE_BOOKS":
            self.ctx.close("completed")
        elif phase == "RECORD_OR_PROPOSE":
            pass  # evidence already accepted via Evidence Analyst
        elif phase == "ENGAGEMENT_LEAD":
            self._invoke_and_apply("engagement_lead")
        elif phase == "ENGINEERING_PLANNER":
            self._invoke_and_apply("engineering_planner")

    # ------------------------------------------------------------------
    # Director review
    # ------------------------------------------------------------------

    def _director_review(self) -> str:
        result = self._safe_invoke("agency_director")
        if result.status == "FAIL_CLOSED":
            self.ctx.close("failed")
            return "NOOP"

        disposition = result.data.get("disposition", "")
        if not disposition or disposition not in DIRECTOR_ROUTES:
            self.ctx.record_incident(
                f"Invalid Director disposition: '{disposition}'", severity="high")
            self.ctx.close("failed")
            return "NOOP"

        self.ctx.append_event(DIRECTOR_DECISION, {
            "disposition": disposition,
            "rationale": result.data.get("rationale", ""),
        })
        self.ctx.add_decision({
            "disposition": disposition, "timestamp": result.timestamp,
            "rationale": result.data.get("rationale", ""),
        })
        return disposition

    # ------------------------------------------------------------------
    # Role invocation + result application
    # ------------------------------------------------------------------

    def _invoke_and_apply(self, role_name: str) -> RoleResult:
        result = self._safe_invoke(role_name)
        if result.status == "FAIL_CLOSED":
            self.ctx.record_incident(
                f"Role {role_name} FAIL_CLOSED: {result.fail_reason}", severity="high")
            self.ctx.close("failed")
            return result

        if result.status == "COMPLETE":
            self._apply_result(role_name, result)
        return result

    def _apply_result(self, role_name: str, result: RoleResult) -> None:
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
            accepted = data.get("accepted", [])
            rejected = data.get("rejected", [])
            if accepted:
                self.ctx.add_accepted_evidence(accepted)
            self.ctx.append_event(SOURCE_ACCEPTED, {
                "accepted": len(accepted), "rejected": len(rejected)})

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

    # ------------------------------------------------------------------
    # Safe invocation
    # ------------------------------------------------------------------

    def _safe_invoke(self, role_name: str) -> RoleResult:
        role_fn = self._role_registry.get(role_name)
        if role_fn is None:
            return RoleResult(role_name, "FAIL_CLOSED",
                              fail_reason=f"Unknown role: {role_name}")

        est_tokens, est_cost = 1000, 0.01
        if not self.budget.reserve(est_tokens, est_cost):
            self.ctx.append_event(BUDGET_EXHAUSTED, self.budget.to_dict())
            self.ctx.close("budget_exhausted")
            return RoleResult(role_name, "FAIL_CLOSED", fail_reason="Budget exhausted")

        ctx_view = self.ctx.view_for(role_name)
        try:
            result = role_fn(ctx_view)
        except Exception as exc:
            result = RoleResult(role_name, "FAIL_CLOSED", fail_reason=str(exc))

        self.budget.reconcile(est_tokens, result.token_estimate, est_cost, result.cost_estimate)

        if result.status == "FAIL_CLOSED":
            self.ctx.append_event(ROLE_FAILED, result.to_dict())
        else:
            self.ctx.append_event(ROLE_COMPLETED, result.to_dict())

        if result.status == "DELEGATE" and result.delegate_to:
            if self.budget.record_delegation():
                return self._safe_invoke(result.delegate_to)
            return RoleResult(role_name, "FAIL_CLOSED", fail_reason="Delegation limit exceeded")

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _check_budget(self) -> bool:
        if self.budget.is_exhausted:
            self.ctx.append_event(BUDGET_EXHAUSTED, self.budget.to_dict())
            self.ctx.close("budget_exhausted")
            return True
        if _time.monotonic() - self._start_wall > self.budget.max_duration_seconds:
            self.ctx.close("failed")
            return True
        return False

    def _check_stale(self) -> bool:
        if self.ctx.is_stale():
            self.ctx.record_incident("Run aborted: repository state changed", severity="high")
            self.ctx.close("failed")
            return True
        return False
