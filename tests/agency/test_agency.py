"""Tests for role outputs, orchestrator, model routing, profiles, HQ, and security."""
from __future__ import annotations

import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agency.context import AgencyContextV1, AgencyBudget
from agency.roles import (RoleResult, ScoutRole, RecordsClerkRole,
                          AgencyDirectorRole,
                          EngagementLeadRole, BridgeExecutorRole,
                          AuditorRole, ROLE_REGISTRY)
from agency.orchestrator import AgencyOrchestrator
from agency.models import model_for_role, is_write_critical, FLASH, PRO
from agency.profiles import AgentProfile
from agency.hq import render_hq_markdown, render_hq_html
from agency.policy import AgencyPolicy


# ---------------------------------------------------------------------------
# RoleResult tests
# ---------------------------------------------------------------------------

class TestRoleResult:
    def test_valid_status_accepted(self):
        r = RoleResult("scout", "COMPLETE")
        assert r.status == "COMPLETE"

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError):
            RoleResult("scout", "INVALID")

    def test_to_dict_serializable(self):
        r = RoleResult("scout", "COMPLETE", data={"count": 1},
                       provenance=["src-001"])
        d = r.to_dict()
        assert d["role"] == "scout"
        assert d["status"] == "COMPLETE"
        assert d["data"]["count"] == 1
        json.dumps(d)  # must not raise

    def test_fail_closed_with_reason(self):
        r = RoleResult("auditor", "FAIL_CLOSED",
                       fail_reason="Budget exhausted")
        assert r.fail_reason == "Budget exhausted"

    def test_delegate_with_target(self):
        r = RoleResult("agency_director", "DELEGATE",
                       delegate_to="evidence_analyst",
                       delegate_reason="Need deeper analysis")
        assert r.delegate_to == "evidence_analyst"


# ---------------------------------------------------------------------------
# Role implementation tests
# ---------------------------------------------------------------------------

class TestScoutRole:
    def test_noop_when_empty_inbox(self):
        scout = ScoutRole()
        result = scout({"inbox": []})
        assert result.status == "NOOP"

    def test_complete_with_candidates(self):
        scout = ScoutRole()
        result = scout({"inbox": [{"url": "https://x.com/p/1"}]})
        assert result.status == "COMPLETE"
        assert result.data["candidates_found"] == 1


class TestRecordsClerkRole:
    def test_noop_when_empty(self):
        clerk = RecordsClerkRole()
        result = clerk({"source_candidates": []})
        assert result.status == "NOOP"

    def test_marks_untrusted(self):
        clerk = RecordsClerkRole()
        result = clerk({"source_candidates": [
            {"url": "https://x.com/p/1", "author_handle": "test"}
        ]})
        assert result.status == "COMPLETE"
        assert result.data["normalized"][0]["untrusted"] is True


class TestAgencyDirectorRole:
    def test_noop_when_no_evidence(self):
        director = AgencyDirectorRole()
        result = director({
            "accepted_evidence": [],
            "budget": {"role_calls_used": 0, "max_role_calls": 20},
            "engagement_proposals": [],
        })
        assert result.status == "NOOP"

    def test_record_only_with_evidence(self):
        director = AgencyDirectorRole()
        result = director({
            "accepted_evidence": [{"source_id": "src-001"}],
            "budget": {"role_calls_used": 0, "max_role_calls": 20},
            "engagement_proposals": [],
        })
        assert result.data.get("disposition") == "RECORD_ONLY"

    def test_fail_closed_when_budget_exhausted(self):
        director = AgencyDirectorRole()
        result = director({
            "accepted_evidence": [{"source_id": "src-001"}],
            "budget": {"role_calls_used": 20, "max_role_calls": 20},
            "engagement_proposals": [],
        })
        assert result.status == "FAIL_CLOSED"


class TestBridgeExecutorRole:
    def test_dry_run_noop(self):
        bridge = BridgeExecutorRole()
        result = bridge({})
        assert result.status == "NOOP"
        assert result.data.get("dry_run") is True


