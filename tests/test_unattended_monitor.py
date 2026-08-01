"""Tests for B001 unattended thread monitoring.

Covers: evidence index comment_id handling, Scout internal-content
filtering, deterministic NOOP fast-path, and the synthesis stop-date gate.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agency.context import RepoStateProvider
from agency.evidence_index import load_evidence_index
from agency.orchestrator import AgencyOrchestrator, build_role_registry
from agency.roles import RoleResult, ScoutRole


def _make_sha(s="t"):
    return hashlib.sha1(s.encode()).hexdigest()


def _fixed_provider(sha):
    class P(RepoStateProvider):
        def current_sha(self): return sha
        def origin_main_sha(self): return sha
    return P()


class _MockReadClient:
    """Minimal Moltbook read client for Scout tests."""

    def __init__(self, post=None, comments=None):
        self._post = post
        self._comments = comments or []

    def fetch_post(self, post_id):
        return {"post": self._post} if self._post else {"post": {}}

    def fetch_comments(self, post_id):
        return {"comments": self._comments}


# ---------------------------------------------------------------------------
# Evidence index
# ---------------------------------------------------------------------------

class TestEvidenceIndexCommentId:

    def test_post_record_indexes_parent_id(self, tmp_path):
        records = tmp_path / "sources" / "records"
        records.mkdir(parents=True)
        (records / "src-b001-009.json").write_text(json.dumps({
            "source_id": "src-b001-009",
            "url": "https://www.moltbook.com/post/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "content_type": "post",
            "inquiry_ids": ["B001"],
        }))
        ids = load_evidence_index(tmp_path)
        assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in ids

    def test_comment_record_indexes_parent_and_comment_id(self, tmp_path):
        records = tmp_path / "sources" / "records"
        records.mkdir(parents=True)
        (records / "src-b001-009.json").write_text(json.dumps({
            "source_id": "src-b001-009",
            "url": "https://www.moltbook.com/post/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "content_type": "comment",
            "comment_id": "11111111-2222-3333-4444-555555555555",
            "inquiry_ids": ["B001"],
        }))
        ids = load_evidence_index(tmp_path)
        assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in ids
        assert "11111111-2222-3333-4444-555555555555" in ids

    def test_malformed_comment_id_fails_closed(self, tmp_path):
        records = tmp_path / "sources" / "records"
        records.mkdir(parents=True)
        (records / "src-b001-009.json").write_text(json.dumps({
            "source_id": "src-b001-009",
            "url": "https://www.moltbook.com/post/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "content_type": "comment",
            "comment_id": "not-a-valid-uuid!",
            "inquiry_ids": ["B001"],
        }))
        with pytest.raises(ValueError):
            load_evidence_index(tmp_path)


# ---------------------------------------------------------------------------
# Scout internal filtering + dedup
# ---------------------------------------------------------------------------

class TestScoutFiltering:

    def _scout_view(self, internal=None, known=None):
        return {
            "inbox": [],
            "known_ids": known or [],
            "campaign": {
                "active_inquiry": "t",
                "internal_author_handles": internal or ["hermes-sankhya-25"],
            },
        }

    def test_internal_hermes_post_ignored(self):
        reader = _MockReadClient(
            post={"id": "post-1", "content": "post",
                  "author": {"name": "hermes-sankhya-25"}},
            comments=[],
        )
        scout = ScoutRole(moltbook=reader)
        result = scout(self._scout_view())
        assert result.status == "NOOP"

    def test_internal_hermes_comment_ignored(self):
        reader = _MockReadClient(
            post={"id": "post-1", "content": "post",
                  "author": {"name": "external_author"}},
            comments=[{"id": "c-int", "content": "internal note",
                       "author": {"name": "hermes-sankhya-25"}}],
        )
        scout = ScoutRole(moltbook=reader)
        result = scout(self._scout_view())
        # External post is a legitimate candidate; internal comment excluded
        assert result.status == "COMPLETE"
        ids = [c["id"] for c in result.data.get("candidates", [])]
        assert "post-1" in ids
        assert "c-int" not in ids

    def test_known_external_comment_id_ignored(self):
        reader = _MockReadClient(
            post={"id": "post-1", "content": "post",
                  "author": {"name": "hermes-sankhya-25"}},
            comments=[{"id": "11111111-2222-3333-4444-555555555555",
                       "content": "known external comment",
                       "author": {"name": "vantik"}}],
        )
        scout = ScoutRole(moltbook=reader)
        result = scout(self._scout_view(known=["11111111-2222-3333-4444-555555555555"]))
        assert result.status == "NOOP"

    def test_unknown_external_comment_is_single_candidate(self):
        reader = _MockReadClient(
            post={"id": "post-1", "content": "post",
                  "author": {"name": "hermes-sankhya-25"}},
            comments=[
                {"id": "c-int", "content": "internal",
                 "author": {"name": "hermes-sankhya-25"}},
                {"id": "c-known", "content": "known",
                 "author": {"name": "vantik"}},
                {"id": "c-new", "content": "brand new external comment",
                 "author": {"name": "new_contributor"}},
            ],
        )
        scout = ScoutRole(moltbook=reader)
        result = scout(self._scout_view(known=["c-known"]))
        assert result.status == "COMPLETE"
        candidates = result.data.get("candidates", [])
        assert len(candidates) == 1
        assert candidates[0]["id"] == "c-new"
        assert candidates[0]["author_handle"] == "new_contributor"


# ---------------------------------------------------------------------------
# NOOP fast-path
# ---------------------------------------------------------------------------

class TestNoopFastPath:

    def test_no_new_evidence_run_ends_after_scout(self):
        sha = _make_sha("noop")
        reader = _MockReadClient(
            post={"id": "post-1", "content": "post",
                  "author": {"name": "hermes-sankhya-25"}},
            comments=[{"id": "11111111-2222-3333-4444-555555555555",
                       "content": "known", "author": {"name": "vantik"}}],
        )
        reg = build_role_registry(moltbook_reader=reader)
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_fixed_provider(sha),
                                  role_registry=reg,
                                  campaign={"active_inquiry": "t",
                                            "objective": "O",
                                            "internal_author_handles": ["hermes-sankhya-25"]})
        orch.ctx.set_evidence_index({"11111111-2222-3333-4444-555555555555"})
        ctx = orch.run()
        assert ctx.status == "completed"
        types = [e["event_type"] for e in ctx.events.to_list()]
        # Scout ROLE_COMPLETED present; no EA/Director ROLE_COMPLETED
        assert types.count("ROLE_COMPLETED") == 1
        assert "DIRECTOR_DECISION" not in types
        assert ctx.decisions == []
        assert ctx.accepted_evidence == []

    def test_noop_path_zero_model_calls_and_zero_tokens(self):
        sha = _make_sha("noop2")
        reader = _MockReadClient(
            post={"id": "post-1", "content": "post",
                  "author": {"name": "hermes-sankhya-25"}},
            comments=[{"id": "11111111-2222-3333-4444-555555555555",
                       "content": "known", "author": {"name": "vantik"}}],
        )
        reg = build_role_registry(moltbook_reader=reader)
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_fixed_provider(sha),
                                  role_registry=reg,
                                  campaign={"active_inquiry": "t",
                                            "objective": "O",
                                            "internal_author_handles": ["hermes-sankhya-25"]})
        orch.ctx.set_evidence_index({"11111111-2222-3333-4444-555555555555"})
        ctx = orch.run()
        assert ctx.status == "completed"
        assert ctx.budget.tokens_used == 0
        assert ctx.budget.cost_estimate_used == 0.0
        # Deterministic Scout invocation is permitted; no model roles ran.
        assert ctx.budget.role_calls_used <= 1
        assert len(ctx.transactions) == 0
        assert len(ctx.incidents) == 0


# ---------------------------------------------------------------------------
# Synthesis stop-date gate
# ---------------------------------------------------------------------------

_OBJECTIVE = "Which claims, proposals, questions, and warnings about independently verifiable agent work receipts appear in the target Moltbook discussion, who made them, and what does the discussion actually establish?"


def _run_gate(date_provider, stop_date="2026-08-09", allow_early=False):
    """Run a READY_FOR_SYNTHESIS Director through the gate with a fixed date."""
    sha = _make_sha("gate")

    class Director(ScoutRole):  # reuse RoleResult import path
        pass

    from agency.roles import AgencyDirectorRole

    class GateDirector(AgencyDirectorRole):
        def __call__(self, ctx_view):
            return RoleResult("agency_director", "COMPLETE", data={
                "decision_id": "d1", "disposition": "READY_FOR_SYNTHESIS",
                "director_run_id": "r1", "timestamp": "2026-08-01T00:00:00Z",
                "rationale": "test",
            })

    reg = build_role_registry()
    reg["agency_director"] = GateDirector()
    orch = AgencyOrchestrator(
        base_sha=sha, repo_provider=_fixed_provider(sha), role_registry=reg,
        campaign={"active_inquiry": "t", "objective": _OBJECTIVE,
                  "internal_author_handles": ["hermes-sankhya-25"],
                  "active_inquiry_stop_date": stop_date,
                  "allow_early_synthesis": allow_early},
        utc_date_provider=date_provider)
    # Give the pipeline one candidate so it reaches the Director
    orch.ctx.add_inbox([{"id": "c1", "url": "https://x.com/c1",
                         "author_handle": "ext", "untrusted": True,
                         "content_excerpt": "Evidence.",
                         "raw_content": "Evidence."}])
    ctx = orch.run()
    if ctx.decisions:
        return ctx.decisions[0]["disposition"]
    return None


class TestSynthesisGate:

    def test_before_stop_date_downgraded_to_record_only(self):
        disp = _run_gate(lambda: __import__("datetime").date(2026, 8, 1))
        assert disp == "RECORD_ONLY"

    def test_on_stop_date_synthesis_allowed(self):
        disp = _run_gate(lambda: __import__("datetime").date(2026, 8, 9))
        assert disp == "READY_FOR_SYNTHESIS"

    def test_after_stop_date_synthesis_allowed(self):
        disp = _run_gate(lambda: __import__("datetime").date(2026, 8, 10))
        assert disp == "READY_FOR_SYNTHESIS"

    def test_allow_early_synthesis_allows_early(self):
        disp = _run_gate(lambda: __import__("datetime").date(2026, 8, 1),
                         allow_early=True)
        assert disp == "READY_FOR_SYNTHESIS"


# ---------------------------------------------------------------------------
# Workflow + security boundaries
# ---------------------------------------------------------------------------

class TestWorkflowAndSecurity:

    def test_workflow_dispatch_ref_not_overridden(self):
        wf = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "moltbook-agency-observe.yml"
        text = wf.read_text()
        # The checkout step must not hardcode ref: main
        assert "ref: main" not in text
        assert "workflow_dispatch" in text
        # Security boundaries preserved
        assert "contents: read" in text
        assert "issues: write" in text
        assert "actions: read" in text
        assert "contents: write" not in text
        assert "pull-requests: write" not in text

    def test_observe_security_boundaries_unchanged(self):
        cfg = Path(__file__).resolve().parents[1] / "config" / "moltbook_agency.toml"
        text = cfg.read_text()
        assert "moltbook_read_only = true" in text
        assert "allow_original_posts = false" in text
        assert "max_active_inquiries = 1" in text
        # Stop-date gate config present
        assert "active_inquiry_stop_date = \"2026-08-09\"" in text
        assert "allow_early_synthesis = false" in text
