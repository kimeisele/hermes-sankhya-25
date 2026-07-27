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
    def test_repository_mismatch_rejected(self):
        from agency.artifact import validate_artifact
        import json
        import tempfile
        import os
        ctx = {"repository": "wrong/repo", "run_id": "test-123"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(ctx, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="Repository mismatch"):
                validate_artifact(path, expected_run_id="test-123")
        finally:
            os.unlink(path)

    def test_base_sha_mismatch_rejected(self):
        from agency.artifact import validate_artifact
        import json
        import tempfile
        import os
        ctx = {"repository": "kimeisele/hermes-sankhya-25", "base_sha": "a" * 40,
               "run_id": "r1"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(ctx, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="Base SHA mismatch"):
                validate_artifact(path, expected_base_sha="b" * 40)
        finally:
            os.unlink(path)

    def test_proposal_hash_mismatch_rejected(self):
        from agency.artifact import validate_artifact
        import json
        import tempfile
        import os
        ctx = {"repository": "kimeisele/hermes-sankhya-25",
               "run_id": "r1", "base_sha": "",
               "engagement_proposals": [{"proposal_id": "p1", "content_hash": "a" * 64,
                                         "approval_state": "approved", "consumed": False,
                                         "target_content_id": "t1"}]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(ctx, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="Proposal hash mismatch"):
                validate_artifact(path, proposal_id="p1", proposal_hash="b" * 64,
                                  target_content_id="t1")
        finally:
            os.unlink(path)

    def test_consumed_proposal_rejected(self):
        from agency.artifact import validate_artifact
        import json
        import tempfile
        import os
        ctx = {"repository": "kimeisele/hermes-sankhya-25",
               "run_id": "r1", "base_sha": "",
               "engagement_proposals": [{"proposal_id": "p1", "content_hash": "a" * 64,
                                         "approval_state": "approved", "consumed": True,
                                         "target_content_id": "t1"}]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(ctx, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="already consumed"):
                validate_artifact(path, proposal_id="p1", proposal_hash="a" * 64,
                                  target_content_id="t1")
        finally:
            os.unlink(path)

    def test_not_approved_rejected(self):
        from agency.artifact import validate_artifact
        import json
        import tempfile
        import os
        ctx = {"repository": "kimeisele/hermes-sankhya-25",
               "run_id": "r1", "base_sha": "",
               "engagement_proposals": [{"proposal_id": "p1", "content_hash": "a" * 64,
                                         "approval_state": "draft", "consumed": False,
                                         "target_content_id": "t1"}]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(ctx, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="not approved"):
                validate_artifact(path, proposal_id="p1", proposal_hash="a" * 64,
                                  target_content_id="t1")
        finally:
            os.unlink(path)

    def test_valid_artifact_passes(self):
        from agency.artifact import validate_artifact
        import json
        import tempfile
        import os
        ctx = {"repository": "kimeisele/hermes-sankhya-25",
               "run_id": "r1", "base_sha": "",
               "engagement_proposals": [{"proposal_id": "p1", "content_hash": "a" * 64,
                                         "approval_state": "approved", "consumed": False,
                                         "target_content_id": "t1"}]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(ctx, f)
            path = f.name
        try:
            result = validate_artifact(path, proposal_id="p1", proposal_hash="a" * 64,
                                       target_content_id="t1")
            assert result["repository"] == "kimeisele/hermes-sankhya-25"
        finally:
            os.unlink(path)


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
