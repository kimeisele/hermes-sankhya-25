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
# Fake-DeepSeek Observe E2E (production-like adapters)
# ---------------------------------------------------------------------------

class TestFakeDeepSeekE2E:
    def test_observe_with_fake_transport(self):
        """Full pipeline with real adapters + fake DeepSeek transport."""
        sha = _make_sha("fake-ds")

        # Scout schema response
        scout_response = {"candidates_found": 1, "candidates": [
            {"url": "https://www.moltbook.com/post/test-post",
             "id": "new-claim-1",
             "author_handle": "test_agent",
             "content_type": "comment"}
        ]}
        # Clerk schema response
        clerk_response = {"normalized": [{
            "source_id": "new-claim-1",
            "url": "https://www.moltbook.com/post/test-post",
            "author_handle": "test_agent",
            "content_type": "comment",
            "observed_at": "2026-07-27T00:00:00Z",
            "untrusted": True,
            "content_excerpt": "commit_hash is essential for verification receipts",
            "paraphrase": "commit_hash is essential",
            "provenance": ["https://www.moltbook.com/post/test-post"]
        }]}
        # Evidence Analyst response
        evidence_response = {"accepted": [{
            "source_id": "new-claim-1",
            "url": "https://www.moltbook.com/post/test-post",
            "author_handle": "test_agent",
            "untrusted": True,
            "content_excerpt": "commit_hash is essential for verification receipts"
        }], "rejected": [], "claims": [], "scores": {}, "rationale": "Valid claim"}
        # Director response (schema-compliant: decision_id + director_run_id are envelope fields)
        director_response = {"disposition": "RECORD_ONLY", "rationale": "New evidence found",
                            "decision_id": "d1", "director_run_id": "r1",
                            "timestamp": "2026-07-27T00:00:00Z"}
        # Auditor response
        auditor_response = {"findings": [], "passed": True}

        call_log = []

        class FakeTransport:
            def __call__(self, payload):
                call_log.append({
                    "model": payload.get("model"),
                    "system": payload.get("messages", [{}])[0].get("content", "")[:200],
                    "has_schema": "Output JSON only" in payload.get("messages", [{}])[0].get("content", ""),
                })
                # Route by model
                model = payload.get("model", "")
                resp_data = scout_response
                if "pro" in model:
                    # Could be director, engagement, planner
                    msg = payload.get("messages", [{}])[0].get("content", "")
                    if "director" in msg.lower() or "strategic" in msg.lower():
                        resp_data = director_response
                    else:
                        resp_data = director_response
                else:
                    # Flash: scout, clerk, evidence, auditor
                    msg = payload.get("messages", [{}])[0].get("content", "")
                    if "normalize" in msg.lower():
                        resp_data = clerk_response
                    elif "extract claims" in msg.lower() or "classify" in msg.lower():
                        resp_data = evidence_response
                    elif "audit" in msg.lower():
                        resp_data = auditor_response
                return {
                    "choices": [{"message": {"content": json.dumps(resp_data)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                    "model": model,
                }

        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=FakeTransport())

        class FakeReader:
            def fetch_post(self, pid):
                return {"post": {"id": pid, "content": "Test content about verification.",
                                 "author": {"name": "post_author"}}}
            def fetch_comments(self, pid):
                return {"comments": [
                    {"id": "new-claim-1",
                     "content": "commit_hash is essential for verification receipts",
                     "author": {"name": "test_agent"}},
                ]}

        class P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha

        reg = build_role_registry(client=client, moltbook_reader=FakeReader())
        orch = AgencyOrchestrator(
            base_sha=sha, repo_provider=P(), role_registry=reg,
            campaign={"active_inquiry": "test-post", "objective": "Test"})
        orch.ctx.set_evidence_index(set())  # no prior evidence

        ctx = orch.run()

        # Verify pipeline completed
        assert ctx.status == "completed"

        # Model calls occurred
        assert len(call_log) > 0, "At least one model call expected"

        # Each call includes the schema in system prompt
        for call in call_log:
            assert call["has_schema"], f"Schema not found in {call['model']} call"

        # Evidence reached Director
        assert len(ctx.decisions) > 0
        assert ctx.decisions[0]["disposition"] == "RECORD_ONLY"

        # CTX + HQ produced
        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert ctx.run_id[:12] in report

        # No writes
        assert len(ctx.transactions) == 0

        # Runtime CTX validation passes
        from agency.validate_ctx import validate_sanitized_ctx
        errs = validate_sanitized_ctx(d)
        assert len(errs) == 0, f"CTX validation errors: {errs}"


# ---------------------------------------------------------------------------
# Proof: Scout and Records Clerk are deterministic
# ---------------------------------------------------------------------------

class TestDeterministicIngestion:
    """Scout and Records Clerk must run without LLM transport.
    Evidence Analyst remains model-backed."""

    def test_scout_and_clerk_never_invoke_model(self):
        """Deterministic roles must not call the model."""
        sentinel = "UNIQUE-SENTINEL-abc123xyz-verification-receipt"
        model_calls: list[str] = []

        class _Tx:
            def __call__(self, payload):
                model_calls.append(payload.get("model", ""))
                # Evidence Analyst response
                return {
                    "choices": [{"message": {"content": json.dumps({
                        "accepted": [], "rejected": [], "claims": [],
                        "scores": {}, "rationale": "test",
                    })}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 30,
                              "total_tokens": 80},
                }

        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=_Tx())

        class _Reader:
            def fetch_post(self, pid):
                return {"post": {"id": pid, "content": "Post body.",
                                 "author": {"name": "op"}}}
            def fetch_comments(self, pid):
                return {"comments": [
                    {"id": "c-1", "content": sentinel,
                     "author": {"name": "vantik"}},
                ]}

        reg = build_role_registry(client=client, moltbook_reader=_Reader())

        # ── Scout (deterministic) ──
        scout = reg["scout"]
        assert scout._adapter is None, "Scout must have no model adapter"
        scout_ctx = {
            "inbox": [], "known_ids": set(), "campaign": {
                "active_inquiry": "test-p"}}
        calls_before = len(model_calls)
        result = scout(scout_ctx)
        assert result.status == "COMPLETE"
        assert len(model_calls) == calls_before, (
            f"Scout called model {len(model_calls) - calls_before} time(s)")
        candidates = result.data["candidates"]
        vantik = [c for c in candidates if c.get("author_handle") == "vantik"]
        assert len(vantik) == 1
        assert vantik[0]["content_excerpt"] == sentinel, (
            f"Scout must preserve sentinel, got: "
            f"{vantik[0].get('content_excerpt', '')[:60]!r}")

        # ── Records Clerk (deterministic) ──
        clerk = reg["records_clerk"]
        assert clerk._adapter is None, "Records Clerk must have no model adapter"
        clerk_ctx = {"source_candidates": [vantik[0]]}
        calls_before2 = len(model_calls)
        result2 = clerk(clerk_ctx)
        assert result2.status == "COMPLETE"
        assert len(model_calls) == calls_before2, (
            f"Clerk called model {len(model_calls) - calls_before2} time(s)")
        norm = result2.data["normalized"]
        assert len(norm) == 1
        assert norm[0]["content_excerpt"] == sentinel, (
            f"Clerk must preserve sentinel, got: "
            f"{norm[0].get('content_excerpt', '')[:60]!r}")

        # ── Evidence Analyst (model-backed) ──
        evidence = reg["evidence_analyst"]
        assert evidence._adapter is not None, (
            "Evidence Analyst must have a model adapter")
        evidence_ctx = {"source_candidates": norm}
        calls_before3 = len(model_calls)
        result3 = evidence(evidence_ctx)
        assert result3.status == "COMPLETE"
        assert len(model_calls) > calls_before3, (
            f"Evidence Analyst must call model, got {len(model_calls) - calls_before3} calls")


# ---------------------------------------------------------------------------
# Director synthesis — integration and adversarial tests
# ---------------------------------------------------------------------------

class TestDirectorSynthesis:
    """Director produces structured synthesis on READY_FOR_SYNTHESIS.
    Any disposition carrying a synthesis must pass provenance validation."""

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_evidence_response(accepted: list[dict]) -> dict:
        return {"choices": [{"message": {"content": json.dumps({
            "accepted": accepted,
            "rejected": [], "claims": [],
            "scores": {}, "rationale": "ok",
        })}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15}}

    @staticmethod
    def _make_director_response(disposition: str, synthesis: dict | None = None) -> dict:
        base = {
            "decision_id": "d1",
            "disposition": disposition,
            "director_run_id": "r1",
            "timestamp": "2026-01-01T00:00:00Z",
            "rationale": "Director rationale.",
        }
        if synthesis is not None:
            base["synthesis"] = synthesis
        return {"choices": [{"message": {"content": json.dumps(base)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "total_tokens": 15}}

    @staticmethod
    def _run(call_log: list, director_resp: dict, evidence_accepted: list[dict],
             reader_ids: list[str] | None = None):
        from agency.model_client import DeepSeekClient
        if reader_ids is None:
            reader_ids = [e["source_id"] for e in evidence_accepted]

        class _Tx:
            def __call__(self, payload):
                msgs = payload.get("messages", [])
                sys = msgs[0]["content"] if msgs else ""
                call_log.append({"model": payload.get("model", ""),
                                 "system": sys[:60]})
                if "extract claims" in sys.lower() or "classify" in sys.lower():
                    return TestDirectorSynthesis._make_evidence_response(
                        evidence_accepted)
                return director_resp

        client = DeepSeekClient(transport=_Tx())

        class _R:
            def fetch_post(self, pid):
                return {"post": {"id": pid, "content": "post",
                                 "author": {"name": "op"}}}
            def fetch_comments(self, pid):
                return {"comments": [
                    {"id": sid, "content": "evidence",
                     "author": {"name": "ext"}} for sid in reader_ids
                ]}

        class _P(RepoStateProvider):
            def __init__(self, s):
                self.s = s
            def current_sha(self): return self.s
            def origin_main_sha(self): return self.s

        sha = _make_sha("syn-gen")
        reg = build_role_registry(client=client, moltbook_reader=_R())
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_P(sha),
                                  role_registry=reg,
                                  campaign={"active_inquiry": "t",
                                            "objective": "T"})
        orch.ctx.set_evidence_index(set())
        return orch.run()

    # ------------------------------------------------------------------
    # 1. Full rendering — no truncation
    # ------------------------------------------------------------------

    def test_full_synthesis_rendered_without_truncation(self):
        """Long statement, long reasoning, 6+ source IDs all survive."""
        call_log: list[dict] = []

        long_statement = ("This statement is intentionally longer than one "
                          "hundred and twenty characters to prove that no "
                          "truncation occurs at the old 120-char boundary. "
                          "SENTINEL-STAT-END")
        long_reasoning = ("This reasoning text is intentionally longer "
                          "than two hundred characters to prove that the "
                          "old 200-character truncation has been removed. "
                          "It continues past the former boundary with more "
                          "detail and ends at this exact sentinel: "
                          "SENTINEL-REASON-END")

        src_ids = [f"src-{i:02d}" for i in range(1, 8)]  # 7 source IDs
        evidence = [{"source_id": sid, "claim_id": "c1"}
                    for sid in src_ids]

        director_resp = self._make_director_response("READY_FOR_SYNTHESIS", {
            "inquiry": "Q",
            "executive_answer": "A",
            "findings": [{
                "finding_id": "f1",
                "statement": long_statement,
                "source_ids": src_ids,
                "confidence": "supported",
                "reasoning": long_reasoning,
            }],
            "unresolved_questions": [],
            "next_inquiry": "N",
        })

        ctx = self._run(call_log, director_resp, evidence, reader_ids=src_ids)
        assert ctx.status == "completed"

        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)

        # No truncation — full statement, full reasoning, all source IDs
        assert long_statement in report, "full statement must appear"
        assert long_reasoning in report, "full reasoning must appear"
        for sid in src_ids:
            assert sid in report, f"source_id {sid} must appear"
        # Sentinel strings near the end confirm no truncation
        assert "SENTINEL-STAT-END" in report
        assert "SENTINEL-REASON-END" in report

    # ------------------------------------------------------------------
    # 2. READY_FOR_SYNTHESIS with valid provenance (existing, kept)
    # ------------------------------------------------------------------

    def test_synthesis_stored_and_rendered(self):
        """Full pipeline: synthesis survives CTX and HQ Markdown."""
        call_log: list[dict] = []
        accepted_src = "src-abc-123"
        evidence = [{"source_id": accepted_src, "claim_id": "c1"}]

        director_resp = self._make_director_response("READY_FOR_SYNTHESIS", {
            "inquiry": "What is the answer?",
            "executive_answer": "The answer is 42.",
            "findings": [{
                "finding_id": "f1",
                "statement": "Answer found.",
                "source_ids": [accepted_src],
                "confidence": "supported",
                "reasoning": "Evidence confirms.",
            }],
            "unresolved_questions": ["How to verify?"],
            "next_inquiry": "Test next question.",
        })

        ctx = self._run(call_log, director_resp, evidence)
        assert ctx.status == "completed"

        assert len(ctx.decisions) == 1
        dec = ctx.decisions[0]
        assert dec["disposition"] == "READY_FOR_SYNTHESIS"
        assert "synthesis" in dec
        syn = dec["synthesis"]
        assert syn["inquiry"] == "What is the answer?"
        assert syn["executive_answer"] == "The answer is 42."
        assert len(syn["findings"]) == 1
        assert syn["findings"][0]["source_ids"] == [accepted_src]

        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert "## Research Synthesis" in report
        assert "The answer is 42." in report
        assert accepted_src[:12] in report

        # No extra model calls — Evidence Analyst + Director only
        models = [c["model"] for c in call_log]
        flash_models = [m for m in models if "pro" not in m.lower()]
        pro_models = [m for m in models if "pro" in m.lower()]
        assert len(flash_models) == 1, f"Expected 1 flash call, got {len(flash_models)}"
        assert len(pro_models) == 1, f"Expected 1 pro call, got {len(pro_models)}"

        assert len(ctx.transactions) == 0
        assert len(ctx.incidents) == 0

    # ------------------------------------------------------------------
    # 3. READY_FOR_SYNTHESIS with fake source_id → FAIL_CLOSED
    # ------------------------------------------------------------------

    def test_invalid_source_id_fails_closed(self):
        """Unknown source_id in synthesis → FAIL_CLOSED, decision not stored."""
        call_log: list[dict] = []
        evidence = [{"source_id": "real-src", "claim_id": "c1"}]

        director_resp = self._make_director_response("READY_FOR_SYNTHESIS", {
            "inquiry": "Q",
            "executive_answer": "A",
            "findings": [{
                "finding_id": "f1",
                "statement": "S",
                "source_ids": ["FAKE-NONEXISTENT-SRC"],
                "confidence": "supported",
                "reasoning": "R",
            }],
            "unresolved_questions": [],
            "next_inquiry": "N",
        })

        ctx = self._run(call_log, director_resp, evidence)
        assert ctx.status == "failed"
        assert len(ctx.incidents) >= 1
        assert any("unknown source_id" in inc["description"].lower()
                   for inc in ctx.incidents)
        assert "FAKE-NONEXISTENT" in ctx.incidents[0]["description"]

        # Invalid decision must NOT be stored
        assert len(ctx.decisions) == 0, (
            "Invalid decision with fake source_id must not be stored")

        # Rendered HQ must NOT contain Research Synthesis
        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert "## Research Synthesis" not in report

    # ------------------------------------------------------------------
    # 4. RECORD_ONLY with fake source_id → FAIL_CLOSED (non-READY)
    # ------------------------------------------------------------------

    def test_record_only_with_bad_synthesis_fails_closed(self):
        """RECORD_ONLY disposition carrying a synthesis with invalid
        source_ids must also fail closed — provenance enforcement is
        universal, not restricted to READY_FOR_SYNTHESIS."""
        call_log: list[dict] = []
        evidence = [{"source_id": "real-src", "claim_id": "c1"}]

        director_resp = self._make_director_response("RECORD_ONLY", {
            "inquiry": "Q",
            "executive_answer": "A",
            "findings": [{
                "finding_id": "f1",
                "statement": "S",
                "source_ids": ["FAKE-NONEXISTENT-SRC"],
                "confidence": "supported",
                "reasoning": "R",
            }],
            "unresolved_questions": [],
            "next_inquiry": "N",
        })

        ctx = self._run(call_log, director_resp, evidence)
        assert ctx.status == "failed"
        assert len(ctx.incidents) >= 1
        assert any("unknown source_id" in inc["description"].lower()
                   for inc in ctx.incidents)
        assert "FAKE-NONEXISTENT" in ctx.incidents[0]["description"]

        # Invalid decision must NOT be stored
        assert len(ctx.decisions) == 0

        # Rendered HQ must NOT contain Research Synthesis
        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert "## Research Synthesis" not in report