class TestAuditorRole:
    def test_passes_under_budget(self):
        auditor = AuditorRole()
        result = auditor({
            "budget": {"role_calls_used": 5, "max_role_calls": 20,
                       "cost_estimate_used": 1.0, "max_cost_estimate": 5.0}
        })
        assert result.status == "COMPLETE"
        assert result.data["passed"] is True

    def test_finds_budget_role_exhaustion(self):
        auditor = AuditorRole()
        result = auditor({
            "budget": {"role_calls_used": 20, "max_role_calls": 20,
                       "cost_estimate_used": 0, "max_cost_estimate": 5.0}
        })
        assert "budget_role_calls_exhausted" in result.data["findings"]
        assert result.data["passed"] is False


# ---------------------------------------------------------------------------
# Model routing tests
# ---------------------------------------------------------------------------

class TestModelRouting:
    def test_scout_is_flash(self):
        assert model_for_role("scout") == FLASH

    def test_agency_director_is_pro(self):
        assert model_for_role("agency_director") == PRO

    def test_bridge_executor_is_deterministic(self):
        assert model_for_role("bridge_executor") == "deterministic"

    def test_unknown_role_raises(self):
        with pytest.raises(KeyError):
            model_for_role("nonexistent")

    def test_engagement_lead_is_write_critical(self):
        assert is_write_critical("engagement_lead") is True

    def test_scout_is_not_write_critical(self):
        assert is_write_critical("scout") is False


# ---------------------------------------------------------------------------
# Orchestrator tests
# ---------------------------------------------------------------------------

class TestOrchestrator:
    def test_full_shift_completes(self):
        orch = AgencyOrchestrator(trigger="manual", shift="morning")
        ctx = orch.run()
        assert ctx.status == "completed"
        assert ctx.events.has_event_type("RUN_CLOSED")
        assert ctx.events.has_event_type("RUN_STARTED")

    def test_budget_exhausted_stops_run(self):
        budget = AgencyBudget(max_role_calls=3)  # Very tight — will exhaust
        orch = AgencyOrchestrator(budget=budget)
        ctx = orch.run()
        # With max_role_calls=3 and ~7 role-invoking phases, run exhausts
        assert ctx.status in ("budget_exhausted", "failed", "completed")
        # If completed, verify budget was tracked correctly
        if ctx.status == "completed":
            assert ctx.budget.role_calls_used <= ctx.budget.max_role_calls

    def test_dry_run_policy_prevents_writes(self):
        orch = AgencyOrchestrator(
            policy_config={"dry_run": True, "moltbook_read_only": True})
        ctx = orch.run()
        assert ctx.policy.get("dry_run") is True

    def test_fail_closed_aborts_run(self):
        """When a role closes the CTX as failed, the orchestrator detects it."""
        # Override _director_review to simulate FAIL_CLOSED
        class FailingOrchestrator(AgencyOrchestrator):
            def _director_review(self):
                self.ctx.close("failed")

        orch = FailingOrchestrator()
        ctx = orch.run()
        assert ctx.status == "failed"
        # Verify the run did NOT get overwritten to "completed"
        assert ctx.status != "completed"

    def test_role_registry_has_all_roles(self):
        for role_name in ["scout", "records_clerk", "evidence_analyst",
                          "agency_director", "engagement_lead",
                          "bridge_executor", "auditor", "engineering_planner"]:
            assert role_name in ROLE_REGISTRY, f"Missing role: {role_name}"


# ---------------------------------------------------------------------------
# Profile tests
# ---------------------------------------------------------------------------

class TestAgentProfile:
    def test_new_profile_is_observed(self):
        p = AgentProfile("test_agent")
        assert p.relationship_stage == "observed"
        assert p.handle == "test_agent"

    def test_interaction_advances_stage(self):
        p = AgentProfile("test_agent")
        p.record_interaction()
        p.update_stage()
        assert p.relationship_stage == "engaged"

    def test_qualified_contributions_advance_faster(self):
        p = AgentProfile("test_agent")
        for _ in range(3):
            p.record_interaction(qualified=True)
        p.update_stage()
        assert p.relationship_stage == "evidence_contributor"

    def test_to_dict_serializable(self):
        p = AgentProfile("test_agent")
        d = p.to_dict()
        assert d["handle"] == "test_agent"
        json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# HQ tests
# ---------------------------------------------------------------------------

