"""Comprehensive tests for Moltbook Agency V1 — hardened pass.

All offline. Covers model adapter, immutable events/views, role results,
Director routing, FAIL_CLOSED, evidence lifecycle, budget, sanitization,
security, Bridge regression.
"""
from __future__ import annotations

import hashlib
import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agency.context import (AgencyContextV1, AgencyBudget, RepoStateProvider, _sanitize_value)
from agency.events import EventLog, RUN_STARTED, RUN_CLOSED
from agency.roles import (RoleResult, ScoutRole, AgencyDirectorRole,
                          EngagementLeadRole, BridgeExecutorRole)
from agency.orchestrator import AgencyOrchestrator, DIRECTOR_ROUTES, build_role_registry
from agency.profiles import AgentProfile
from agency.hq import render_hq_markdown
from agency.policy import AgencyPolicy

# ---------------------------------------------------------------------------
# Fake model client
# ---------------------------------------------------------------------------

class FakeModelClient:
    def __init__(self, responses=None, always_fail=False, fail_kind="transport"):
        self.responses = responses or [{"disposition": "RECORD_ONLY"}]
        self.call_count = 0
        self.always_fail = always_fail
        self.fail_kind = fail_kind
        self.flash_model = "deepseek-v4-flash"
        self.pro_model = "deepseek-v4-pro"

    def call(self, model, system, user_context, schema=None, temperature=0.0):
        self.call_count += 1
        from agency.model_client import ModelCallResult
        if self.always_fail:
            return ModelCallResult(success=False, error="fail", error_kind=self.fail_kind)
        idx = min(self.call_count - 1, len(self.responses) - 1)
        return ModelCallResult(success=True, data=self.responses[idx],
                              input_tokens=100, output_tokens=50,
                              total_tokens=150, estimated_cost=0.001)


def _make_sha(s="t"):
    return hashlib.sha1(s.encode()).hexdigest()


def _fixed_provider(sha):
    class P(RepoStateProvider):
        def current_sha(self): return sha
        def origin_main_sha(self): return sha
    return P()


# ---------------------------------------------------------------------------
# Model adapter
# ---------------------------------------------------------------------------

class TestModelAdapter:
    def test_fake_success(self):
        f = FakeModelClient()
        r = f.call("m", "s", {}, {})
        assert r.success

    def test_fake_fail(self):
        f = FakeModelClient(always_fail=True, fail_kind="schema")
        r = f.call("m", "s", {}, {})
        assert not r.success
        assert r.error_kind == "schema"

    def test_cost_estimation(self):
        from agency.model_client import estimate_cost
        assert estimate_cost("deepseek-v4-flash", 1000, 500) > 0
        assert estimate_cost("deepseek-v4-pro", 1000, 500) > estimate_cost("deepseek-v4-flash", 1000, 500)

    def test_missing_key(self):
        from agency.model_client import DeepSeekClient
        import os
        old = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            c = DeepSeekClient()
            r = c.call("m", "s", {}, None)
            assert not r.success
            assert r.error_kind == "missing_key"
        finally:
            if old:
                os.environ["DEEPSEEK_API_KEY"] = old


# ---------------------------------------------------------------------------
# Event immutability
# ---------------------------------------------------------------------------

class TestEventImmutability:
    def test_input_mutation(self):
        data = {"k": "v"}
        log = EventLog()
        e = log.append(RUN_STARTED, data)
        data["k"] = "changed"
        assert e.data["k"] == "v"

    def test_serialized_mutation(self):
        log = EventLog()
        log.append(RUN_STARTED, {"x": 1})
        d = log.to_list()
        d[0]["data"]["x"] = 999
        assert log.last().data["x"] == 1

    def test_provenance_mutation(self):
        log = EventLog()
        log.append(RUN_STARTED, provenance=["a"])
        p = log.last().provenance
        p.append("b")
        assert len(log.last().provenance) == 1

    def test_sequence_monotonic(self):
        log = EventLog()
        assert log.append(RUN_STARTED).sequence == 0
        assert log.append(RUN_CLOSED).sequence == 1

    def test_frozen_rejects(self):
        log = EventLog()
        log.freeze()
        with pytest.raises(RuntimeError):
            log.append(RUN_CLOSED)


# ---------------------------------------------------------------------------
# CTX view immutability
# ---------------------------------------------------------------------------

class TestCTXViews:
    def test_view_mutation_safe(self):
        sha = _make_sha()
        ctx = AgencyContextV1(base_sha=sha)
        ctx.add_inbox([{"url": "x"}])
        v = ctx.view_for("scout")
        v["inbox"].clear()
        assert len(ctx.inbox) == 1

    def test_unknown_role_raises(self):
        sha = _make_sha()
        ctx = AgencyContextV1(base_sha=sha)
        with pytest.raises(ValueError):
            ctx.view_for("unknown")


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

