"""Agency orchestrator — per-run role factory, FAIL_CLOSED termination,
dynamic Director routing, and evidence lifecycle.
"""
from __future__ import annotations

import time as _time
from typing import Any

import jsonschema

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

        import copy as _copy

        # Derive a reduced Evidence Analyst model-output schema.
        # The committed schema permits optional 'claims', 'scores', and
        # 'rationale' that the orchestrator does not consume.  Removing
        # them from the model contract may improve structured-output
        # reliability by reducing the output surface.
        _ea_model_schema = _copy.deepcopy(evidence_schema)
        for _field in ("claims", "scores", "rationale"):
            _ea_model_schema["properties"].pop(_field, None)
        _ea_model_schema["properties"] = {
            k: v for k, v in _ea_model_schema["properties"].items()
            if k in ("accepted", "rejected")
        }
        # Allow additional properties at the top level so existing
        # responses that still include the removed fields are not
        # rejected — the model just won't be instructed to produce them.
        _ea_model_schema.pop("additionalProperties", None)
        evidence_adapter = RoleModelAdapter(client, client.flash_model,
                                            flash_system or "You extract claims and classify evidence.",
                                            _ea_model_schema, is_write_critical=False)
        # Derive Director model-output schema from committed durable schema.
        # The model must not produce deterministic metadata fields.
        _finding_props = list(decision_schema["properties"]["synthesis"]
                              ["properties"]["findings"]["items"]["properties"].keys())
        _finding_req = list(decision_schema["properties"]["synthesis"]
                            ["properties"]["findings"]["items"]["required"])
        _model_schema = _copy.deepcopy(decision_schema)
        _model_finding = _model_schema["properties"]["synthesis"]["properties"]["findings"]["items"]
        for _field in ("source_basis", "distinct_author_count", "distinct_external_author_count"):
            _model_finding["properties"].pop(_field, None)
            if _field in _model_finding["required"]:
                _model_finding["required"].remove(_field)
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
                                       _model_schema, is_write_critical=True)
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
        # Reject raw Director response containing deterministic metadata fields
        for forbidden in ("source_basis", "distinct_author_count",
                          "distinct_external_author_count", "confidence"):
            for finding in director_data.get("synthesis", {}).get("findings", []):
                if forbidden in finding:
                    self.ctx.record_incident(
                        f"Director returned forbidden deterministic field: {forbidden}",
                        severity="high")
                    self.ctx.close("failed")
                    return "NOOP"

        disposition = director_data.get("disposition", "")
        if not disposition or disposition not in DIRECTOR_ROUTES:
            self.ctx.record_incident(
                f"Invalid Director disposition: '{disposition}'", severity="high")
            self.ctx.close("failed")
            return "NOOP"

        synthesis = director_data.get("synthesis")
        if synthesis:
            objective = self.ctx.campaign.get("objective", "")
            syn_inquiry = synthesis.get("inquiry", "")
            if syn_inquiry != objective:
                self.ctx.record_incident(
                    f"Synthesis inquiry mismatch: got '{syn_inquiry[:80]}' "
                    f"expected campaign objective", severity="high")
                self.ctx.close("failed")
                return "NOOP"

            # Build claim-level lookup: (source_id, claim_id) → evidence
            accepted_claims: dict[tuple[str, str], dict[str, Any]] = {}
            accepted_src_ids: set[str] = set()
            for ev in self.ctx.accepted_evidence:
                sid = ev.get("source_id", "")
                cid = ev.get("claim_id", "")
                if sid and cid:
                    accepted_src_ids.add(sid)
                    accepted_claims[(sid, cid)] = ev

            for finding in synthesis.get("findings", []):
                # Every source_id must be represented by at least one quote
                for src_id in finding.get("source_ids", []):
                    if not src_id or src_id not in accepted_src_ids:
                        self.ctx.record_incident(
                            f"Synthesis finding {finding.get('finding_id','?')} "
                            f"references unknown source_id: {src_id}",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"

                quoted_src_ids: set[str] = set()
                # Claim-kind fidelity: all referenced claims must have same claim_kind,
                # and finding_kind must match.
                claim_kinds: set[str] = set()
                for sq in finding.get("source_quotes", []):
                    q_src = sq.get("source_id", "")
                    q_claim = sq.get("claim_id", "")
                    quote = sq.get("quote", "")
                    if not q_src or q_src not in finding.get("source_ids", []):
                        self.ctx.record_incident(
                            f"Quote source_id {q_src} not in finding source_ids",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"
                    if not q_claim:
                        self.ctx.record_incident(
                            f"Quote missing claim_id in finding {finding.get('finding_id','?')}",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"
                    if not quote:
                        self.ctx.record_incident(
                            f"Empty quote in finding {finding.get('finding_id','?')}",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"
                    claim_key = (q_src, q_claim)
                    ac_ev = accepted_claims.get(claim_key)
                    if ac_ev is None:
                        self.ctx.record_incident(
                            f"Quote references unknown claim: {claim_key}",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"
                    excerpt = ac_ev.get("content_excerpt", "")
                    if quote not in excerpt:
                        self.ctx.record_incident(
                            f"Quote not found in canonical excerpt for "
                            f"finding {finding.get('finding_id','?')}, "
                            f"source {q_src} claim {q_claim}",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"
                    quoted_src_ids.add(q_src)
                    claim_kinds.add(ac_ev.get("claim_kind", "unknown"))

                # Claim-kind fidelity enforcement
                if len(claim_kinds) > 1:
                    self.ctx.record_incident(
                        f"Finding {finding.get('finding_id','?')}: "
                        f"mixed claim kinds {sorted(claim_kinds)} — "
                        f"all quoted claims must have the same claim_kind",
                        severity="high")
                    self.ctx.close("failed")
                    return "NOOP"
                if claim_kinds:
                    actual_kind = next(iter(claim_kinds))
                    if finding.get("finding_kind") != actual_kind:
                        self.ctx.record_incident(
                            f"Finding {finding.get('finding_id','?')}: "
                            f"finding_kind '{finding.get('finding_kind')}' "
                            f"does not match claim kind '{actual_kind}'",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"

                # Every finding source_id must have at least one quote
                for src_id in finding.get("source_ids", []):
                    if src_id not in quoted_src_ids:
                        self.ctx.record_incident(
                            f"Finding {finding.get('finding_id','?')}: "
                            f"source_id {src_id} has no quote coverage",
                            severity="high")
                        self.ctx.close("failed")
                        return "NOOP"

                # Compute deterministic metadata
                finding["source_basis"] = self._compute_source_basis(finding)
                finding["distinct_author_count"] = self._count_distinct_authors(finding)
                finding["distinct_external_author_count"] = (
                    self._count_distinct_external_authors(finding))

            # Validate enriched decision against committed schema
            from pathlib import Path as _Path
            import json as _json
            sd = _Path(__file__).resolve().parents[1] / "schemas"
            dec_schema = _json.loads((sd / "agency-decision-v1.schema.json").read_text())
            try:
                jsonschema.validate(instance=director_data, schema=dec_schema)
            except jsonschema.ValidationError as exc:
                self.ctx.record_incident(
                    f"Enriched Director decision schema validation failed: {exc.message}",
                    severity="high")
                self.ctx.close("failed")
                return "NOOP"

        self.ctx.append_event(DIRECTOR_DECISION, {
            "disposition": disposition,
            "rationale": director_data.get("rationale", ""),
        })
        self.ctx.add_decision(director_data)
        return disposition

    def _compute_source_basis(self, finding: dict[str, Any]) -> str:
        internal = 0
        external = 0
        unknown = 0
        for ev in self.ctx.accepted_evidence:
            sid = ev.get("source_id", "")
            if sid in finding.get("source_ids", []):
                sc = ev.get("source_class", "")
                if sc == "internal":
                    internal += 1
                elif sc == "external":
                    external += 1
                elif sc == "unknown":
                    unknown += 1
                else:
                    unknown += 1
        if internal and external:
            return "mixed"
        if internal and not external and not unknown:
            return "internal"
        if external and not internal and not unknown:
            return "external"
        if not internal and not external:
            return "unknown"
        return "mixed"

    def _count_distinct_authors(self, finding: dict[str, Any]) -> int:
        authors = set()
        for ev in self.ctx.accepted_evidence:
            sid = ev.get("source_id", "")
            if sid in finding.get("source_ids", []):
                ah = ev.get("author_handle", "")
                if ah and ah != "unknown":
                    authors.add(ah)
        return len(authors)

    def _count_distinct_external_authors(self, finding: dict[str, Any]) -> int:
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
                canonical_by_id: dict[str, dict[str, Any]] = {}
                for sc in self.ctx.source_candidates:
                    sid = sc.get("id") or sc.get("source_id", "")
                    if sid:
                        canonical_by_id[sid] = sc
                rehydrated = []
                seen_claims: set[tuple[str, str]] = set()
                for entry in accepted:
                    sid = entry.get("source_id", "")
                    cid = entry.get("claim_id", "")
                    if not sid:
                        self.ctx.record_incident(
                            "Evidence Analyst accepted entry with empty source_id",
                            severity="high")
                        self.ctx.close("failed")
                        return
                    if not cid:
                        self.ctx.record_incident(
                            "Evidence Analyst accepted entry with empty claim_id",
                            severity="high")
                        self.ctx.close("failed")
                        return
                    claim_key = (sid, cid)
                    if claim_key in seen_claims:
                        self.ctx.record_incident(
                            f"Evidence Analyst accepted duplicate claim: "
                            f"source_id={sid}, claim_id={cid}",
                            severity="high")
                        self.ctx.close("failed")
                        return
                    seen_claims.add(claim_key)
                    orig = canonical_by_id.get(sid)
                    if not orig:
                        self.ctx.record_incident(
                            f"Evidence Analyst accepted unknown source_id: {sid}",
                            severity="high")
                        self.ctx.close("failed")
                        return
                    author = orig.get("author_handle", "")
                    if not author or author == "unknown" or author not in self._internal_handles:
                        if not author or author in ("unknown", ""):
                            sc = "unknown"
                        else:
                            sc = "external"
                    else:
                        sc = "internal"
                    rehydrated.append({
                        "source_id": orig.get("id", sid),
                        "claim_id": cid,
                        "author_handle": author,
                        "content_excerpt": orig.get("content_excerpt", ""),
                        "content_type": orig.get("content_type", ""),
                        "url": orig.get("url", ""),
                        "source_class": sc,
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
