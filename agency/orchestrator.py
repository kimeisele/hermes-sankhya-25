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
    "READY_FOR_SYNTHESIS": ["AUDIT", "CLOSE_BOOKS"],
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
        # Domain-specific output schemas per role
        evidence_schema = json.loads((sd / "evidence-analysis-output.schema.json").read_text())
        decision_schema = json.loads((sd / "agency-decision-v1.schema.json").read_text())
        engagement_schema = json.loads((sd / "engagement-proposal.schema.json").read_text())
        audit_schema = json.loads((sd / "audit-output.schema.json").read_text())
        proposal_schema = json.loads((sd / "engineering-proposal-v1.schema.json").read_text())

        evidence_adapter = RoleModelAdapter(client, client.flash_model,
                                            flash_system or "You extract claims and classify evidence.",
                                            evidence_schema, is_write_critical=False)
        pro_adapter = RoleModelAdapter(client, client.pro_model,
                                       pro_system or (
            "You are a source-grounded research synthesizer, not an engineering "
            "adviser. Answer only the configured research objective. Report "
            "what sources assert, opine, propose, ask, or warn about. "
            "Preserve those distinctions. Distinguish internal discussion from "
            "external contribution. Never describe internal repetition as "
            "independent confirmation. Never convert a question or warning into "
            "a requirement. Never invent a solution. Never recommend "
            "implementation work. Never select the next inquiry. Unresolved "
            "questions must describe missing evidence, not prescribe action."),
                                       decision_schema, is_write_critical=True)
        engagement_adapter = RoleModelAdapter(client, client.pro_model,
                                              pro_system or "You draft engagement proposals.",
                                              engagement_schema, is_write_critical=True)
        auditor_adapter = RoleModelAdapter(client, client.flash_model,
                                           flash_system or "You audit policy, receipts, and budgets.",
                                           audit_schema, is_write_critical=False)
        planner_adapter = RoleModelAdapter(client, client.pro_model,
                                           pro_system or "You create engineering proposals.",
                                           proposal_schema, is_write_critical=True)

        registry["scout"] = ScoutRole(moltbook=moltbook_reader)
        registry["records_clerk"] = RecordsClerkRole()
        registry["evidence_analyst"] = EvidenceAnalystRole(adapter=evidence_adapter)
        registry["agency_director"] = AgencyDirectorRole(adapter=pro_adapter)
        registry["engagement_lead"] = EngagementLeadRole(adapter=engagement_adapter)
        registry["auditor"] = AuditorRole(adapter=auditor_adapter, pro_adapter=pro_adapter)
        registry["engineering_planner"] = EngineeringPlannerRole(adapter=planner_adapter)
    else:
        registry["scout"] = ScoutRole(moltbook=moltbook_reader)
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
                 moltbook_reader: Any = None,
                 workflow_run_id: str | None = None) -> None:
        self.policy = AgencyPolicy(policy_config)
        self.budget = budget or AgencyBudget()
        self._repo_provider = repo_provider or RepoStateProvider()
        self._role_registry = role_registry or build_role_registry()
        self._moltbook_reader = moltbook_reader
        self._internal_handles: set[str] = set(
            (campaign or {}).get("internal_author_handles", []))

        sha = base_sha
        if sha is None:
            sha = self._repo_provider.current_sha()
            if not sha or len(sha) != 40:
                raise ValueError(f"Cannot resolve valid base SHA: got '{sha}'")

        self.ctx = AgencyContextV1(
            trigger=trigger, shift=shift, repository=repository, base_sha=sha,
            campaign=campaign, policy=self.policy.to_dict(), budget=self.budget,
            repo_provider=self._repo_provider, workflow_run_id=workflow_run_id)
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
            self.ctx.record_incident(
                f"Role agency_director FAIL_CLOSED: {result.fail_reason}",
                severity="high")
            self.ctx.close("failed")
            return "NOOP"

        director_data = result.data
        disposition = director_data.get("disposition", "")
        if not disposition or disposition not in DIRECTOR_ROUTES:
            self.ctx.record_incident(
                f"Invalid Director disposition: '{disposition}'", severity="high")
            self.ctx.close("failed")
            return "NOOP"

        synthesis = director_data.get("synthesis")
        if synthesis:
            # --- inquiry must equal campaign objective ---
            objective = self.ctx.campaign.get("objective", "")
            syn_inquiry = synthesis.get("inquiry", "")
            if syn_inquiry != objective:
                self.ctx.record_incident(
                    f"Synthesis inquiry mismatch: got '{syn_inquiry[:80]}' "
                    f"expected campaign objective", severity="high")
                self.ctx.close("failed")
                return "NOOP"

            # --- build accepted evidence lookup ---
            accepted_ids: set[str] = set()
            accepted_map: dict[str, dict[str, Any]] = {}
            for ev in self.ctx.accepted_evidence:
                sid = ev.get("source_id", "")
                if sid:
                    accepted_ids.add(sid)
                    accepted_map[sid] = ev

            # --- validate source_ids and quotes ---
            for finding in synthesis.get("findings", []):
                for src_id in finding.get("source_ids", []):
                    if not src_id or src_id not in accepted_ids:
                        self.ctx.record_incident(
                            f"Synthesis finding {finding.get('finding_id','?')} "
                            f"references unknown source_id: {src_id}",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"

                # Validate exact quotes
                for sq in finding.get("source_quotes", []):
                    q_src = sq.get("source_id", "")
                    quote = sq.get("quote", "")
                    if not q_src or q_src not in finding.get("source_ids", []):
                        self.ctx.record_incident(
                            f"Quote source_id {q_src} not in finding source_ids",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"
                    if not quote:
                        self.ctx.record_incident(
                            f"Empty quote in finding {finding.get('finding_id','?')}",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"
                    ac_ev = accepted_map.get(q_src, {})
                    excerpt = ac_ev.get("content_excerpt", "")
                    if quote not in excerpt:
                        self.ctx.record_incident(
                            f"Quote not found in canonical excerpt for "
                            f"finding {finding.get('finding_id','?')}, "
                            f"source {q_src}",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"

                # --- confidence calibration ---
                confidence = finding.get("confidence", "")
                source_basis = self._compute_source_basis(finding)
                ext_contributors = self._count_independent_external(finding)

                # supported requires external source
                if confidence == "supported":
                    if source_basis == "internal":
                        self.ctx.record_incident(
                            f"Finding {finding.get('finding_id','?')}: "
                            f"confidence 'supported' requires external source, "
                            f"but source_basis is 'internal'",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"
                    if ext_contributors == 0:
                        self.ctx.record_incident(
                            f"Finding {finding.get('finding_id','?')}: "
                            f"confidence 'supported' requires at least one "
                            f"independent external contributor",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"
                    # Check that at least one external source is assertion
                    has_assertion = any(
                        accepted_map.get(sid, {}).get("claim_kind") == "assertion"
                        and accepted_map.get(sid, {}).get("source_class") == "external"
                        for sid in finding.get("source_ids", []))
                    if not has_assertion:
                        self.ctx.record_incident(
                            f"Finding {finding.get('finding_id','?')}: "
                            f"confidence 'supported' requires at least one external "
                            f"assertion source",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"

                # internal-only: only inferred or unknown
                if source_basis == "internal":
                    if confidence not in ("inferred", "unknown"):
                        self.ctx.record_incident(
                            f"Finding {finding.get('finding_id','?')}: "
                            f"internal-only findings may use 'inferred' or "
                            f"'unknown', not '{confidence}'",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"

                # All-opinion/proposal/question/warning/unknown → no supported
                kinds = {accepted_map.get(sid, {}).get("claim_kind", "unknown")
                         for sid in finding.get("source_ids", [])}
                if kinds and kinds <= {"opinion", "proposal", "question",
                                        "warning", "unknown"}:
                    if confidence == "supported":
                        self.ctx.record_incident(
                            f"Finding {finding.get('finding_id','?')}: "
                            f"confidence 'supported' not allowed when all "
                            f"sources are opinions/proposals/questions/warnings",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"

                # --- deterministic metadata ---
                finding["source_basis"] = source_basis
                finding["distinct_author_count"] = self._count_distinct_authors(finding)
                finding["independent_external_contributor_count"] = ext_contributors

        self.ctx.append_event(DIRECTOR_DECISION, {
            "disposition": disposition,
            "rationale": director_data.get("rationale", ""),
        })
        self.ctx.add_decision(director_data)
        return disposition

    def _compute_source_basis(self, finding: dict[str, Any]) -> str:
        internal = 0
        external = 0
        for ev in self.ctx.accepted_evidence:
            sid = ev.get("source_id", "")
            if sid in finding.get("source_ids", []):
                if ev.get("source_class") == "internal":
                    internal += 1
                else:
                    external += 1
        if internal and external:
            return "mixed"
        if internal:
            return "internal"
        return "external"

    def _count_distinct_authors(self, finding: dict[str, Any]) -> int:
        authors = set()
        for ev in self.ctx.accepted_evidence:
            sid = ev.get("source_id", "")
            if sid in finding.get("source_ids", []):
                authors.add(ev.get("author_handle", ""))
        return len(authors)

    def _count_independent_external(self, finding: dict[str, Any]) -> int:
        authors = set()
        for ev in self.ctx.accepted_evidence:
            sid = ev.get("source_id", "")
            if sid in finding.get("source_ids", []):
                if ev.get("source_class") == "external":
                    authors.add(ev.get("author_handle", ""))
        return len(authors)

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
                # Rehydrate every accepted entry from canonical source records.
                canonical_by_id: dict[str, dict[str, Any]] = {}
                for sc in self.ctx.source_candidates:
                    sid = sc.get("id") or sc.get("source_id", "")
                    if sid:
                        canonical_by_id[sid] = sc
                rehydrated = []
                for entry in accepted:
                    sid = entry.get("source_id", "")
                    if not sid:
                        self.ctx.record_incident(
                            "Evidence Analyst accepted entry with empty source_id",
                            severity="high")
                        self.ctx.close("failed")
                        return
                    orig = canonical_by_id.get(sid)
                    if not orig:
                        self.ctx.record_incident(
                            f"Evidence Analyst accepted unknown source_id: {sid}",
                            severity="high")
                        self.ctx.close("failed")
                        return
                    is_internal = orig.get("author_handle", "") in self._internal_handles
                    rehydrated.append({
                        "source_id": orig.get("id", orig.get("source_id", sid)),
                        "author_handle": orig.get("author_handle", ""),
                        "content_excerpt": orig.get("content_excerpt", ""),
                        "content_type": orig.get("content_type", ""),
                        "url": orig.get("url", ""),
                        "source_class": "internal" if is_internal else "external",
                        "claim_id": entry.get("claim_id", ""),
                        "claim_kind": entry.get("claim_kind", "unknown"),
                        "claim_text": entry.get("claim_text", ""),
                    })
                self.ctx.add_accepted_evidence(rehydrated)
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