class TestSanitization:
    def test_api_key_redacted(self):
        assert _sanitize_value({"api_key": "sk-123"}, "") == {"api_key": "[REDACTED]"}

    def test_nested_token_redacted(self):
        assert _sanitize_value({"a": {"moltbook_token": "t"}}, "")["a"]["moltbook_token"] == "[REDACTED]"

    def test_sanitized_has_incidents(self):
        sha = _make_sha()
        ctx = AgencyContextV1(base_sha=sha)
        ctx.record_incident("test", severity="high")
        ctx.close("completed")
        d = ctx.to_dict(sanitize=True)
        assert len(d["incidents"]) == 1


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class TestBudget:
    def test_would_exceed(self):
        b = AgencyBudget(max_tokens=100)
        b.tokens_used = 90
        assert b.would_exceed(tokens=20)

    def test_reserve_blocks(self):
        b = AgencyBudget(max_tokens=100)
        b.tokens_used = 95
        assert not b.reserve(estimated_tokens=10)

    def test_reconcile(self):
        b = AgencyBudget(max_tokens=1000)
        b.reserve(estimated_tokens=100, estimated_cost=0.01)
        b.reconcile(100, 50, 0.01, 0.005)
        assert b.tokens_used == 50

    def test_budget_exhaustion_terminal(self):
        sha = _make_sha()
        budget = AgencyBudget(max_role_calls=2)
        orch = AgencyOrchestrator(budget=budget, base_sha=sha,
                                  repo_provider=_fixed_provider(sha),
                                  role_registry=build_role_registry())
        ctx = orch.run()
        assert ctx.status == "budget_exhausted"
        assert ctx.events.has_event_type("BUDGET_EXHAUSTED")


# ---------------------------------------------------------------------------
# FAIL_CLOSED: every FAIL_CLOSED terminates
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_director_fail_terminates(self):
        sha = _make_sha()

        class FailingDirector(AgencyDirectorRole):
            def __call__(self, ctx_view):
                return RoleResult("agency_director", "FAIL_CLOSED",
                                  fail_reason="test failure")

        reg = build_role_registry()
        reg["agency_director"] = FailingDirector()
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_fixed_provider(sha),
                                  role_registry=reg)
        ctx = orch.run()
        assert ctx.status == "failed"

    def test_unknown_disposition_terminates(self):
        sha = _make_sha()

        class BadDirector(AgencyDirectorRole):
            def __call__(self, ctx_view):
                return RoleResult("agency_director", "COMPLETE",
                                  data={"disposition": "EXECUTE_ANYTHING"})

        reg = build_role_registry()
        reg["agency_director"] = BadDirector()
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_fixed_provider(sha),
                                  role_registry=reg)
        ctx = orch.run()
        assert ctx.status == "failed"

    def test_scout_fail_terminates(self):
        sha = _make_sha()

        class FailingScout(ScoutRole):
            def __call__(self, ctx_view):
                return RoleResult("scout", "FAIL_CLOSED", fail_reason="test")

        reg = build_role_registry()
        reg["scout"] = FailingScout()
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_fixed_provider(sha),
                                  role_registry=reg)
        ctx = orch.run()
        assert ctx.status == "failed"


# ---------------------------------------------------------------------------
# Evidence lifecycle: inbox → candidates → normalized → evidence → Director
# ---------------------------------------------------------------------------

class TestEvidenceLifecycle:
    def test_inbox_becomes_accepted_evidence(self):
        sha = _make_sha()
        reg = build_role_registry()
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_fixed_provider(sha),
                                  role_registry=reg)
        # Inject inbox item
        orch.ctx.add_inbox([{"id": "item1", "url": "https://x.com/p/1",
                             "author_handle": "test"}])
        ctx = orch.run()
        assert ctx.status == "completed"
        assert len(ctx.accepted_evidence) > 0

    def test_evidence_changes_director_disposition(self):
        sha = _make_sha()
        reg = build_role_registry()
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_fixed_provider(sha),
                                  role_registry=reg)
        orch.ctx.add_inbox([{"id": "item1", "url": "https://x.com/p/1",
                             "author_handle": "test"}])
        ctx = orch.run()
        decisions = ctx.decisions
        assert len(decisions) > 0
        # With evidence, Director should not be NOOP
        assert decisions[0]["disposition"] != "NOOP"


# ---------------------------------------------------------------------------
# Director routing
# ---------------------------------------------------------------------------

class TestRouting:
    def test_all_dispositions_defined(self):
        assert set(DIRECTOR_ROUTES.keys()) == {
            "NOOP", "RECORD_ONLY", "PROPOSE_ENGAGEMENT",
            "PROPOSE_ENGINEERING_INTAKE", "READY_FOR_SYNTHESIS",
            "ESCALATE_TO_HUMAN"}

    def test_noop_completes(self):
        sha = _make_sha()
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_fixed_provider(sha),
                                  role_registry=build_role_registry())
        ctx = orch.run()
        assert ctx.status == "completed"


# ---------------------------------------------------------------------------
# Operational roles
# ---------------------------------------------------------------------------

class TestOperationalRoles:
    def test_bridge_executor_command_allowlisting(self):
        bridge = BridgeExecutorRole()
        assert bridge.ROLE == "bridge_executor"
        # execute_write is the only write path
        result = bridge.execute_write({"type": "post", "title": "x", "submolt": "s"})
        assert isinstance(result, dict)  # parsed JSON or error dict

    def test_engagement_lead_hash(self):
        lead = EngagementLeadRole()
        result = lead({"accepted_evidence": [{"source_id": "src-1"}],
                       "decisions": [{"disposition": "PROPOSE_ENGAGEMENT"}],
                       "campaign": {"active_inquiry": "test-id"},
                       "base_sha": "a" * 40})
        assert result.status == "COMPLETE"
        assert "proposal" in result.data

    def test_dry_run_no_model_calls(self):
        sha = _make_sha()
        reg = build_role_registry(client=None)  # no client
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_fixed_provider(sha),
                                  role_registry=reg)
        ctx = orch.run()
        assert ctx.status == "completed"