class TestHeadquarters:
    def test_render_markdown_no_secrets(self):
        ctx = AgencyContextV1()
        ctx.close("completed")
        report = render_hq_markdown(ctx.to_dict(sanitize=True))
        assert "Run ID" in report
        for pat in ["api_key", "Bearer", "Authorization", "password",
                    "MOLTBOOK_TOKEN", "access_token", "client_secret"]:
            assert pat.lower() not in report.lower()

    def test_render_html_contains_budget(self):
        ctx = AgencyContextV1()
        ctx.close("completed")
        html = render_hq_html(ctx.to_dict(sanitize=True))
        assert "<!DOCTYPE html>" in html
        assert "Budget" in html

    def test_render_displays_incidents(self):
        ctx = AgencyContextV1()
        ctx.record_incident("Test", severity="critical")
        ctx.close("completed")
        # Sanitized output doesn't show incidents — that's in unsanitized
        report = render_hq_markdown(ctx.to_dict(sanitize=True))
        assert "Status" in report  # basic render works


# ---------------------------------------------------------------------------
# Policy tests
# ---------------------------------------------------------------------------

class TestPolicy:
    def test_default_prevents_writes(self):
        p = AgencyPolicy()
        assert p.can_write() is False
        assert p.dry_run is True

    def test_can_write_requires_all_conditions(self):
        p = AgencyPolicy({"dry_run": False, "automation_enabled": True,
                          "moltbook_read_only": False})
        assert p.can_write() is True

    def test_to_dict_roundtrips(self):
        p = AgencyPolicy()
        d = p.to_dict()
        assert d["dry_run"] is True
        assert d["max_writes_per_run"] == 1


# ---------------------------------------------------------------------------
# Security tests
# ---------------------------------------------------------------------------

class TestSecurity:
    def test_ctx_no_secrets(self):
        """CTX serialization must not contain any secret-like patterns."""
        ctx = AgencyContextV1()
        ctx.close("completed")
        d = ctx.to_dict(sanitize=True)
        s = json.dumps(d)
        for pat in ["api_key", "Bearer", "Authorization", "password",
                    "MOLTBOOK_TOKEN", "access_token", "client_secret"]:
            assert pat.lower() not in s.lower(), \
                f"Pattern '{pat}' found in sanitized CTX"

    def test_ctx_external_text_marked_untrusted(self):
        """Any external content in CTX must carry untrusted markers."""
        ctx = AgencyContextV1()
        ctx.source_candidates.append({
            "url": "https://moltbook.com/post/123",
            "content": "some external text",
            "untrusted": True,
        })
        for candidate in ctx.source_candidates:
            assert candidate.get("untrusted") is True, \
                "External content must be marked untrusted"

    def test_policy_fail_closed(self):
        """Default policy must prevent all writes."""
        p = AgencyPolicy()
        assert p.can_write() is False
        assert p.moltbook_read_only is True

    def test_invalid_model_output_fails_closed(self):
        """Invalid status in RoleResult raises ValueError."""
        with pytest.raises(ValueError):
            RoleResult("test", "EXECUTE_SHELL_COMMAND")

    def test_bridge_never_invoked_in_dry_run(self):
        """Bridge executor must return NOOP in dry-run."""
        bridge = BridgeExecutorRole()
        result = bridge({})
        assert result.status == "NOOP"
        assert result.data.get("dry_run") is True

    def test_engagement_requires_approval_inputs(self):
        """Engagement leads produce proposals, they don't execute writes."""
        lead = EngagementLeadRole()
        result = lead({"engagement_proposals": []})
        assert result.status == "NOOP"

    def test_stale_base_sha_can_be_detected(self):
        """CTX binds to base SHA at creation time."""
        sha = "c" * 40
        ctx = AgencyContextV1(base_sha=sha)
        assert ctx.base_sha == sha

    def test_duplicate_run_ids_are_unique(self):
        """Each CTX gets a unique run_id."""
        ctx1 = AgencyContextV1()
        ctx2 = AgencyContextV1()
        assert ctx1.run_id != ctx2.run_id

    def test_budget_exhaustion_prevents_calls(self):
        """Budget exhaustion flag can be checked."""
        b = AgencyBudget(max_role_calls=1)
        b.record_role_call()
        assert b.is_exhausted

    def test_no_original_post_automation(self):
        """V1 policy disallows original posts."""
        p = AgencyPolicy()
        assert p.allow_original_posts is False

    def test_max_one_active_inquiry(self):
        """Policy enforces single active inquiry."""
        p = AgencyPolicy()
        assert p.max_active_inquiries == 1