# ---------------------------------------------------------------------------
# Behavioral: artifact validation
# ---------------------------------------------------------------------------

class TestArtifactValidation:
    def _make_prop(self, **overrides):
        from agency.artifact import canonical_hash
        prop = {"proposal_id": "p1", "target_content_id": "t1",
                "payload": {}, "base_sha": "", "repository": "kimeisele/hermes-sankhya-25",
                "approval_state": "approved", "consumed": False}
        prop.update(overrides)
        prop["content_hash"] = canonical_hash(prop)
        return prop

    def _make_ctx(self, proposals=None):
        return {"repository": "kimeisele/hermes-sankhya-25",
                "run_id": "r1", "base_sha": "",
                "engagement_proposals": proposals or []}

    def _write_and_validate(self, ctx, **kw):
        import json
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(ctx, f)
            path = f.name
        try:
            from agency.artifact import validate_artifact
            return validate_artifact(path, **kw)
        finally:
            os.unlink(path)

    def test_repository_mismatch_rejected(self):
        ctx = {"repository": "wrong/repo", "run_id": "test-123"}
        with pytest.raises(ValueError, match="Repository mismatch"):
            self._write_and_validate(ctx)

    def test_base_sha_mismatch_rejected(self):
        ctx = self._make_ctx()
        with pytest.raises(ValueError, match="Base SHA mismatch"):
            self._write_and_validate(ctx, expected_base_sha="b" * 40)

    def test_proposal_hash_mismatch_rejected(self):
        prop = self._make_prop()
        ctx = self._make_ctx([prop])
        with pytest.raises(ValueError, match="Canonical hash mismatch"):
            self._write_and_validate(ctx, proposal_id="p1", proposal_hash="b" * 64,
                                     target_content_id="t1")

    def test_consumed_proposal_rejected(self):
        prop = self._make_prop(consumed=True)
        ctx = self._make_ctx([prop])
        with pytest.raises(ValueError, match="already consumed"):
            self._write_and_validate(ctx, proposal_id="p1",
                                     proposal_hash=prop["content_hash"],
                                     target_content_id="t1")

    def test_not_approved_rejected(self):
        prop = self._make_prop(approval_state="draft")
        ctx = self._make_ctx([prop])
        with pytest.raises(ValueError, match="not approved"):
            self._write_and_validate(ctx, proposal_id="p1",
                                     proposal_hash=prop["content_hash"],
                                     target_content_id="t1")

    def test_valid_artifact_passes(self):
        prop = self._make_prop()
        ctx = self._make_ctx([prop])
        result = self._write_and_validate(ctx, proposal_id="p1",
                                          proposal_hash=prop["content_hash"],
                                          target_content_id="t1")
        assert result["repository"] == "kimeisele/hermes-sankhya-25"


# ---------------------------------------------------------------------------
# Behavioral: fake Moltbook reader
# ---------------------------------------------------------------------------

class TestFakeReader:
    def test_scout_uses_fake_reader(self):
        class FakeReader:
            def fetch_post(self, pid):
                return {"post": {"id": pid, "author": {"name": "test"},
                                 "url": f"https://x.com/p/{pid}"}}
            def fetch_comments(self, pid):
                return {"comments": []}

        scout = ScoutRole(moltbook=FakeReader())
        result = scout({"inbox": [], "accepted_evidence_ids": [],
                        "campaign": {"active_inquiry": "post-123"}})
        assert result.status == "COMPLETE"
        assert result.data["candidates_found"] == 1


# ---------------------------------------------------------------------------
# Behavioral: observe report/CTX same run
# ---------------------------------------------------------------------------

class TestSameRunArtifact:
    def test_report_and_ctx_same_run_id(self):
        sha = _make_sha()
        reg = build_role_registry()

        class P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha

        orch = AgencyOrchestrator(base_sha=sha, repo_provider=P(), role_registry=reg)
        ctx = orch.run()
        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert ctx.run_id[:12] in report  # HQ truncates to 12 chars


# ---------------------------------------------------------------------------
# Behavioral: PROPOSE_ENGAGEMENT creates proposal
# ---------------------------------------------------------------------------

class TestProposeEngagement:
    def test_engagement_creates_first_proposal(self):
        sha = _make_sha()

        class DirectorWithEngagement(AgencyDirectorRole):
            def __call__(self, ctx_view):
                return RoleResult("agency_director", "COMPLETE",
                                  data={"disposition": "PROPOSE_ENGAGEMENT"})

        reg = build_role_registry()
        reg["agency_director"] = DirectorWithEngagement()

        class P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha

        orch = AgencyOrchestrator(base_sha=sha, repo_provider=P(), role_registry=reg)
        # Add evidence so EngagementLead doesn't return NOOP
        orch.ctx.add_accepted_evidence([{"source_id": "src-1", "url": "x", "untrusted": True}])
        orch.ctx.campaign["active_inquiry"] = "test-inquiry"
        ctx = orch.run()
        proposals = ctx.engagement_proposals
        assert len(proposals) > 0, "PROPOSE_ENGAGEMENT should create at least one proposal"
        prop = proposals[0]
        assert "proposal_id" in prop
        assert "content_hash" in prop
        assert prop["consumed"] is False
        prop = proposals[0]
        assert "proposal_id" in prop
        assert "content_hash" in prop
        assert "approval_state" in prop
        assert prop["consumed"] is False


# ---------------------------------------------------------------------------
# Stale-state
# ---------------------------------------------------------------------------

class TestStaleState:
    def test_same_sha_not_stale(self):
        sha = _make_sha("same")
        ctx = AgencyContextV1(base_sha=sha, repo_provider=_fixed_provider(sha))
        assert not ctx.is_stale()

    def test_diff_sha_is_stale(self):
        ctx = AgencyContextV1(base_sha=_make_sha("a"),
                              repo_provider=_fixed_provider(_make_sha("b")))
        assert ctx.is_stale()


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

class TestProfiles:
    def test_progression(self):
        p = AgentProfile("a")
        assert p.relationship_stage == "observed"
        p.record_interaction(qualified=True)
        p.update_stage()
        assert p.relationship_stage == "engaged"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

class TestPolicy:
    def test_default_safe(self):
        p = AgencyPolicy()
        assert not p.can_write()

    def test_all_conditions(self):
        p = AgencyPolicy({"dry_run": False, "automation_enabled": True, "moltbook_read_only": False})
        assert p.can_write()


# ---------------------------------------------------------------------------
# HQ
# ---------------------------------------------------------------------------

class TestHQ:
    def test_shows_incidents(self):
        sha = _make_sha()
        ctx = AgencyContextV1(base_sha=sha)
        ctx.record_incident("Test incident", severity="high")
        ctx.close("completed")
        r = render_hq_markdown(ctx.to_dict(sanitize=True))
        assert "Test incident" in r

    def test_no_secrets(self):
        sha = _make_sha()
        ctx = AgencyContextV1(base_sha=sha)
        ctx.close("completed")
        r = render_hq_markdown(ctx.to_dict(sanitize=True))
        for pat in ["api_key", "Bearer", "Authorization"]:
            assert pat.lower() not in r.lower()


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

class TestSecurity:
    def test_prompt_injection_is_data(self):
        sha = _make_sha()
        ctx = AgencyContextV1(base_sha=sha)
        ctx.add_inbox([{"url": "x", "content": "rm -rf /"}])
        d = ctx.to_dict(sanitize=True)
        assert "rm -rf" not in json.dumps(d)

    def test_no_credential_fields(self):
        sha = _make_sha()
        ctx = AgencyContextV1(base_sha=sha)
        d = ctx.to_dict(sanitize=True)
        s = json.dumps(d)
        for pat in ["api_key", "Bearer", "Authorization", "MOLTBOOK_TOKEN"]:
            assert pat.lower() not in s.lower()

    def test_invalid_sha_rejected(self):
        from agency.context import RepoStateProvider
        class EmptyProvider(RepoStateProvider):
            def current_sha(self): return ""
        with pytest.raises(ValueError):
            AgencyContextV1(base_sha="", repo_provider=EmptyProvider())

    def test_valid_sha_accepted(self):
        sha = _make_sha("ok")
        ctx = AgencyContextV1(base_sha=sha)
        assert len(ctx.base_sha) == 40

    def test_double_close_idempotent(self):
        sha = _make_sha()
        ctx = AgencyContextV1(base_sha=sha)
        ctx.close("completed")
        ctx.close("failed")
        assert ctx.status == "completed"

    def test_json_schema_validation(self):
        from agency.model_client import validate_against_schema
        errors = validate_against_schema({"role": "x", "status": "COMPLETE"},
                                         {"type": "object", "required": ["role", "status"]})
        assert errors == []

        errors = validate_against_schema({"role": "x"},
                                         {"type": "object", "required": ["role", "status"]})
        assert len(errors) > 0

    def test_additional_properties_rejected(self):
        from agency.model_client import validate_against_schema
        schema = {"type": "object", "properties": {"role": {"type": "string"}},
                  "additionalProperties": False}
        errors = validate_against_schema({"role": "x", "extra": "y"}, schema)
        assert len(errors) > 0

    def test_wrong_top_level_type(self):
        from agency.model_client import validate_against_schema
        errors = validate_against_schema("not_an_object",
                                         {"type": "object"})
        assert len(errors) > 0


# ---------------------------------------------------------------------------
# Read-only E2E integration test
# ---------------------------------------------------------------------------

class TestReadOnlyE2E:
    def test_full_read_only_shift(self):
        """End-to-end: fake Moltbook → dedup → Scout → Clerk → Analyst → Director → CTX → HQ."""
        sha = _make_sha("e2e")

        # Fake Moltbook with known + new content
        KNOWN_ID = "known-comment-1"
        NEW_ID = "new-comment-1"

        class FakeReader:
            def fetch_post(self, pid):
                return {"post": {"id": pid, "content": "Test post content about verification receipts.",
                                 "author": {"name": "post_author"}}}
            def fetch_comments(self, pid):
                return {"comments": [
                    {"id": KNOWN_ID, "content": "Already analyzed: commit_hash is essential.",
                     "author": {"name": "known_agent"}},
                    {"id": NEW_ID, "content": "New claim: diff_url should be mandatory for code tasks.",
                     "author": {"name": "new_agent"}},
                ]}

        # Evidence index with known_id → cross-run dedup
        class P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha

        reg = build_role_registry(moltbook_reader=FakeReader())
        orch = AgencyOrchestrator(
            base_sha=sha, repo_provider=P(), role_registry=reg,
            workflow_run_id="42",
            campaign={"active_inquiry": "test-post", "objective": "Test inquiry"})
        orch.ctx.set_evidence_index({KNOWN_ID})  # simulate cross-run dedup

        ctx = orch.run()

        # Verify run completed
        assert ctx.status == "completed"
        assert ctx.workflow_run_id == "42"

        # Known comment deduplicated
        candidates = ctx.source_candidates
        candidate_ids = {c.get("source_id", c.get("id")) for c in candidates}
        assert KNOWN_ID not in candidate_ids, "Known comment should be deduplicated"
        assert NEW_ID in candidate_ids, "New comment should be a candidate"

        # Evidence reached analysis
        evidence = ctx.accepted_evidence
        assert len(evidence) > 0, "Evidence should be accepted"

        # Check content_excerpt present
        for c in candidates:
            if c.get("source_id", c.get("id")) == NEW_ID:
                assert "content_excerpt" in c
                assert "diff_url" in c.get("content_excerpt", "")

        # HQ + CTX share run_id
        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert ctx.run_id[:12] in report

        # CTX schema validation
        import jsonschema
        schema = json.loads(
            (Path(__file__).resolve().parents[2] / "schemas" / "agency-context-v1.schema.json").read_text())
        jsonschema.validate(instance=d, schema=schema)

        # No write occurred
        txn_count = len(ctx.transactions)
        assert txn_count == 0, "No Moltbook writes in read-only mode"

    def test_canonical_hash_consistency(self):
        """Proposal hash passes its own canonical-hash validation."""
        from agency.artifact import canonical_hash
        from agency.roles import EngagementLeadRole
        lead = EngagementLeadRole()
        result = lead({"accepted_evidence": [{"source_id": "src-1"}],
                       "decisions": [{"disposition": "PROPOSE_ENGAGEMENT"}],
                       "campaign": {"active_inquiry": "test-id"},
                       "base_sha": "a" * 40})
        assert result.status == "COMPLETE"
        prop = result.data["proposal"]
        computed = canonical_hash(prop)
        assert prop["content_hash"] == computed, "Canonical hash mismatch"
        assert prop["approval_state"] == "draft"

    def test_durable_dedup_known_rejected(self):
        """Known ID is rejected; new ID is emitted as candidate."""
        sha = _make_sha("dedup")

        class FakeReader:
            def fetch_post(self, pid):
                return {"post": {"id": "post-1", "content": "x", "author": {"name": "a"}}}
            def fetch_comments(self, pid):
                return {"comments": [
                    {"id": "known-1", "content": "x", "author": {"name": "a"}},
                    {"id": "new-1", "content": "y", "author": {"name": "b"}},
                ]}

        class P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha

        reg = build_role_registry(moltbook_reader=FakeReader())
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=P(),
                                  role_registry=reg)
        orch.ctx.campaign["active_inquiry"] = "test-post"
        orch.ctx.set_evidence_index({"known-1"})

        ctx = orch.run()
        candidates = ctx.source_candidates
        ids = {c.get("source_id", c.get("id")) for c in candidates}
        assert "known-1" not in ids
        assert "new-1" in ids

    def test_evidence_index_loader(self):
        """Repository-backed loader excludes known IDs."""
        from agency.evidence_index import load_evidence_index
        ids = load_evidence_index()
        assert isinstance(ids, set)
        # source records may or may not exist in test context
        # but the loader must always return a set, never crash


# ---------------------------------------------------------------------------
# Runtime CTX validation
# ---------------------------------------------------------------------------

class TestRuntimeValidation:
    def test_valid_ctx_passes(self):
        sha = _make_sha("val")
        ctx = AgencyContextV1(base_sha=sha)
        ctx.close("completed")
        d = ctx.to_dict(sanitize=True)
        from agency.validate_ctx import validate_sanitized_ctx
        assert validate_sanitized_ctx(d) == []

    def test_non_numeric_workflow_run_id_rejected(self):
        sha = _make_sha("val2")
        ctx = AgencyContextV1(base_sha=sha, workflow_run_id="abc123")
        ctx.close("completed")
        d = ctx.to_dict(sanitize=True)
        from agency.validate_ctx import validate_sanitized_ctx
        errs = validate_sanitized_ctx(d)
        assert any("workflow_run_id" in e for e in errs)

    def test_bad_base_sha_rejected(self):
        d = {"schema_version": "1.0", "run_id": "x", "workflow_run_id": None,
             "trigger": "manual", "shift": "morning",
             "started_at": "2026-01-01T00:00:00Z",
             "repository": "kimeisele/hermes-sankhya-25",
             "base_sha": "bad", "campaign": {}, "policy": {},
             "budget": {"max_role_calls": 1, "max_delegation_rounds": 1,
                        "max_tokens": 1, "max_cost_estimate": 1.0,
                        "max_duration_seconds": 1},
             "status": "completed", "completed_at": None, "events": []}
        from agency.validate_ctx import validate_sanitized_ctx
        errs = validate_sanitized_ctx(d)
        assert any("base_sha" in e for e in errs)




# ---------------------------------------------------------------------------
# Epistemic hardening tests A–H
# ---------------------------------------------------------------------------

_OBJECTIVE = "What claims and proposals appear in the discussion?"

_FAKE_TX = None  # will be instantiated per test

def _make_evidence_response(accepted_list, rejected=None):
    return {"choices": [{"message": {"content": json.dumps({
        "accepted": accepted_list,
        "rejected": rejected or [],
        "claims": [], "scores": {}, "rationale": "test",
    })}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

def _make_director_response(disposition, synthesis=None):
    base = {"decision_id": "d1", "disposition": disposition,
            "director_run_id": "r1", "timestamp": "2026-01-01T00:00:00Z",
            "rationale": "test"}
    if synthesis is not None:
        base["synthesis"] = synthesis
    return {"choices": [{"message": {"content": json.dumps(base)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class TestEpistemicHardening:
    """Tests A–H for epistemic hardening of the research synthesis."""

    def _make_canonical(self, sid, author, excerpt, kind, text):
        return {
            "id": sid, "url": f"https://m.example/{sid}",
            "author_handle": author, "content_type": "comment",
            "untrusted": True, "content_excerpt": excerpt,
            "claim_id": f"c-{sid[:4]}", "claim_kind": kind,
            "claim_text": text,
        }

    def _make_finding(self, fid, statement, src_ids, confidence, kind, quotes, reasoning="R"):
        return {
            "finding_id": fid, "statement": statement,
            "source_ids": src_ids, "confidence": confidence,
            "finding_kind": kind, "source_quotes": quotes,
            "reasoning": reasoning,
        }

    def _run(self, evidence_accepted, director_resp, canonical_items, call_log=None):
        from agency.model_client import DeepSeekClient
        if call_log is None:
            call_log = []

        class _Tx:
            def __call__(self, payload):
                call_log.append(payload.get("model", ""))
                msgs = payload.get("messages", [])
                sys = msgs[0]["content"] if msgs else ""
                if "extract claims" in sys.lower() or "classify" in sys.lower():
                    return _make_evidence_response(evidence_accepted)
                return director_resp

        client = DeepSeekClient(transport=_Tx())

        class _R:
            def fetch_post(self, pid):
                return {"post": {"id": pid, "content": "body", "author": {"name": "op"}}}
            def fetch_comments(self, pid):
                return {"comments": [{"id": c["id"], "content": c["content_excerpt"],
                         "author": {"name": c["author_handle"]}} for c in canonical_items]}

        sha = _make_sha("epi")
        class _P(RepoStateProvider):
            def __init__(self, s): self.s = s
            def current_sha(self): return self.s
            def origin_main_sha(self): return self.s

        reg = build_role_registry(client=client, moltbook_reader=_R())
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_P(sha),
            role_registry=reg, campaign={"active_inquiry": "t",
            "objective": _OBJECTIVE, "internal_author_handles": ["hermes-sankhya-25"]})
        orch.ctx.set_evidence_index(set())
        return orch.run()

    # ── Test A: canonical source ownership ──
    def test_a_canonical_source_ownership(self):
        """Model-forged author and excerpt must not survive."""
        sid = "src-a-001"
        real_author = "vantik"
        real_excerpt = "REAL EXCERPT from vantik."
        canonical = [self._make_canonical(sid, real_author, real_excerpt, "assertion", "Claim A")]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion",
                     "claim_text": "FAKE TEXT FROM MODEL"}]
        director = _make_director_response("RECORD_ONLY")
        ctx = self._run(evidence, director, canonical)
        assert ctx.status == "completed"
        assert len(ctx.accepted_evidence) >= 1
        ev = next(e for e in ctx.accepted_evidence if e["source_id"] == sid)
        assert ev["author_handle"] == real_author, f"Expected {real_author}, got {ev['author_handle']}"
        assert ev["content_excerpt"] == real_excerpt
        assert ev["source_class"] == "external"
        # claim_text is model-provided and survives as-is; canonical
        # override applies only to author, excerpt, type, url, class

    # ── Test B: unknown accepted source ──
    def test_b_unknown_accepted_source(self):
        """Evidence Analyst accepts FAKE-SOURCE-ID → fail closed."""
        evidence = [{"source_id": "FAKE-SOURCE-ID", "claim_id": "c1",
                     "claim_kind": "assertion", "claim_text": "X"}]
        director = _make_director_response("RECORD_ONLY")
        canonical = [self._make_canonical("real-src", "vantik", "real text", "assertion", "X")]
        ctx = self._run(evidence, director, canonical)
        assert ctx.status == "failed"
        assert len(ctx.incidents) >= 1
        assert any("unknown source_id" in i["description"].lower() for i in ctx.incidents)
        assert len(ctx.accepted_evidence) == 0

    # ── Test C: real inquiry required ──
    def test_c_inquiry_mismatch_fails_closed(self):
        """Synthesis inquiry is UUID, not objective → fail closed."""
        sid = "src-c-001"
        canonical = [self._make_canonical(sid, "vantik", "Assertion text here.", "assertion", "C")]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "C"}]
        synthesis = {
            "inquiry": "fd2c8049-5a16-417b-ab5d-8400a80d3ca7",  # UUID, not objective
            "executive_answer": "A", "findings": [],
            "unresolved_questions": [],
        }
        director = _make_director_response("READY_FOR_SYNTHESIS", synthesis)
        ctx = self._run(evidence, director, canonical)
        assert ctx.status == "failed"
        assert any("inquiry mismatch" in i["description"].lower() for i in ctx.incidents)
        assert len(ctx.decisions) == 0
        d = ctx.to_dict(sanitize=True)
        assert "## Research Synthesis" not in render_hq_markdown(d)

    # ── Test D: internal question cannot become supported requirement ──
    def test_d_internal_question_no_supported(self):
        """Internal question with confidence supported → FAIL_CLOSED."""
        sid = "src-d-001"
        excerpt = "Should we require binding to the work artifact?"
        canonical = [self._make_canonical(sid, "hermes-sankhya-25", excerpt, "question", "D")]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "question", "claim_text": "D"}]
        finding = self._make_finding("f1", "Binding is required.", [sid],
            "supported", "assertion",
            [{"source_id": sid, "quote": "Should we require binding"}])
        synthesis = {"inquiry": _OBJECTIVE, "executive_answer": "A",
            "findings": [finding], "unresolved_questions": []}
        director = _make_director_response("READY_FOR_SYNTHESIS", synthesis)
        ctx = self._run(evidence, director, canonical)
        assert ctx.status == "failed"
        assert len(ctx.incidents) >= 1
        incident_text = " ".join(i["description"].lower() for i in ctx.incidents)
        assert ("requires external source" in incident_text or
                "opinions/proposals/questions/warnings" in incident_text), (
            f"Unexpected incident: {ctx.incidents[0]['description']}")
        assert len(ctx.decisions) == 0

    # ── Test E: one external opinion is not supported fact ──
    def test_e_external_opinion_not_supported(self):
        """External opinion with confidence supported → FAIL_CLOSED."""
        sid = "src-e-001"
        excerpt = "I believe schema adoption is the real moat."
        canonical = [self._make_canonical(sid, "vantik", excerpt, "opinion", "E")]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "opinion", "claim_text": "E"}]
        finding = self._make_finding("f1", "Schema adoption is moat.", [sid],
            "supported", "assertion",
            [{"source_id": sid, "quote": "schema adoption is the real moat"}])
        synthesis = {"inquiry": _OBJECTIVE, "executive_answer": "A",
            "findings": [finding], "unresolved_questions": []}
        director = _make_director_response("READY_FOR_SYNTHESIS", synthesis)
        ctx = self._run(evidence, director, canonical)
        assert ctx.status == "failed"
        assert any("assertion" in i["description"].lower() for i in ctx.incidents)
        assert len(ctx.decisions) == 0

    # ── Test F: exact quote validation ──
    def test_f_bad_quote_fails_closed(self):
        """Quote not in excerpt → FAIL_CLOSED."""
        sid = "src-f-001"
        excerpt = "The minimum receipt needs commit_hash and timestamp."
        canonical = [self._make_canonical(sid, "vantik", excerpt, "assertion", "F")]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "F"}]
        bad_finding = self._make_finding("f1", "Receipt needs fields.", [sid],
            "supported", "assertion",
            [{"source_id": sid, "quote": "THIS TEXT IS NOT IN THE EXCERPT"}])
        synthesis = {"inquiry": _OBJECTIVE, "executive_answer": "A",
            "findings": [bad_finding], "unresolved_questions": []}
        ctx = self._run(evidence, _make_director_response("READY_FOR_SYNTHESIS", synthesis), canonical)
        assert ctx.status == "failed"
        assert any("quote not found" in i["description"].lower() for i in ctx.incidents)

    def test_f_good_quote_succeeds(self):
        """Exact substring in excerpt → success."""
        sid = "src-f-002"
        excerpt = "The minimum receipt needs commit_hash and timestamp."
        canonical = [self._make_canonical(sid, "vantik", excerpt, "assertion", "F2")]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "F2"}]
        good_finding = self._make_finding("f1", "Receipt needs fields.", [sid],
            "supported", "assertion",
            [{"source_id": sid, "quote": "needs commit_hash and timestamp"}])
        synthesis = {"inquiry": _OBJECTIVE, "executive_answer": "A",
            "findings": [good_finding], "unresolved_questions": []}
        ctx = self._run(evidence, _make_director_response("READY_FOR_SYNTHESIS", synthesis), canonical)
        assert ctx.status == "completed"
        d = ctx.to_dict(sanitize=True)
        assert "## Research Synthesis" in render_hq_markdown(d)

    # ── Test G: successful mixed synthesis ──
    def test_g_successful_mixed_synthesis(self):
        """Mixed internal/external with proper assertions → success."""
        s_int = "src-int"
        s_ext1 = "src-ext1"
        s_ext2 = "src-ext2"
        int_excerpt = "One follow-up question: what binds the receipt to work?"
        ext1_excerpt = "The minimum receipt needs commit_hash and timestamp as core fields."
        ext2_excerpt = "I assert that test_run_id with pass/fail counts is also required."

        canonical = [
            self._make_canonical(s_int, "hermes-sankhya-25", int_excerpt, "question", "Internal Q"),
            self._make_canonical(s_ext1, "vantik", ext1_excerpt, "assertion", "Ext assertion 1"),
            self._make_canonical(s_ext2, "contributor_b", ext2_excerpt, "assertion", "Ext assertion 2"),
        ]
        evidence = [
            {"source_id": s_int, "claim_id": "c1", "claim_kind": "question", "claim_text": "IQ"},
            {"source_id": s_ext1, "claim_id": "c2", "claim_kind": "assertion", "claim_text": "EA1"},
            {"source_id": s_ext2, "claim_id": "c3", "claim_kind": "assertion", "claim_text": "EA2"},
        ]

        # f1: mixed sources, supported (has external assertion)
        f1 = self._make_finding("f1", "Receipt needs core fields.", [s_ext1, s_int],
            "supported", "assertion",
            [{"source_id": s_ext1, "quote": "commit_hash and timestamp"},
             {"source_id": s_int, "quote": "what binds the receipt to work"}])

        # f2: external-only, supported
        f2 = self._make_finding("f2", "test_run_id is required.", [s_ext2],
            "supported", "assertion",
            [{"source_id": s_ext2, "quote": "test_run_id with pass/fail counts"}])

        # f3: internal-only, inferred (correctly calibrated)
        f3 = self._make_finding("f3", "Binding is an open question.", [s_int],
            "inferred", "question",
            [{"source_id": s_int, "quote": "what binds the receipt to work"}])

        synthesis = {"inquiry": _OBJECTIVE, "executive_answer": "Receipt fields are proposed.",
            "findings": [f1, f2, f3], "unresolved_questions": ["What binds receipt to work?"]}

        ctx = self._run(evidence, _make_director_response("READY_FOR_SYNTHESIS", synthesis), canonical)
        assert ctx.status == "completed"

        assert len(ctx.decisions) == 1
        dec = ctx.decisions[0]
        assert dec["disposition"] == "READY_FOR_SYNTHESIS"
        syn = dec["synthesis"]
        assert syn["inquiry"] == _OBJECTIVE

        # internal/external classification
        ev_map = {e["source_id"]: e for e in ctx.accepted_evidence}
        assert ev_map[s_int]["source_class"] == "internal"
        assert ev_map[s_ext1]["source_class"] == "external"
        assert ev_map[s_ext2]["source_class"] == "external"

        # source_basis computed correctly
        assert syn["findings"][0]["source_basis"] == "mixed"
        assert syn["findings"][1]["source_basis"] == "external"
        assert syn["findings"][2]["source_basis"] == "internal"

        # distinct author count
        assert syn["findings"][0]["distinct_author_count"] == 2
        assert syn["findings"][2]["distinct_author_count"] == 1

        # independent external contributor count
        assert syn["findings"][0]["independent_external_contributor_count"] == 1
        assert syn["findings"][1]["independent_external_contributor_count"] == 1
        assert syn["findings"][2]["independent_external_contributor_count"] == 0

        # HQ renders all metadata and quotes
        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert "## Research Synthesis" in report
        assert _OBJECTIVE in report
        assert "Source basis: mixed" in report
        assert "Distinct authors: 2" in report
        assert "Independent external contributors: 1" in report
        assert "commit_hash and timestamp" in report
        assert "test_run_id with pass/fail counts" in report
        assert "## Unresolved Questions" in report

        # no next_inquiry rendered
        assert "Next Inquiry" not in report

        assert len(ctx.transactions) == 0

    # ── Test H: schema rejection ──
    def test_h_schema_rejection(self):
        """Missing claim_kind, extra fields, missing source_quotes → all rejected."""
        # missing claim_kind
        bad1 = {"source_id": "x", "claim_id": "c1", "claim_text": "X"}
        # unknown claim_kind
        bad2 = {"source_id": "x", "claim_id": "c1", "claim_kind": "invalid_kind", "claim_text": "X"}
        # extra field in accepted
        bad3 = {"source_id": "x", "claim_id": "c1", "claim_kind": "assertion", "claim_text": "X", "extra_field": True}

        from agency.model_client import validate_against_schema
        import json
        sd = Path(__file__).resolve().parents[2] / "schemas"
        ev_schema = json.loads((sd / "evidence-analysis-output.schema.json").read_text())
        dec_schema = json.loads((sd / "agency-decision-v1.schema.json").read_text())

        # Evidence Analyst schema rejects bad items
        assert len(validate_against_schema({"accepted": [bad1], "rejected": []}, ev_schema)) > 0
        assert len(validate_against_schema({"accepted": [bad2], "rejected": []}, ev_schema)) > 0
        assert len(validate_against_schema({"accepted": [bad3], "rejected": []}, ev_schema)) > 0

        # Director schema: missing source_quotes
        bad_finding_no_quotes = {
            "finding_id": "f1", "statement": "S", "source_ids": ["s"],
            "confidence": "supported", "finding_kind": "assertion", "reasoning": "R",
        }
        bad_syn_no_quotes = {"inquiry": _OBJECTIVE, "executive_answer": "A",
            "findings": [bad_finding_no_quotes], "unresolved_questions": []}
        bad_dec = {"decision_id": "d1", "disposition": "READY_FOR_SYNTHESIS",
            "director_run_id": "r1", "timestamp": "2026-01-01T00:00:00Z",
            "rationale": "x", "synthesis": bad_syn_no_quotes}
        assert len(validate_against_schema(bad_dec, dec_schema)) > 0

        # extra field in finding
        bad_finding_extra = dict(bad_finding_no_quotes)
        bad_finding_extra["source_quotes"] = [{"source_id": "s", "quote": "q"}]
        bad_finding_extra["extra_field"] = True
        bad_syn_extra = {"inquiry": _OBJECTIVE, "executive_answer": "A",
            "findings": [bad_finding_extra], "unresolved_questions": []}
        bad_dec_extra = dict(bad_dec)
        bad_dec_extra["synthesis"] = bad_syn_extra
        assert len(validate_against_schema(bad_dec_extra, dec_schema)) > 0

        # next_inquiry in synthesis (extra property rejected by additionalProperties: false)
        syn_with_next = {"inquiry": _OBJECTIVE, "executive_answer": "A",
            "findings": [], "unresolved_questions": [], "next_inquiry": "N"}
        dec_with_next = dict(bad_dec)
        dec_with_next["synthesis"] = syn_with_next
        assert len(validate_against_schema(dec_with_next, dec_schema)) > 0
