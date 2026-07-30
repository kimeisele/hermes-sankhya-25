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
from agency.roles import (RoleResult, ScoutRole, RecordsClerkRole,
                         EvidenceAnalystRole, AgencyDirectorRole)
from agency.orchestrator import AgencyOrchestrator, build_role_registry
from agency.hq import render_hq_markdown

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
# Epistemic hardening tests + restored regression tests
# ---------------------------------------------------------------------------

_OBJECTIVE = "What claims and proposals appear in the discussion?"


def _ev_resp(accepted_list, rejected=None):
    """Accepted items must use span_ids format (no claim_text)."""
    return {"choices": [{"message": {"content": json.dumps({
        "accepted": accepted_list, "rejected": rejected or [],
    })}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def _dir_resp(disposition, synthesis=None):
    """Director response — synthesis source_quotes must NOT contain quote."""
    base = {"decision_id": "d1", "disposition": disposition,
            "director_run_id": "r1", "timestamp": "2026-01-01T00:00:00Z",
            "rationale": "test"}
    if synthesis is not None:
        base["synthesis"] = synthesis
    return {"choices": [{"message": {"content": json.dumps(base)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def _make_epi_canonical(sid, author, excerpt):
    return {"id": sid, "url": f"https://m.example/{sid}",
            "author_handle": author, "content_type": "comment",
            "untrusted": True, "content_excerpt": excerpt}


def _ea_span_ids(source_id, count=1):
    """Build span_ids list for a source — each source has one span 'span/0'."""
    return [f"{source_id}/span/{i}" for i in range(count)]


def _make_finding(fid, statement, src_ids, kind, quotes, reasoning="R"):
    """quotes is a list of {source_id, claim_id} — NO quote field (code injects)."""
    return {"finding_id": fid, "statement": statement,
            "source_ids": src_ids, "finding_kind": kind,
            "source_quotes": quotes, "reasoning": reasoning}


def _run_epi(evidence_accepted, director_resp, canonical_items,
             call_log=None, internal_handles=None):
    """Run epistemic test with span-based EA output.

    evidence_accepted items must use span_ids (not claim_text).
    Director source_quotes must NOT contain quote.
    """
    from agency.model_client import DeepSeekClient
    if call_log is None:
        call_log = []
    if internal_handles is None:
        internal_handles = ["hermes-sankhya-25"]

    class _Tx:
        def __call__(self, payload):
            call_log.append(payload.get("model", ""))
            msgs = payload.get("messages", [])
            sys = msgs[0]["content"] if msgs else ""
            if "extract claims" in sys.lower() or "classify" in sys.lower():
                # Include a rejected entry for the fixture post (id "t")
                # which is always present as a model-facing source.
                rejected = [{"source_id": "t",
                             "reason": "fixture post outside canonical evidence set"}]
                return _ev_resp(evidence_accepted, rejected=rejected)
            return director_resp

    client = DeepSeekClient(transport=_Tx())

    class _R:
        def fetch_post(self, pid):
            return {
                "post": {
                    "id": pid,
                    "content": "body",
                    "author": {"name": "op"},
                }
            }
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
        "objective": _OBJECTIVE, "internal_author_handles": internal_handles})
    orch.ctx.set_evidence_index(set())
    return orch.run()


class TestEpistemicHardening:
    """Tests for claim-level provenance, deterministic metadata, schema boundaries."""

    # ── A1: schema split — model schema rejects deterministic fields ──
    def test_a1_model_schema_rejects_deterministic_fields(self):
        """Raw model output with source_basis etc → schema invalid."""
        from agency.model_client import validate_against_schema
        import copy as _copy
        sd = Path(__file__).resolve().parents[2] / "schemas"
        dec_schema = json.loads((sd / "agency-decision-v1.schema.json").read_text())
        # Derive model schema (same as build_role_registry)
        model_schema = _copy.deepcopy(dec_schema)
        mf = model_schema["properties"]["synthesis"]["properties"]["findings"]["items"]
        for fld in ("source_basis", "distinct_author_count", "distinct_external_author_count"):
            mf["properties"].pop(fld, None)
            if fld in mf["required"]:
                mf["required"].remove(fld)
        # Valid raw output without det fields
        raw_dec = {"decision_id": "d1", "disposition": "READY_FOR_SYNTHESIS",
            "director_run_id": "r1", "timestamp": "2026-01-01T00:00:00Z",
            "rationale": "x", "synthesis": {"inquiry": _OBJECTIVE,
                "executive_answer": "A", "findings": [
                    {"finding_id": "f1", "statement": "S", "source_ids": ["s"],
                     "finding_kind": "assertion", "reasoning": "R",
                     "source_quotes": [{"source_id": "s", "claim_id": "c", "quote": "q"}],
                     }], "unresolved_questions": []}}
        assert validate_against_schema(raw_dec, model_schema) == []
        # Raw with deterministic field fails
        raw_dec["synthesis"]["findings"][0]["source_basis"] = "external"
        assert len(validate_against_schema(raw_dec, model_schema)) > 0
        # Durable schema requires the field
        assert len(validate_against_schema(raw_dec, dec_schema)) > 0  # extra field in model output
        # Enriched (has det fields) passes durable schema
        enriched = _copy.deepcopy(raw_dec)
        enriched["synthesis"]["findings"][0]["distinct_author_count"] = 1
        enriched["synthesis"]["findings"][0]["distinct_external_author_count"] = 1
        assert validate_against_schema(enriched, dec_schema) == []
        # Removing a det field breaks durable schema
        del enriched["synthesis"]["findings"][0]["source_basis"]
        assert len(validate_against_schema(enriched, dec_schema)) > 0

    # ── A2: canonical rehydration ──
    def test_a2_canonical_rehydration(self):
        sid = "src-a2"
        real_author = "vantik"
        real_excerpt = "REAL EXCERPT from vantik."
        canonical = [_make_epi_canonical(sid, real_author, real_excerpt)]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids(sid)}]
        ctx = _run_epi(evidence, _dir_resp("RECORD_ONLY"), canonical)
        assert ctx.status == "completed"
        ev = next(e for e in ctx.accepted_evidence if e["source_id"] == sid)
        assert ev["author_handle"] == real_author
        assert ev["content_excerpt"] == real_excerpt
        assert ev["source_class"] == "external"

    # ── B: unknown accepted source + Director not called ──
    def test_b_unknown_accepted_source(self):
        call_log = []
        evidence = [{"source_id": "FAKE-SRC", "claim_id": "c1", "claim_kind": "assertion", "span_ids": ["FAKE-SRC/span/0"]}]
        canonical = [_make_epi_canonical("real-src", "vantik", "real")]
        ctx = _run_epi(evidence, _dir_resp("RECORD_ONLY"), canonical, call_log=call_log)
        assert ctx.status == "failed"
        assert len(ctx.incidents) >= 1
        assert len(ctx.accepted_evidence) == 0
        flash_calls = [m for m in call_log if "pro" not in m.lower()]
        pro_calls = [m for m in call_log if "pro" in m.lower()]
        assert len(flash_calls) >= 1
        assert len(pro_calls) == 0
        assert len(ctx.decisions) == 0

    # ── C: real inquiry required ──
    def test_c_inquiry_mismatch_fails_closed(self):
        sid = "src-c"
        canonical = [_make_epi_canonical(sid, "vantik", "text.")]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids(sid)}]
        syn = {"inquiry": "fd2c8049-5a16-417b-ab5d-8400a80d3ca7",
               "executive_answer": "A", "findings": [], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "failed"
        assert len(ctx.incidents) >= 1
        assert len(ctx.decisions) == 0

    # ── D: question → assertion conversion fails closed ──
    def test_d_question_to_assertion_fails(self):
        sid = "src-q"
        canonical = [_make_epi_canonical(sid, "hermes-sankhya-25", "Should we bind the receipt?")]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "question", "span_ids": _ea_span_ids(sid)}]
        f = _make_finding("f1", "Binding is required.", [sid], "assertion",  # finding_kind=assertion
            [{"source_id": sid, "claim_id": "c1"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "failed"
        assert any("finding_kind" in i["description"].lower() or
                   "claim kind" in i["description"].lower() for i in ctx.incidents)

    # ── E: mixed claim kinds in same finding fails closed ──
    def test_e_mixed_claim_kinds_fails(self):
        sid1, sid2 = "src-a", "src-q"
        canonical = [_make_epi_canonical(sid1, "vantik", "Commit hash is essential."),
                     _make_epi_canonical(sid2, "vantik", "Should receipts be universal?")]
        evidence = [{"source_id": sid1, "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids(sid1)},
                    {"source_id": sid2, "claim_id": "c2", "claim_kind": "question", "span_ids": _ea_span_ids(sid2)}]
        f = _make_finding("f1", "Mixed finding.", [sid1, sid2], "assertion",
            [{"source_id": sid1, "claim_id": "c1"},
             {"source_id": sid2, "claim_id": "c2"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "failed"
        incident_text = " ".join(i["description"].lower() for i in ctx.incidents)
        assert ("mixed claim kinds" in incident_text or
                "finding_kind" in incident_text or
                "claim kind" in incident_text), (
            f"Expected claim-kind incident, got: {ctx.incidents}")

    # ── F: unknown author counts ──
    def test_f_unknown_author_counts_zero(self):
        sid = "src-u"
        canonical = {"id": sid, "url": f"https://m.example/{sid}",
                      "author_handle": "", "content_type": "comment",
                      "untrusted": True, "content_excerpt": "text."}
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids(sid)}]
        ctx = _run_epi(evidence, _dir_resp("RECORD_ONLY"), [canonical])
        assert ctx.status == "completed"
        ev = next(e for e in ctx.accepted_evidence if e["source_id"] == sid)
        assert ev["source_class"] == "unknown"

    def test_f2_unknown_plus_external_counts(self):
        sid_u = "src-u"
        sid_e = "src-e"
        canonical = [
            {"id": sid_u, "url": f"https://m.example/{sid_u}",
             "author_handle": "", "content_type": "comment",
             "untrusted": True, "content_excerpt": "unknown text."},
            {"id": sid_e, "url": f"https://m.example/{sid_e}",
             "author_handle": "vantik", "content_type": "comment",
             "untrusted": True, "content_excerpt": "external text."},
        ]
        evidence = [
            {"source_id": sid_u, "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids(sid_u)},
            {"source_id": sid_e, "claim_id": "c2", "claim_kind": "assertion", "span_ids": _ea_span_ids(sid_e)},
        ]
        f = _make_finding("f1", "Mix.", [sid_u, sid_e], "assertion",
            [{"source_id": sid_u, "claim_id": "c1"},
             {"source_id": sid_e, "claim_id": "c2"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "completed"
        f1 = ctx.decisions[0]["synthesis"]["findings"][0]
        assert f1["distinct_author_count"] == 1   # only vantik (unknown excluded)
        assert f1["distinct_external_author_count"] == 1

    # ── G: long quote + deterministic full text ──
    def test_g_long_quote_no_truncation(self):
        sid = "src-long"
        long_quote = "AAAA" + ("X" * 250) + "SENTINEL-LONG-END"
        canonical = [_make_epi_canonical(sid, "vantik", long_quote)]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids(sid)}]
        f = _make_finding("f1", "Long test.", [sid], "assertion",
            [{"source_id": sid, "claim_id": "c1"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "completed"
        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert "SENTINEL-LONG-END" in report

    # ── H: enriched decision validates against committed schema ──
    def test_h_enriched_decision_validates_against_schema(self):
        sid = "src-h"
        canonical = [_make_epi_canonical(sid, "vantik", "Enriched schema test text.")]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids(sid)}]
        f = _make_finding("f1", "Schema validated.", [sid], "assertion",
            [{"source_id": sid, "claim_id": "c1"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "completed"
        assert len(ctx.decisions) == 1
        sd = Path(__file__).resolve().parents[2] / "schemas"
        dec_schema = json.loads((sd / "agency-decision-v1.schema.json").read_text())
        import jsonschema as _js
        _js.validate(instance=ctx.decisions[0], schema=dec_schema)

    # ── I: Director-supplied quote rejected ──
    def test_i_bad_quote_fails(self):
        sid = "src-i"
        canonical = [_make_epi_canonical(sid, "vantik", "exact canonical text here.")]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids(sid)}]
        # Director provides its own quote — must be rejected
        f = {"finding_id": "f1", "statement": "Bad.", "source_ids": [sid],
             "finding_kind": "assertion", "reasoning": "R",
             "source_quotes": [{"source_id": sid, "claim_id": "c1", "quote": "DIRECTOR WROTE THIS"}]}
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "failed"
        assert len(ctx.incidents) >= 1
        assert any("agency_director" in i["description"] and
                   ("quote" in i["description"].lower() or
                    "additional" in i["description"].lower())
                   for i in ctx.incidents), f"Expected Director quote rejection, got: {ctx.incidents}"

    # ── J: successful mixed synthesis, all same kind ──
    def test_j_successful_mixed_synthesis(self):
        s_int, s_e1, s_e2 = "src-int", "src-ext1", "src-ext2"
        ie = "One follow-up question: what binds the receipt to work?"
        e1e = "The minimum receipt needs commit_hash and timestamp as core fields."
        e2e = "I assert that test_run_id with pass/fail counts is also required."
        canonical = [
            _make_epi_canonical(s_int, "hermes-sankhya-25", ie),
            _make_epi_canonical(s_e1, "vantik", e1e),
            _make_epi_canonical(s_e2, "contributor_b", e2e),
        ]
        evidence = [
            {"source_id": s_int, "claim_id": "c-int", "claim_kind": "assertion", "span_ids": _ea_span_ids(s_int)},
            {"source_id": s_e1, "claim_id": "c-e1", "claim_kind": "assertion", "span_ids": _ea_span_ids(s_e1)},
            {"source_id": s_e2, "claim_id": "c-e2", "claim_kind": "assertion", "span_ids": _ea_span_ids(s_e2)},
        ]
        call_log = []
        f = _make_finding("f1", "Receipt fields and binding.", [s_e1, s_e2, s_int],
            "assertion",
            [{"source_id": s_e1, "claim_id": "c-e1"},
             {"source_id": s_e2, "claim_id": "c-e2"},
             {"source_id": s_int, "claim_id": "c-int"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "Receipt fields proposed.",
               "findings": [f], "unresolved_questions": ["What binds receipt?"]}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical, call_log=call_log)
        assert ctx.status == "completed"
        f1 = ctx.decisions[0]["synthesis"]["findings"][0]
        assert f1["source_basis"] == "mixed"
        assert f1["distinct_author_count"] == 3
        assert f1["distinct_external_author_count"] == 2
        sd = Path(__file__).resolve().parents[2] / "schemas"
        dec_schema = json.loads((sd / "agency-decision-v1.schema.json").read_text())
        import jsonschema as _js
        _js.validate(instance=ctx.decisions[0], schema=dec_schema)
        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert "## Research Synthesis" in report
        assert "commit_hash and timestamp" in report
        assert "Source basis: mixed" in report
        assert "Distinct authors: 3" in report
        assert "Distinct external authors: 2" in report
        flash = [m for m in call_log if "pro" not in m.lower()]
        pro = [m for m in call_log if "pro" in m.lower()]
        assert len(flash) == 1
        assert len(pro) == 1
        assert len(ctx.transactions) == 0

    # ── K: schema rejection ──
    def test_k_schema_rejection(self):
        from agency.model_client import validate_against_schema
        sd = Path(__file__).resolve().parents[2] / "schemas"
        ev_schema = json.loads((sd / "evidence-analysis-output.schema.json").read_text())
        dec_schema = json.loads((sd / "agency-decision-v1.schema.json").read_text())
        assert len(validate_against_schema({"accepted": [
            {"source_id": "x", "claim_id": "c1", "claim_text": "X"}], "rejected": []}, ev_schema)) > 0
        assert len(validate_against_schema({"accepted": [
            {"source_id": "x", "claim_id": "c1", "claim_kind": "invalid", "claim_text": "X"}],
            "rejected": []}, ev_schema)) > 0
        assert len(validate_against_schema({"accepted": [
            {"source_id": "x", "claim_id": "c1", "claim_kind": "assertion", "claim_text": "X",
             "extra": True}], "rejected": []}, ev_schema)) > 0
        bf = {"finding_id": "f1", "statement": "S", "source_ids": ["s"],
              "finding_kind": "assertion", "reasoning": "R"}
        bs = {"inquiry": _OBJECTIVE, "executive_answer": "A", "findings": [bf], "unresolved_questions": []}
        bd = {"decision_id": "d1", "disposition": "READY_FOR_SYNTHESIS",
              "director_run_id": "r1", "timestamp": "2026-01-01T00:00:00Z",
              "rationale": "x", "synthesis": bs}
        assert len(validate_against_schema(bd, dec_schema)) > 0
        sn = {"inquiry": _OBJECTIVE, "executive_answer": "A", "findings": [],
              "unresolved_questions": [], "next_inquiry": "N"}
        dn = dict(bd)
        dn["synthesis"] = sn
        assert len(validate_against_schema(dn, dec_schema)) > 0


# ---------------------------------------------------------------------------
# Restored regression tests (adapted for hardened schemas)
# ---------------------------------------------------------------------------

class TestEvidenceLifecycle:
    def test_inbox_becomes_accepted_evidence(self):
        sha = _make_sha()
        sentinel = "EXACT-SENTINEL-for-deterministic-evidence-test"
        reg = build_role_registry()
        class _P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_P(),
                                  role_registry=reg)
        orch.ctx.add_inbox([{"id": "item1", "url": "https://x.com/p/1",
                             "author_handle": "test", "untrusted": True,
                             "content_excerpt": sentinel,
                             "raw_content": sentinel}])
        ctx = orch.run()
        assert ctx.status == "completed"
        assert len(ctx.accepted_evidence) >= 1
        ev = ctx.accepted_evidence[0]
        assert ev["content_excerpt"] == sentinel
        assert ev["claim_text"] == sentinel
        assert ev["claim_id"]
        assert ev["source_class"] == "external"

    def test_evidence_changes_director_disposition(self):
        sha = _make_sha()
        class _P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha
        reg = build_role_registry()
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_P(),
                                  role_registry=reg)
        orch.ctx.add_inbox([{"id": "item1", "url": "https://x.com/p/1",
                             "author_handle": "test", "untrusted": True,
                             "content_excerpt": "Valid evidence content.",
                             "raw_content": "Valid evidence content."}])
        ctx = orch.run()
        assert len(ctx.accepted_evidence) >= 1
        assert ctx.decisions[0]["disposition"] == "RECORD_ONLY"


class TestEvidenceLifecycleOriginal:
    def test_inbox_becomes_accepted_evidence(self):
        sha = _make_sha()
        reg = build_role_registry()
        class _P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_P(),
                                  role_registry=reg)
        orch.ctx.add_inbox([{"id": "item1", "url": "https://x.com/p/1",
                             "author_handle": "test", "untrusted": True,
                             "content_excerpt": "test content.",
                             "raw_content": "test content."}])
        ctx = orch.run()
        assert ctx.status == "completed"

    def test_evidence_changes_director_disposition(self):
        sha = _make_sha()
        class _P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha
        reg = build_role_registry()
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_P(),
                                  role_registry=reg)
        orch.ctx.add_inbox([{"id": "item1", "url": "https://x.com/p/1",
                             "author_handle": "test", "untrusted": True,
                             "content_excerpt": "Valid evidence.",
                             "raw_content": "Valid evidence."}])
        ctx = orch.run()
        assert len(ctx.decisions) > 0
        assert ctx.decisions[0]["disposition"] != "NOOP"


class TestFakeDeepSeekE2E:
    def test_observe_with_fake_transport(self):
        sha = _make_sha("fake-ds")
        call_log = []
        class _Tx:
            def __call__(self, payload):
                call_log.append({"model": payload.get("model"),
                    "has_schema": "Output JSON only" in payload.get("messages", [{}])[0].get("content","")})
                model = payload.get("model", "")
                if "pro" in model:
                    return {"choices": [{"message": {"content": json.dumps({
                        "decision_id": "d1", "disposition": "RECORD_ONLY",
                        "director_run_id": "r1", "timestamp": "2026-07-27T00:00:00Z",
                        "rationale": "New evidence found"})}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
                return {"choices": [{"message": {"content": json.dumps({
                    "accepted": [{"source_id": "new-claim-1", "claim_id": "c1",
                     "claim_kind": "assertion", "span_ids": ["new-claim-1/span/0"]}],
                    "rejected": [
                        {"source_id": "test-post",
                         "reason": "fixture post has no claim required by this test"},
                    ]})}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=_Tx())
        class _R:
            def fetch_post(self, pid):
                return {
                    "post": {
                        "id": pid,
                        "content": "Test content.",
                        "author": {"name": "post_author"},
                    }
                }
            def fetch_comments(self, pid):
                return {"comments": [{"id": "new-claim-1",
                    "content": "commit_hash is essential", "author": {"name": "test_agent"}}]}
        class _P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha
        reg = build_role_registry(client=client, moltbook_reader=_R())
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_P(), role_registry=reg,
            campaign={"active_inquiry": "test-post", "objective": "Test",
                      "internal_author_handles": ["hermes-sankhya-25"]})
        orch.ctx.set_evidence_index(set())
        ctx = orch.run()
        assert ctx.status == "completed"
        assert len(call_log) > 0
        for c in call_log:
            assert c["has_schema"]
        assert len(ctx.decisions) > 0
        assert ctx.decisions[0]["disposition"] == "RECORD_ONLY"
        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert ctx.run_id[:12] in report
        assert len(ctx.transactions) == 0
        from agency.validate_ctx import validate_sanitized_ctx
        assert len(validate_sanitized_ctx(d)) == 0


class TestDeterministicIngestion:
    def test_scout_and_clerk_never_invoke_model(self):
        sentinel = "UNIQUE-SENTINEL-abc123xyz"
        model_calls = []
        class _Tx:
            def __call__(self, payload):
                model_calls.append(payload.get("model", ""))
                return {"choices": [{"message": {"content": json.dumps({
                    "accepted": [], "rejected": []})}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}}
        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=_Tx())
        class _R:
            def fetch_post(self, pid):
                return {
                    "post": {
                        "id": pid,
                        "content": "body",
                        "author": {"name": "op"},
                    }
                }
            def fetch_comments(self, pid):
                return {"comments": [{"id": "c-1", "content": sentinel, "author": {"name": "vantik"}}]}
        reg = build_role_registry(client=client, moltbook_reader=_R())
        scout = reg["scout"]
        assert scout._adapter is None
        cb = len(model_calls)
        r = scout({"inbox": [], "known_ids": set(), "campaign": {"active_inquiry": "test-p"}})
        assert r.status == "COMPLETE"
        assert len(model_calls) == cb
        vantik = [c for c in r.data["candidates"] if c.get("author_handle") == "vantik"]
        assert len(vantik) == 1
        assert vantik[0]["content_excerpt"] == sentinel
        clerk = reg["records_clerk"]
        assert clerk._adapter is None
        cb2 = len(model_calls)
        r2 = clerk({"source_candidates": [vantik[0]]})
        assert r2.status == "COMPLETE"
        assert len(model_calls) == cb2
        assert r2.data["normalized"][0]["content_excerpt"] == sentinel
        evidence = reg["evidence_analyst"]
        assert evidence._adapter is not None
        cb3 = len(model_calls)
        r3 = evidence({"source_candidates": r2.data["normalized"]})
        assert r3.status == "COMPLETE"
        assert len(model_calls) > cb3


class TestDirectorSynthesis:
    def test_synthesis_stored_and_rendered(self):
        call_log = []
        accepted_src = "src-syn"
        canonical = [_make_epi_canonical(accepted_src, "vantik", "evidence text for " + accepted_src)]
        evidence = [{"source_id": accepted_src, "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids(accepted_src)}]
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "The answer is 42.",
               "findings": [_make_finding("f1", "Answer found.", [accepted_src], "assertion",
                   [{"source_id": accepted_src, "claim_id": "c1"}])],
               "unresolved_questions": ["Q?"]}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical, call_log=call_log)
        assert ctx.status == "completed"
        assert len(ctx.decisions) == 1
        assert ctx.decisions[0]["disposition"] == "READY_FOR_SYNTHESIS"
        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert "## Research Synthesis" in report
        assert "The answer is 42." in report
        models = [m for m in call_log if m]
        assert len([m for m in models if "pro" not in m.lower()]) == 1
        assert len([m for m in models if "pro" in m.lower()]) == 1
        assert len(ctx.transactions) == 0
        assert len(ctx.incidents) == 0
        assert "Next Inquiry" not in report

    def test_full_statement_and_reasoning_no_truncation(self):
        call_log = []
        accepted_src = "src-long"
        canonical = [_make_epi_canonical(accepted_src, "vantik", "evidence text for " + accepted_src)]
        evidence = [{"source_id": accepted_src, "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids(accepted_src)}]
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [_make_finding("f1", "A" * 150 + "STAT-END", [accepted_src], "assertion",
                   [{"source_id": accepted_src, "claim_id": "c1"}],
                   "B" * 250 + "REASON-END")],
               "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical, call_log=call_log)
        assert ctx.status == "completed"
        report = render_hq_markdown(ctx.to_dict(sanitize=True))
        assert "STAT-END" in report
        assert "REASON-END" in report

    def test_invalid_source_id_fails_closed(self):
        call_log = []
        canonical = [_make_epi_canonical("real-src", "vantik", "evidence text for real-src")]
        evidence = [{"source_id": "real-src", "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids("real-src")}]
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [_make_finding("f1", "S", ["FAKE-SRC"], "assertion",
                   [{"source_id": "FAKE-SRC", "claim_id": "c1", "quote": "x"}])],
               "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical, call_log=call_log)
        assert ctx.status == "failed"
        assert len(ctx.incidents) >= 1
        assert len(ctx.decisions) == 0
        assert "## Research Synthesis" not in render_hq_markdown(ctx.to_dict(sanitize=True))

    def test_record_only_with_bad_synthesis_fails_closed(self):
        call_log = []
        canonical = [_make_epi_canonical("real-src", "vantik", "evidence text for real-src")]
        evidence = [{"source_id": "real-src", "claim_id": "c1", "claim_kind": "assertion", "span_ids": _ea_span_ids("real-src")}]
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [_make_finding("f1", "S", ["FAKE-SRC"], "assertion",
                   [{"source_id": "FAKE-SRC", "claim_id": "c1"}])],
               "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("RECORD_ONLY", syn), canonical, call_log=call_log)
        assert ctx.status == "failed"
        assert len(ctx.incidents) >= 1
        assert len(ctx.decisions) == 0
        assert "## Research Synthesis" not in render_hq_markdown(ctx.to_dict(sanitize=True))


class TestDirectorFailClosed:
    def test_director_timeout_records_incident(self):
        sha = _make_sha("dir-fail")
        accepted_src = "src-df"
        raw_error = "DeepSeek timeout after 60s"
        class _Tx:
            cc = 0
            def __call__(self, payload):
                _Tx.cc += 1
                if _Tx.cc == 1:
                    return {"choices": [{"message": {"content": json.dumps({
                        "accepted": [{"source_id": accepted_src, "claim_id": "c1",
                         "claim_kind": "assertion", "span_ids": [f"{accepted_src}/span/0"]}],
                        "rejected": [
                            {"source_id": "t",
                             "reason": "fixture post has no claim required by this test"},
                        ]})}}],
                        "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}}
                raise RuntimeError(raw_error)
        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=_Tx())
        class _R:
            def fetch_post(self, pid):
                return {
                    "post": {
                        "id": pid,
                        "content": "post",
                        "author": {"name": "op"},
                    }
                }
            def fetch_comments(self, pid):
                return {"comments": [{"id": accepted_src, "content": "receipt evidence",
                         "author": {"name": "vantik"}}]}
        class _P(RepoStateProvider):
            def __init__(self, s): self.s = s
            def current_sha(self): return self.s
            def origin_main_sha(self): return self.s
        reg = build_role_registry(client=client, moltbook_reader=_R())
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_P(sha), role_registry=reg,
            campaign={"active_inquiry": "t", "objective": "T",
                      "internal_author_handles": ["hermes-sankhya-25"]})
        orch.ctx.set_evidence_index(set())
        ctx = orch.run()
        assert ctx.status == "failed"
        assert len(ctx.incidents) == 1
        inc = ctx.incidents[0]
        assert "agency_director" in inc["description"]
        assert raw_error in inc["description"]
        assert inc["severity"] == "high"
        assert len(ctx.decisions) == 0
        assert len(ctx.accepted_evidence) == 1
        assert ctx.accepted_evidence[0]["source_id"] == accepted_src
        d = ctx.to_dict(sanitize=True)
        report = render_hq_markdown(d)
        assert "## Incidents" in report
        assert raw_error in report
        assert "## Accepted Evidence" in report
        assert "## Research Synthesis" not in report
        assert len(ctx.transactions) == 0


# ---------------------------------------------------------------------------
# Director context: remove duplicate source_candidates
# ---------------------------------------------------------------------------

class TestDirectorContext:
    """Director view must exclude source_candidates (already present in
    accepted_evidence). Evidence Analyst must continue receiving them."""

    def test_director_view_excludes_source_candidates(self):
        sha = _make_sha()
        ctx = AgencyContextV1(
            base_sha=sha, trigger="manual", shift="morning",
            repository="test",
            campaign={"active_inquiry": "t", "objective": "Test",
                      "internal_author_handles": ["hermes-sankhya-25"]})
        ctx.set_source_candidates([{
            "id": "src-1",
            "url": "https://m.example/src-1",
            "author_handle": "vantik",
            "content_type": "comment",
            "untrusted": True,
            "content_excerpt": "CANONICAL_EXCERPT_FOR_TEST_12345",
        }])
        ctx.set_raw_source("src-1", "https://m.example/src-1", "vantik",
                           "comment", "CANONICAL_EXCERPT_FOR_TEST_12345")
        ctx.add_accepted_evidence([{
            "source_id": "src-1",
            "claim_id": "c1",
            "author_handle": "vantik",
            "content_type": "comment",
            "content_excerpt": "CANONICAL_EXCERPT_FOR_TEST_12345",
            "url": "https://m.example/src-1",
            "source_class": "external",
            "claim_kind": "assertion",
            "claim_text": "A claim.",
        }])

        director_view = ctx.view_for("agency_director")
        assert "source_candidates" not in director_view, (
            "Director view must not contain source_candidates")
        assert "accepted_evidence" in director_view
        assert len(director_view["accepted_evidence"]) == 1
        ev = director_view["accepted_evidence"][0]
        assert ev["claim_id"] == "c1"
        assert ev["claim_kind"] == "assertion"
        # Director view must not expose internal runtime fields
        assert "source_content_hash" not in ev
        assert "span_ids" not in ev
        assert "content_excerpt" not in ev

        ea_view = ctx.view_for("evidence_analyst")
        assert "sources" in ea_view, (
            "Evidence Analyst must receive sources")
        assert len(ea_view["sources"]) == 1
        assert ea_view["sources"][0]["source_id"] == "src-1"
        # EA view must not expose internal runtime fields
        assert "source_candidates" not in ea_view
        assert "raw_content" not in ea_view["sources"][0]
        assert "content_excerpt" not in ea_view["sources"][0]


# ---------------------------------------------------------------------------
# Evidence Analyst: empty model output must fail closed
# ---------------------------------------------------------------------------

class TestEAFailClosed:
    """Evidence Analyst with non-empty candidates must not silently
    convert an empty model response into NOOP."""

    # ── Test 1: empty model output fails closed ──
    def test_empty_model_output_fails_closed(self):
        from agency.roles import EvidenceAnalystRole
        from agency.model_client import RoleModelAdapter, DeepSeekClient

        model_calls = 0

        class _Tx:
            def __call__(self, payload):
                nonlocal model_calls
                model_calls += 1
                # Return no choices — triggers error_kind="empty"
                return {"choices": [],
                        "usage": {"prompt_tokens": 300, "completion_tokens": 21,
                                  "total_tokens": 321}}

        client = DeepSeekClient(transport=_Tx())
        schema = {"type": "object", "properties": {"accepted": {"type": "array"}}}
        adapter = RoleModelAdapter(client, "deepseek-v4-flash",
                                   "You extract claims.", schema)
        role = EvidenceAnalystRole(adapter=adapter)
        result = role({"source_candidates": [
            {"id": "src-1", "author_handle": "vantik", "untrusted": True,
             "content_excerpt": "Evidence text."},
        ]})

        assert model_calls == 2, f"Expected 2 model calls (1 + retry), got {model_calls}"
        assert result.status == "FAIL_CLOSED", f"Expected FAIL_CLOSED, got {result.status}"
        assert "Empty response" in result.fail_reason
        # Token estimate may be from last call (321) on retry

    # ── Test 2: orchestrator stops before Director ──
    def test_orchestrator_stops_before_director(self):
        """Empty EA response → failed status, incident, Director never called."""
        sha = _make_sha("ea-nodir")
        call_log = []

        class _Tx:
            def __call__(self, payload):
                call_log.append(payload.get("model", ""))
                msgs = payload.get("messages", [])
                sys = msgs[0]["content"] if msgs else ""
                # Evidence Analyst — empty response
                if "extract claims" in sys.lower() or "classify" in sys.lower():
                    return {"choices": [],
                            "usage": {"prompt_tokens": 300, "completion_tokens": 21,
                                      "total_tokens": 321}}
                # Director (should never be reached)
                return {"choices": [{"message": {"content": "{}"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}}

        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=_Tx())

        class _R:
            def fetch_post(self, pid):
                return {
                    "post": {
                        "id": pid,
                        "content": "post",
                        "author": {"name": "op"},
                    }
                }
            def fetch_comments(self, pid):
                return {"comments": [
                    {"id": "src-1", "content": "Evidence text.",
                     "author": {"name": "vantik"}},
                ]}

        class _P(RepoStateProvider):
            def __init__(self, s): self.s = s
            def current_sha(self): return self.s
            def origin_main_sha(self): return self.s

        reg = build_role_registry(client=client, moltbook_reader=_R())
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_P(sha),
            role_registry=reg, campaign={"active_inquiry": "t",
            "objective": "Test", "internal_author_handles": ["hermes-sankhya-25"]})
        orch.ctx.set_evidence_index(set())
        ctx = orch.run()

        assert ctx.status == "failed"
        assert len(ctx.incidents) >= 1
        inc_text = " ".join(i["description"].lower() for i in ctx.incidents)
        assert "evidence_analyst" in inc_text
        assert "empty response" in inc_text, f"Incident: {ctx.incidents}"
        assert len(ctx.accepted_evidence) == 0
        pro_calls = [m for m in call_log if "pro" in m.lower()]
        assert len(pro_calls) == 0, f"Director should not be called, got {len(pro_calls)} pro calls"
        assert len(ctx.decisions) == 0
        assert len(ctx.transactions) == 0

    # ── Test 3: legitimate no-input NOOP remains ──
    def test_legitimate_noop_remains(self):
        from agency.roles import EvidenceAnalystRole
        from agency.model_client import RoleModelAdapter, DeepSeekClient

        model_calls = 0

        class _Tx:
            def __call__(self, payload):
                nonlocal model_calls
                model_calls += 1
                return {"choices": [{"message": {"content": "{}"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}}

        client = DeepSeekClient(transport=_Tx())
        schema = {"type": "object", "properties": {"accepted": {"type": "array"}}}
        adapter = RoleModelAdapter(client, "deepseek-v4-flash",
                                   "You extract claims.", schema)
        role = EvidenceAnalystRole(adapter=adapter)
        result = role({"source_candidates": []})

        assert result.status == "NOOP", f"Expected NOOP, got {result.status}"
        assert model_calls == 0, f"Expected 0 model calls, got {model_calls}"


# ---------------------------------------------------------------------------
# Evidence Analyst: minimized model-facing schema
# ---------------------------------------------------------------------------

class TestEAContract:
    """The model-facing Evidence Analyst schema must only expose
    'accepted' and 'rejected'. The committed schema retains its
    broader definition."""

    def test_model_facing_schema_has_only_accepted_and_rejected(self):
        """Build the real registry and inspect the EA adapter schema."""
        from agency.model_client import DeepSeekClient

        class _Tx:
            def __call__(self, payload):
                return {"choices": [{"message": {"content": "{}"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}}

        client = DeepSeekClient(transport=_Tx())
        reg = build_role_registry(client=client)
        ea = reg["evidence_analyst"]
        schema = ea._adapter.output_schema

        assert set(schema["properties"].keys()) == {"accepted", "rejected"}, (
            f"Expected only accepted/rejected, got {sorted(schema['properties'].keys())}")
        assert schema["required"] == ["accepted", "rejected"]
        assert schema["additionalProperties"] is False

        # Accepted item contract unchanged
        acc_req = schema["properties"]["accepted"]["items"]["required"]
        assert set(acc_req) == {"source_id", "claim_id", "claim_kind", "span_ids"}
        acc_props = schema["properties"]["accepted"]["items"]["properties"]
        assert acc_props["claim_kind"]["enum"] == ["assertion", "opinion", "proposal",
                                                    "question", "warning", "unknown"]

        # Rejected item contract unchanged
        rej_req = schema["properties"]["rejected"]["items"]["required"]
        assert set(rej_req) == {"source_id", "reason"}

    def test_committed_schema_retains_broader_properties(self):
        """Load the committed schema file directly and prove it still has
        claims, scores, and rationale."""
        sd = Path(__file__).resolve().parents[2] / "schemas"
        committed = json.loads((sd / "evidence-analysis-output.schema.json").read_text())
        props = committed["properties"]
        for field in ("accepted", "rejected", "claims", "scores", "rationale"):
            assert field in props, f"Committed schema missing field: {field}"

    def test_valid_response_with_only_accepted_and_rejected(self):
        """A response with only accepted+rejected must succeed in one call."""
        model_calls = 0

        class _Tx:
            def __call__(self, payload):
                nonlocal model_calls
                model_calls += 1
                return {"choices": [{"message": {"content": json.dumps({
                    "accepted": [
                        {"source_id": "src-1", "claim_id": "claim-1",
                         "claim_kind": "assertion", "span_ids": ["src-1/span/0"]},
                    ],
                    "rejected": [],
                })}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                              "total_tokens": 150}}

        from agency.model_client import DeepSeekClient as _DS
        client_ds = _DS(transport=_Tx())
        reg = build_role_registry(client=client_ds)
        ea = reg["evidence_analyst"]

        result = ea({"source_candidates": [
            {"id": "src-1", "author_handle": "vantik", "untrusted": True,
             "content_excerpt": "Evidence text."},
        ]})

        assert result.status == "COMPLETE", (
            f"Expected COMPLETE, got {result.status}: {result.fail_reason}")
        assert model_calls == 1, f"Expected 1 call (no repair), got {model_calls}"
        acc = result.data.get("accepted", [])
        assert len(acc) == 1
        assert acc[0]["span_ids"] == ["src-1/span/0"]

    def test_extra_field_repairs_on_second_call(self):
        """First response has rationale (extra field → schema rejection).
        Second response is valid.  Two calls total, COMPLETE."""
        model_calls = 0

        class _Tx:
            def __call__(self, payload):
                nonlocal model_calls
                model_calls += 1
                if model_calls == 1:
                    # Extra field: rationale
                    return {"choices": [{"message": {"content": json.dumps({
                        "accepted": [
                            {"source_id": "src-1", "claim_id": "claim-1",
                             "claim_kind": "assertion", "span_ids": ["src-1/span/0"]},
                        ],
                        "rejected": [],
                        "rationale": "extra field",
                    })}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                  "total_tokens": 150}}
                else:
                    return {"choices": [{"message": {"content": json.dumps({
                        "accepted": [
                            {"source_id": "src-1", "claim_id": "claim-1",
                             "claim_kind": "assertion", "span_ids": ["src-1/span/0"]},
                        ],
                        "rejected": [],
                    })}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                  "total_tokens": 150}}

        from agency.model_client import DeepSeekClient as _DS
        client_ds = _DS(transport=_Tx())
        reg = build_role_registry(client=client_ds)
        ea = reg["evidence_analyst"]

        result = ea({"source_candidates": [
            {"id": "src-1", "author_handle": "vantik", "untrusted": True,
             "content_excerpt": "Evidence text."},
        ]})

        assert result.status == "COMPLETE", (
            f"Expected COMPLETE after repair, got {result.status}: {result.fail_reason}")
        assert model_calls == 2, f"Expected 2 calls (1 reject + 1 repair), got {model_calls}"


# ---------------------------------------------------------------------------
# Evidence Analyst: thinking mode disabled
# ---------------------------------------------------------------------------

class TestEAThinking:
    """Evidence Analyst must disable provider thinking mode.
    All other adapters must remain provider-default (None)."""

    def test_evidence_analyst_disables_thinking(self):
        """Initial call includes 'thinking': {'type': 'disabled'}."""
        payloads = []

        class _Tx:
            def __call__(self, payload):
                payloads.append(dict(payload))  # shallow copy
                return {"choices": [{"message": {"content": json.dumps({
                    "accepted": [
                        {"source_id": "s", "claim_id": "c",
                         "claim_kind": "assertion", "span_ids": ["s/span/0"]},
                    ],
                    "rejected": [],
                })}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=_Tx())
        reg = build_role_registry(client=client)
        ea = reg["evidence_analyst"]
        result = ea({"source_candidates": [
            {"id": "s", "author_handle": "vantik", "untrusted": True,
             "content_excerpt": "text."},
        ]})

        assert len(payloads) == 1, f"Expected 1 call, got {len(payloads)}"
        assert payloads[0]["thinking"] == {"type": "disabled"}
        assert result.status == "COMPLETE"

    def test_evidence_analyst_repair_keeps_thinking_disabled(self):
        """Repair call also carries 'thinking': {'type': 'disabled'}."""
        payloads = []

        class _Tx:
            def __call__(self, payload):
                payloads.append(dict(payload))
                if len(payloads) == 1:
                    return {"choices": [{"message": {"content": json.dumps({
                        "accepted": [], "rejected": [],
                        "rationale": "extra",
                    })}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                return {"choices": [{"message": {"content": json.dumps({
                    "accepted": [], "rejected": [],
                })}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=_Tx())
        reg = build_role_registry(client=client)
        ea = reg["evidence_analyst"]
        result = ea({"source_candidates": [
            {"id": "s", "author_handle": "vantik", "untrusted": True,
             "content_excerpt": "text."},
        ]})

        assert len(payloads) == 2, f"Expected 2 calls, got {len(payloads)}"
        assert all(p["thinking"] == {"type": "disabled"} for p in payloads)
        assert result.status == "COMPLETE"

    def test_only_evidence_analyst_and_director_force_thinking_mode(self):
        """EA and Director have thinking_enabled=False; all other adapters are None."""
        from agency.model_client import DeepSeekClient

        class _Tx:
            def __call__(self, payload):
                return {"choices": [{"message": {"content": "{}"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

        client = DeepSeekClient(transport=_Tx())
        reg = build_role_registry(client=client)

        # EA and Director have thinking_enabled=False; others are None
        for role_name, adapter in reg.items():
            if not hasattr(adapter, '_adapter') or adapter._adapter is None:
                continue
            if role_name in ("evidence_analyst", "agency_director"):
                assert adapter._adapter.thinking_enabled is False, (
                    f"{role_name} must have thinking_enabled=False, "
                    f"got {adapter._adapter.thinking_enabled}")
            else:
                assert adapter._adapter.thinking_enabled is None, (
                    f"{role_name} must have thinking_enabled=None, "
                    f"got {adapter._adapter.thinking_enabled}")
        # Auditor _pro_adapter must remain provider-default
        auditor = reg.get("auditor")
        assert auditor is not None
        assert auditor._pro_adapter.thinking_enabled is None

    def test_director_disables_thinking(self):
        """Director adapter includes 'thinking': {'type': 'disabled'}."""
        payloads = []

        class _Tx:
            def __call__(self, payload):
                payloads.append(dict(payload))
                return {"choices": [{"message": {"content": json.dumps({
                    "decision_id": "d1", "disposition": "RECORD_ONLY",
                    "director_run_id": "r1", "timestamp": "2026-01-01T00:00:00Z",
                    "rationale": "test",
                })}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=_Tx())
        reg = build_role_registry(client=client)
        director = reg["agency_director"]
        result = director({"accepted_evidence": [], "campaign": {"objective": "T"},
                           "budget": {"role_calls_used": 0, "max_role_calls": 20}})

        assert len(payloads) == 1, f"Expected 1 call, got {len(payloads)}"
        assert payloads[0]["thinking"] == {"type": "disabled"}
        assert result.status == "COMPLETE"

    def test_auditor_pro_adapter_invoke_omits_thinking(self):
        """Auditor's _pro_adapter.invoke() must not include 'thinking' in
        payload and must return success."""
        payloads = []

        class _Tx:
            def __call__(self, payload):
                payloads.append(dict(payload))
                return _dir_resp("RECORD_ONLY")

        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=_Tx())
        reg = build_role_registry(client=client)
        auditor = reg["auditor"]

        assert auditor._pro_adapter.thinking_enabled is None

        result = auditor._pro_adapter.invoke({
            "findings": ["test_finding"],
            "budget": {"role_calls_used": 1, "max_role_calls": 20},
            "campaign": {"objective": "Test"},
        })

        assert len(payloads) == 1
        assert "thinking" not in payloads[0]
        assert result.success is True


# ---------------------------------------------------------------------------
# Source-fidelity contract tests (span-based evidence)
# ---------------------------------------------------------------------------

class TestSourceFidelity:
    """Tests for the span-based source-fidelity contract."""

    def test_span_segmentation_recovers_raw_content(self):
        """Concatenating all span texts recovers original raw_content."""
        from agency.context import _segment_spans
        raw = "Hello world. **Bold text.** More content.\n\nNew paragraph here."
        spans = _segment_spans("src", "hash", raw)
        assert "".join(s.text for s in spans) == raw
        assert all(len(s.text) <= 1000 for s in spans)

    def test_paragraph_separator_on_previous_span(self):
        """\\n\\n belongs to the end of the previous paragraph, not the next."""
        from agency.context import _segment_spans
        raw = "First sentence.\n\nSecond sentence."
        spans = _segment_spans("src", "hash", raw)
        assert spans[0].text == "First sentence.\n\n"
        assert spans[1].text == "Second sentence."
        assert "".join(s.text for s in spans) == raw

    def test_delimiter_pure_newline_source(self):
        """Pure \\n\\n source → exactly one span."""
        from agency.context import _segment_spans
        raw = "\n\n"
        spans = _segment_spans("src", "hash", raw)
        assert len(spans) == 1
        assert spans[0].text == "\n\n"
        assert "".join(s.text for s in spans) == raw

    def test_delimiter_repeated_separators(self):
        """Repeated \\n\\n\\n\\n belongs to the preceding text."""
        from agency.context import _segment_spans
        raw = "Heading\n\n\n\nBody"
        spans = _segment_spans("src", "hash", raw)
        assert spans[0].text == "Heading\n\n\n\n"
        assert spans[1].text == "Body"
        assert "".join(s.text for s in spans) == raw

    def test_delimiter_leading_newlines(self):
        """Leading \\n\\n belongs to the first textual span."""
        from agency.context import _segment_spans
        raw = "\n\nBody"
        spans = _segment_spans("src", "hash", raw)
        assert spans[0].text == "\n\nBody"
        assert "".join(s.text for s in spans) == raw

    def test_delimiter_trailing_newlines(self):
        """Trailing \\n\\n belongs to the last textual span."""
        from agency.context import _segment_spans
        raw = "Body\n\n"
        spans = _segment_spans("src", "hash", raw)
        assert spans[0].text == "Body\n\n"
        assert "".join(s.text for s in spans) == raw

    def test_delimiter_mixed_boundaries(self):
        """Mixed single and repeated separators."""
        from agency.context import _segment_spans
        raw = "A\n\n\n\nB\n\nC"
        spans = _segment_spans("src", "hash", raw)
        assert spans[0].text == "A\n\n\n\n"
        assert spans[1].text == "B\n\n"
        assert spans[2].text == "C"
        assert "".join(s.text for s in spans) == raw

    def test_markdown_bold_preserved_in_span(self):
        """Markdown **bold** markers survive segmentation unchanged."""
        from agency.context import _segment_spans
        raw = "**Permission escalation patterns.** Not just stuff."
        spans = _segment_spans("src", "hash", raw)
        assert "**Permission escalation patterns.**" in spans[0].text

    def test_claim_text_extracted_from_raw_source(self):
        """Orchestrator extracts claim_text from raw source via span_ids."""
        canonical = [_make_epi_canonical("src-ct", "vantik", "The exact claim text here.")]
        evidence = [{"source_id": "src-ct", "claim_id": "c1",
                     "claim_kind": "assertion",
                     "span_ids": _ea_span_ids("src-ct")}]
        ctx = _run_epi(evidence, _dir_resp("RECORD_ONLY"), canonical)
        assert ctx.status == "completed"
        ev = next(e for e in ctx.accepted_evidence if e["source_id"] == "src-ct")
        assert ev["claim_text"] == "The exact claim text here."
        assert "source_content_hash" in ev

    def test_director_quote_injected_from_accepted_evidence(self):
        """Director provides only source_id+claim_id; code injects quote."""
        sid = "src-inj"
        canonical = [_make_epi_canonical(sid, "vantik", "Injected quote text here.")]
        evidence = [{"source_id": sid, "claim_id": "c1",
                     "claim_kind": "assertion",
                     "span_ids": _ea_span_ids(sid)}]
        f = _make_finding("f1", "Injected test.", [sid], "assertion",
            [{"source_id": sid, "claim_id": "c1"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "completed"
        f1 = ctx.decisions[0]["synthesis"]["findings"][0]
        injected = f1["source_quotes"][0]["quote"]
        assert injected == "Injected quote text here."

    def test_unknown_span_id_fails_closed(self):
        """EA referencing a non-existent span_id → FAIL_CLOSED."""
        sid = "src-unk"
        canonical = [_make_epi_canonical(sid, "vantik", "Valid content.")]
        evidence = [{"source_id": sid, "claim_id": "c1",
                     "claim_kind": "assertion",
                     "span_ids": [f"{sid}/span/999"]}]  # does not exist
        ctx = _run_epi(evidence, _dir_resp("RECORD_ONLY"), canonical)
        assert ctx.status == "failed"
        assert any("Unknown span_id" in i["description"]
                   for i in ctx.incidents)

    def test_non_contiguous_span_ids_fails_closed(self):
        """EA referencing non-contiguous spans → FAIL_CLOSED."""
        sid = "src-nc"
        # Content long enough to produce at least 3 spans
        long_text = "Sentence one here. " + ("x" * 1100) + " Sentence three here."
        canonical = [_make_epi_canonical(sid, "vantik", long_text)]
        # Reference span 0 and span 2 (skipping span 1)
        evidence = [{"source_id": sid, "claim_id": "c1",
                     "claim_kind": "assertion",
                     "span_ids": [f"{sid}/span/0", f"{sid}/span/2"]}]
        ctx = _run_epi(evidence, _dir_resp("RECORD_ONLY"), canonical)
        assert ctx.status == "failed"
        assert any("Non-contiguous" in i["description"]
                   for i in ctx.incidents)

    def test_empty_span_ids_fails_closed(self):
        """EA with empty span_ids → schema rejection → FAIL_CLOSED."""
        sid = "src-empty"
        canonical = [_make_epi_canonical(sid, "vantik", "Content.")]
        evidence = [{"source_id": sid, "claim_id": "c1",
                     "claim_kind": "assertion",
                     "span_ids": []}]
        ctx = _run_epi(evidence, _dir_resp("RECORD_ONLY"), canonical)
        assert ctx.status == "failed"
        assert any("evidence_analyst" in i["description"].lower()
                   for i in ctx.incidents), (
            f"Expected EA failure, got: {ctx.incidents}")

    def test_director_quote_injected_not_authored(self):
        """Director model-facing schema must not include quote field."""
        from agency.model_client import DeepSeekClient
        class _Tx:
            def __call__(self, payload):
                return {"choices": [{"message": {"content": "{}"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}}
        client = DeepSeekClient(transport=_Tx())
        reg = build_role_registry(client=client)
        director = reg["agency_director"]
        director_schema = director._adapter.output_schema
        sq_props = (director_schema["properties"]["synthesis"]
                    ["properties"]["findings"]["items"]
                    ["properties"]["source_quotes"]["items"]["properties"])
        assert "quote" not in sq_props, (
            f"Director model-facing schema must not have quote: {sorted(sq_props.keys())}")

    def test_no_writes_in_dry_run(self):
        """Dry-run observe produces zero transactions."""
        canonical = [_make_epi_canonical("src-dry", "vantik", "No writes.")]
        evidence = [{"source_id": "src-dry", "claim_id": "c1",
                     "claim_kind": "assertion",
                     "span_ids": _ea_span_ids("src-dry")}]
        ctx = _run_epi(evidence, _dir_resp("RECORD_ONLY"), canonical)
        assert len(ctx.transactions) == 0

    # ── B001 regression: quote-character mismatch eliminated ──
    def test_b001_quote_punctuation_preserved(self):
        """Source with double quotes → claim_text + injected quote preserve them."""
        raw = 'That showed up earlier as "you can verify a well-formed lie."'
        canonical = [_make_epi_canonical("src-b001", "hermes-sankhya-25", raw)]
        # EA references the span containing the double-quoted text
        evidence = [{"source_id": "src-b001", "claim_id": "c1",
                     "claim_kind": "assertion",
                     "span_ids": _ea_span_ids("src-b001")}]
        f = _make_finding("f1", "Well-formed lie warning.", ["src-b001"], "assertion",
            [{"source_id": "src-b001", "claim_id": "c1"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "completed"
        # Stored claim_text retains double quotes
        ev = next(e for e in ctx.accepted_evidence if e["claim_id"] == "c1")
        assert '"you can verify a well-formed lie."' in ev["claim_text"]
        # Injected quote is identical to canonical text
        f1 = ctx.decisions[0]["synthesis"]["findings"][0]
        injected = f1["source_quotes"][0]["quote"]
        assert injected == raw
        # No provenance incidents
        assert len(ctx.incidents) == 0

    # ── B007 regression: markdown bold + content beyond 500 chars ──
    def test_b007_markdown_bold_and_long_content_preserved(self):
        """Content with **markdown** beyond char 500 survives intact."""
        # Build >700 chars with claim text after char 500
        prefix = "Preamble text. " * 80  # ~1200 chars of filler
        claim_text = "**Permission escalation patterns.** Not just 'agent ran sudo' — but when an agent gradually expands its own access over multiple steps."
        raw = prefix + claim_text
        assert len(raw) > 700
        assert "**Permission escalation patterns.**" in raw
        # The claim text starts well after char 500
        assert raw.index("**Permission escalation patterns.**") > 500

        canonical = [_make_epi_canonical("src-b007", "murphyhook", raw)]
        # The claim spans across one or more spans — EA references them all
        # Find which span(s) contain the claim
        from agency.context import _segment_spans
        spans = _segment_spans("src-b007", "hash", raw)
        claim_spans = [s for s in spans if "Permission escalation patterns" in s.text]
        span_ids = [s.span_id for s in claim_spans]

        evidence = [{"source_id": "src-b007", "claim_id": "c1",
                     "claim_kind": "assertion",
                     "span_ids": span_ids}]
        f = _make_finding("f1", "Permission escalation patterns logged.", ["src-b007"], "assertion",
            [{"source_id": "src-b007", "claim_id": "c1"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "completed"

        # Claim text contains markdown bold markers exactly
        ev = next(e for e in ctx.accepted_evidence if e["claim_id"] == "c1")
        assert "**Permission escalation patterns.**" in ev["claim_text"]

        # Injected quote is exactly the stored claim_text
        f1 = ctx.decisions[0]["synthesis"]["findings"][0]
        injected = f1["source_quotes"][0]["quote"]
        assert injected == ev["claim_text"], (
            "Injected quote must equal claim_text exactly")
        assert ev["claim_text"] == claim_text, (
            "Stored claim_text must equal the exact canonical claim")
        assert "**" in injected

        # No provenance incidents
        assert len(ctx.incidents) == 0

    # ── View contract tests ──
    def test_ea_view_contains_spans_exactly_once(self):
        """EA view sources[*].spans contains span text exactly once per span."""
        sha = _make_sha("eav")
        ctx = AgencyContextV1(base_sha=sha)
        ctx.set_source_candidates([{"id": "s1", "url": "", "author_handle": "a",
                                     "content_type": "comment", "untrusted": True,
                                     "content_excerpt": "Hello world."}])
        ctx.set_raw_source("s1", "", "a", "comment", "Hello world. Second sentence.")
        view = ctx.view_for("evidence_analyst")
        assert len(view["sources"]) == 1
        spans = view["sources"][0]["spans"]
        assert len(spans) >= 1
        # Concatenated span texts must not duplicate
        texts = [s["text"] for s in spans]
        full = "".join(texts)
        assert full == "Hello world. Second sentence."
        assert full.count("Hello world.") == 1

    def test_ea_view_excludes_raw_content_and_content_excerpt(self):
        """EA view must not expose raw_content or content_excerpt anywhere."""
        sha = _make_sha("eav2")
        ctx = AgencyContextV1(base_sha=sha)
        ctx.set_source_candidates([{"id": "s1", "url": "", "author_handle": "a",
                                     "content_type": "comment", "untrusted": True,
                                     "content_excerpt": "Hello."}])
        ctx.set_raw_source("s1", "", "a", "comment", "Hello.")
        view = ctx.view_for("evidence_analyst")

        _FORBIDDEN = ("source_candidates", "raw_content", "content_excerpt",
                      "source_content_hash")
        # Top-level view
        for key in _FORBIDDEN:
            assert key not in view, f"EA view must not contain {key}"
        # Each source
        for src in view["sources"]:
            for key in _FORBIDDEN:
                assert key not in src, f"EA source must not contain {key}"
            # Each span
            for span in src.get("spans", []):
                for key in _FORBIDDEN:
                    assert key not in span, f"EA span must not contain {key}"

    def test_director_view_excludes_internal_fields(self):
        """Director view accepted_evidence excludes source_content_hash, span_ids."""
        sha = _make_sha("dv")
        ctx = AgencyContextV1(base_sha=sha)
        ctx.add_accepted_evidence([{
            "source_id": "s1", "claim_id": "c1", "claim_kind": "assertion",
            "claim_text": "Text.", "author_handle": "a", "source_class": "external",
            "content_type": "comment", "url": "", "content_excerpt": "excerpt",
            "source_content_hash": "abc123", "span_ids": ["s1/span/0"],
        }])
        view = ctx.view_for("agency_director")
        ev = view["accepted_evidence"][0]
        assert "source_content_hash" not in ev
        assert "span_ids" not in ev
        assert "content_excerpt" not in ev
        # Public fields present
        for f in ("source_id", "claim_id", "claim_kind", "claim_text",
                  "author_handle", "source_class"):
            assert f in ev, f"Director view missing {f}"

    def test_internal_evidence_retains_hash_and_spans(self):
        """Internal _accepted_evidence retains source_content_hash, span_ids."""
        sha = _make_sha("ie")
        ctx = AgencyContextV1(base_sha=sha)
        ctx.add_accepted_evidence([{
            "source_id": "s1", "claim_id": "c1", "claim_kind": "assertion",
            "claim_text": "Text.", "author_handle": "a", "source_class": "external",
            "content_type": "comment", "url": "", "content_excerpt": "excerpt",
            "source_content_hash": "abc123", "span_ids": ["s1/span/0"],
        }])
        # Internal data retains runtime fields
        ev = ctx.accepted_evidence[0]
        assert ev["source_content_hash"] == "abc123"
        assert ev["span_ids"] == ["s1/span/0"]


# ---------------------------------------------------------------------------
# Source-accounting contract tests
# ---------------------------------------------------------------------------

class TestSourceAccounting:
    """Structural source-accounting validation for Evidence Analyst output."""

    # -- helpers --
    @staticmethod
    def _acct_setup(input_sources, accepted, rejected):
        """Build orchestrator with sources, create RoleResult, call _apply_result."""
        sha = _make_sha("acct")
        reg = build_role_registry()
        class _P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha
        orch = AgencyOrchestrator(base_sha=sha, repo_provider=_P(),
                                  role_registry=reg)
        candidates = []
        for src in input_sources:
            orch.ctx.set_raw_source(
                src["source_id"], src.get("url", ""),
                src.get("author_handle", "unknown"),
                src.get("content_type", "comment"),
                src.get("text", "placeholder claim text for testing."),
            )
            candidates.append({
                "id": src["source_id"],
                "url": src.get("url", ""),
                "author_handle": src.get("author_handle", "unknown"),
                "content_type": src.get("content_type", "comment"),
                "untrusted": True,
                "content_excerpt": src.get("text", "placeholder claim text for testing."),
            })
        orch.ctx.set_source_candidates(candidates)
        orch.ctx.status = "running"
        result = RoleResult(
            "evidence_analyst",
            "COMPLETE",
            data={
                "accepted": accepted,
                "rejected": rejected,
            },
        )
        orch._apply_result("evidence_analyst", result)
        return orch

    # -- success cases --

    def test_all_sources_accepted(self):
        srcs = [{"source_id": "s1", "author_handle": "a", "text": "A."},
                {"source_id": "s2", "author_handle": "b", "text": "B."}]
        orch = self._acct_setup(
            srcs,
            accepted=[{"source_id": "s1", "claim_id": "c1", "claim_kind": "assertion", "span_ids": ["s1/span/0"]},
                      {"source_id": "s2", "claim_id": "c2", "claim_kind": "assertion", "span_ids": ["s2/span/0"]}],
            rejected=[],
        )
        assert orch.ctx.status != "failed"
        assert len(orch.ctx.incidents) == 0
        assert len(orch.ctx.accepted_evidence) == 2
        assert orch.ctx.events.has_event_type("SOURCE_ACCEPTED")

    def test_mix_accepted_and_rejected_all_accounted(self):
        srcs = [{"source_id": "s1", "author_handle": "a", "text": "A."},
                {"source_id": "s2", "author_handle": "b", "text": "B."}]
        orch = self._acct_setup(
            srcs,
            accepted=[{"source_id": "s1", "claim_id": "c1", "claim_kind": "assertion", "span_ids": ["s1/span/0"]}],
            rejected=[{"source_id": "s2", "reason": "no relevant claim"}],
        )
        assert orch.ctx.status != "failed"
        assert len(orch.ctx.incidents) == 0
        assert len(orch.ctx.accepted_evidence) == 1
        assert orch.ctx.events.has_event_type("SOURCE_ACCEPTED")

    def test_multiple_accepted_claims_for_one_source(self):
        srcs = [{"source_id": "s1", "author_handle": "a", "text": "A."}]
        orch = self._acct_setup(
            srcs,
            accepted=[{"source_id": "s1", "claim_id": "c1", "claim_kind": "assertion", "span_ids": ["s1/span/0"]},
                      {"source_id": "s1", "claim_id": "c2", "claim_kind": "question", "span_ids": ["s1/span/0"]}],
            rejected=[],
        )
        assert orch.ctx.status != "failed"
        assert len(orch.ctx.incidents) == 0
        assert len(orch.ctx.accepted_evidence) == 2
        assert orch.ctx.events.has_event_type("SOURCE_ACCEPTED")

    def test_b001_shaped_accounting(self):
        srcs = [{"source_id": f"s{i}", "author_handle": "a", "text": "Claim text for testing purposes."} for i in range(6)]
        orch = self._acct_setup(
            srcs,
            accepted=[{"source_id": f"s{i}", "claim_id": f"c{i}", "claim_kind": "assertion", "span_ids": [f"s{i}/span/0"]} for i in range(3)],
            rejected=[{"source_id": f"s{i}", "reason": "duplicate"} for i in range(3, 6)],
        )
        assert orch.ctx.status != "failed"
        assert len(orch.ctx.incidents) == 0
        assert len(orch.ctx.accepted_evidence) == 3
        # Check SOURCE_ACCEPTED event data
        sa_events = [e for e in orch.ctx.events.to_list()
                     if e["event_type"] == "SOURCE_ACCEPTED"]
        assert len(sa_events) == 1
        assert sa_events[0]["data"]["accepted"] == 3
        assert sa_events[0]["data"]["rejected"] == 3

    # -- failure cases --

    _ERR_UNKNOWN_ACC = (
        "Evidence Analyst source accounting: unknown accepted source_id: FAKE")
    _ERR_UNKNOWN_REJ = (
        "Evidence Analyst source accounting: unknown rejected source_id: FAKE")
    _ERR_DUP_REJ = (
        "Evidence Analyst source accounting: duplicate rejected source_id: s1")
    _ERR_CONFLICT = (
        "Evidence Analyst source accounting: source_id present in both "
        "accepted and rejected: s1")
    _ERR_UNACCOUNTED = (
        "Evidence Analyst source accounting: unaccounted source_id: s2")

    def _assert_fail(self, orch, expected_error):
        assert orch.ctx.status == "failed"
        assert orch.ctx.accepted_evidence == []
        assert len(orch.ctx.incidents) == 1
        assert orch.ctx.incidents[0]["severity"] == "high"
        assert orch.ctx.incidents[0]["description"] == expected_error
        assert not orch.ctx.events.has_event_type("SOURCE_ACCEPTED")

    def test_unknown_accepted_source_id(self):
        srcs = [{"source_id": "s1", "author_handle": "a", "text": "A."}]
        orch = self._acct_setup(
            srcs,
            accepted=[{"source_id": "s1", "claim_id": "c1", "claim_kind": "assertion", "span_ids": ["s1/span/0"]},
                      {"source_id": "FAKE", "claim_id": "c2", "claim_kind": "assertion", "span_ids": ["FAKE/span/0"]}],
            rejected=[],
        )
        self._assert_fail(orch, self._ERR_UNKNOWN_ACC)

    def test_unknown_rejected_source_id(self):
        srcs = [{"source_id": "s1", "author_handle": "a", "text": "A."}]
        orch = self._acct_setup(
            srcs,
            accepted=[{"source_id": "s1", "claim_id": "c1", "claim_kind": "assertion", "span_ids": ["s1/span/0"]}],
            rejected=[{"source_id": "FAKE", "reason": "bad"}],
        )
        self._assert_fail(orch, self._ERR_UNKNOWN_REJ)

    def test_duplicate_rejected_entries(self):
        srcs = [{"source_id": "s1", "author_handle": "a", "text": "A."}]
        orch = self._acct_setup(
            srcs,
            accepted=[],
            rejected=[{"source_id": "s1", "reason": "r1"},
                      {"source_id": "s1", "reason": "r2"}],
        )
        self._assert_fail(orch, self._ERR_DUP_REJ)

    def test_source_in_accepted_and_rejected(self):
        srcs = [{"source_id": "s1", "author_handle": "a", "text": "A."}]
        orch = self._acct_setup(
            srcs,
            accepted=[{"source_id": "s1", "claim_id": "c1", "claim_kind": "assertion", "span_ids": ["s1/span/0"]}],
            rejected=[{"source_id": "s1", "reason": "bad"}],
        )
        self._assert_fail(orch, self._ERR_CONFLICT)

    def test_source_absent_from_both_arrays(self):
        srcs = [{"source_id": "s1", "author_handle": "a", "text": "A."},
                {"source_id": "s2", "author_handle": "b", "text": "B."}]
        orch = self._acct_setup(
            srcs,
            accepted=[{"source_id": "s1", "claim_id": "c1", "claim_kind": "assertion", "span_ids": ["s1/span/0"]}],
            rejected=[],
        )
        self._assert_fail(orch, self._ERR_UNACCOUNTED)

    def test_b007_shaped_accounting(self):
        srcs = [{"source_id": f"s{i}", "author_handle": "a", "text": "Claim text here."} for i in range(4)]
        orch = self._acct_setup(
            srcs,
            accepted=[{"source_id": f"s{i}", "claim_id": f"c{i}", "claim_kind": "assertion", "span_ids": [f"s{i}/span/0"]} for i in range(3)],
            rejected=[],
        )
        self._assert_fail(orch,
            "Evidence Analyst source accounting: unaccounted source_id: s3")

    # -- Director-stop integration test (full orch.run path) --

    def test_director_not_called_on_accounting_failure(self):
        """Unaccounted source → FAIL_CLOSED before Director, via orch.run()."""
        sha = _make_sha("dir-stop")
        director_calls = []

        # Stub roles
        class _StubScout(ScoutRole):
            def __call__(self, ctx_view):
                _StubScout.called = getattr(_StubScout, 'called', 0) + 1
                candidates = []
                for i in range(4):
                    sid = f"s{i}"
                    candidates.append({
                        "id": sid,
                        "url": f"https://example.invalid/{sid}",
                        "author_handle": "fixture-author",
                        "content_type": "comment",
                        "untrusted": True,
                        "content_excerpt": f"Canonical claim for {sid}.",
                        "raw_content": f"Canonical claim for {sid}.",
                    })
                return RoleResult("scout", "COMPLETE",
                    data={"candidates_found": 4, "candidates": candidates},
                    provenance=[c["url"] for c in candidates])

        class _StubClerk(RecordsClerkRole):
            def __call__(self, ctx_view):
                _StubClerk.called = getattr(_StubClerk, 'called', 0) + 1
                normalized = []
                for c in ctx_view.get("source_candidates", []):
                    normalized.append({
                        "source_id": c.get("id", c.get("source_id", "")),
                        "url": c.get("url", ""),
                        "author_handle": c.get("author_handle", "unknown"),
                        "content_type": c.get("content_type", "unknown"),
                        "untrusted": True,
                        "observed_at": "2026-01-01T00:00:00Z",
                        "content_excerpt": c.get("content_excerpt", ""),
                        "raw_content": c.get("raw_content", ""),
                        "paraphrase": "",
                        "provenance": [c.get("url", "")],
                    })
                return RoleResult("records_clerk", "COMPLETE",
                    data={"normalized": normalized})

        class _StubEA(EvidenceAnalystRole):
            def __call__(self, ctx_view):
                _StubEA.called = getattr(_StubEA, 'called', 0) + 1
                return RoleResult("evidence_analyst", "COMPLETE", data={
                    "accepted": [
                        {"source_id": f"s{i}", "claim_id": f"c{i}",
                         "claim_kind": "assertion",
                         "span_ids": [f"s{i}/span/0"]} for i in range(3)
                    ],
                    "rejected": [],
                })

        class _SpyDirector(AgencyDirectorRole):
            def __call__(self, ctx_view):
                director_calls.append(1)
                return RoleResult("agency_director", "COMPLETE",
                    data={"disposition": "RECORD_ONLY"})

        reg = build_role_registry()
        reg["scout"] = _StubScout()
        reg["records_clerk"] = _StubClerk()
        reg["evidence_analyst"] = _StubEA()
        reg["agency_director"] = _SpyDirector()

        class _P(RepoStateProvider):
            def current_sha(self): return sha
            def origin_main_sha(self): return sha
        orch = AgencyOrchestrator(
            base_sha=sha, repo_provider=_P(), role_registry=reg,
            campaign={"active_inquiry": "t", "objective": "Test",
                      "internal_author_handles": ["hermes-sankhya-25"]})
        orch.ctx.set_evidence_index(set())
        ctx = orch.run()

        assert ctx.status == "failed"
        assert getattr(_StubScout, 'called', 0) == 1
        assert getattr(_StubClerk, 'called', 0) == 1
        assert getattr(_StubEA, 'called', 0) == 1
        assert len(director_calls) == 0

        assert ctx.decisions == []
        assert ctx.accepted_evidence == []
        assert not ctx.events.has_event_type("SOURCE_ACCEPTED")

        assert len(ctx.incidents) == 1
        assert ctx.incidents[0]["severity"] == "high"
        assert ctx.incidents[0]["description"] == (
            "Evidence Analyst source accounting: unaccounted source_id: s3")

        assert ctx.events.has_event_type("ROLE_COMPLETED")
        assert ctx.events.has_event_type("RUN_CLOSED")
        assert not ctx.events.has_event_type("DIRECTOR_DECISION")

    # -- validation order --

    @pytest.mark.parametrize("accepted,rejected,expected_error", [
        # unknown accepted beats unknown rejected
        ([{"source_id": "FAKE1", "claim_id": "c1", "claim_kind": "assertion", "span_ids": ["FAKE1/span/0"]},
          {"source_id": "s1", "claim_id": "c2", "claim_kind": "assertion", "span_ids": ["s1/span/0"]}],
         [{"source_id": "FAKE2", "reason": "bad"}],
         "Evidence Analyst source accounting: unknown accepted source_id: FAKE1"),
        # unknown rejected beats duplicate rejected
        ([{"source_id": "s1", "claim_id": "c1", "claim_kind": "assertion", "span_ids": ["s1/span/0"]}],
         [{"source_id": "FAKE", "reason": "r1"},
          {"source_id": "FAKE", "reason": "r2"}],
         "Evidence Analyst source accounting: unknown rejected source_id: FAKE"),
        # duplicate rejected beats conflict
        ([],
         [{"source_id": "s1", "reason": "r1"},
          {"source_id": "s1", "reason": "r2"}],
         "Evidence Analyst source accounting: duplicate rejected source_id: s1"),
        # conflict beats unaccounted
        ([{"source_id": "s1", "claim_id": "c1", "claim_kind": "assertion", "span_ids": ["s1/span/0"]},
          {"source_id": "s2", "claim_id": "c2", "claim_kind": "assertion", "span_ids": ["s2/span/0"]}],
         [{"source_id": "s2", "reason": "conflict"}],
         "Evidence Analyst source accounting: source_id present in both accepted and rejected: s2"),
    ])
    def test_validation_order(self, accepted, rejected, expected_error):
        srcs = [{"source_id": "s1", "author_handle": "a", "text": "T1."},
                {"source_id": "s2", "author_handle": "b", "text": "T2."}]
        orch = self._acct_setup(srcs, accepted, rejected)
        self._assert_fail(orch, expected_error)

    # -- prompt contract --

    def test_ea_prompt_contains_source_accounting(self):
        """EA system prompt includes the source-accounting instruction."""
        from agency.model_client import DeepSeekClient
        class _Tx:
            def __call__(self, payload):
                return {"choices": [{"message": {"content": "{}"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}}
        client = DeepSeekClient(transport=_Tx())
        reg = build_role_registry(client=client)
        ea = reg["evidence_analyst"]
        prompt = ea._adapter.system_prompt
        required = (
            "Account for every input source. For each source, either return "
            "one or more accepted claims or exactly one rejected entry "
            "explaining why no objective-relevant claim was extracted. "
            "Never place the same source_id in both accepted and rejected."
        )
        assert required in prompt, "Missing source-accounting instruction"
        # Forbidden phrases
        for forbidden in ("select at least one claim from every source",
                          "cover every source with a claim",
                          "maximize claim count"):
            assert forbidden not in prompt, (
                f"Forbidden phrase found in EA prompt: {forbidden}")


# ---------------------------------------------------------------------------
# Director claim-kind fidelity tests
# ---------------------------------------------------------------------------

_B007_CANONICAL_CLAIM = (
    "After monitoring thousands of agent sessions at AgentSteer, "
    "here are the three most commonly missed logs: Permission "
    "escalation patterns. Not just 'agent ran sudo' — but when "
    "an agent gradually expands its own access over multiple "
    "steps. First it reads a config, then it modifies it, then "
    "it uses the modified config to access something new. Each "
    "step looks innocent. The pattern is the signal."
)

class TestDirectorClaimKind:
    """Director finding_kind must equal the claim_kind of all quoted claims."""

    @staticmethod
    def _run_kind_test(claim_kind, finding_kind):
        sid = "src-ck"
        raw = "Test claim content for kind validation purposes."
        canonical = [_make_epi_canonical(sid, "vantik", raw)]
        evidence = [{"source_id": sid, "claim_id": "c1",
                     "claim_kind": claim_kind,
                     "span_ids": _ea_span_ids(sid)}]
        f = _make_finding("f1", "Statement.", [sid], finding_kind,
            [{"source_id": sid, "claim_id": "c1"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        return _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn),
                        canonical)

    @staticmethod
    def _run_mixed_test(kind_a, kind_b):
        sid_a, sid_p = "src-a", "src-b"
        canonical = [
            _make_epi_canonical(sid_a, "a", "Assertion text for testing."),
            _make_epi_canonical(sid_p, "b", "Second text for testing purposes."),
        ]
        evidence = [
            {"source_id": sid_a, "claim_id": "c1", "claim_kind": kind_a,
             "span_ids": _ea_span_ids(sid_a)},
            {"source_id": sid_p, "claim_id": "c2", "claim_kind": kind_b,
             "span_ids": _ea_span_ids(sid_p)},
        ]
        f = _make_finding("f1", "Mixed.", [sid_a, sid_p], kind_a,
            [{"source_id": sid_a, "claim_id": "c1"},
             {"source_id": sid_p, "claim_id": "c2"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        return _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn),
                        canonical)

    # -- valid: exact match --

    @pytest.mark.parametrize("kind", [
        "assertion", "opinion", "proposal", "question", "warning", "unknown",
    ])
    def test_valid_exact_match(self, kind):
        ctx = self._run_kind_test(kind, kind)
        assert ctx.status == "completed"
        assert ctx.incidents == []
        assert len(ctx.decisions) == 1

    # -- invalid: mismatched kinds --

    @pytest.mark.parametrize("claim_kind,finding_kind", [
        ("assertion", "proposal"),
        ("assertion", "opinion"),
        ("opinion", "assertion"),
        ("opinion", "proposal"),
        ("proposal", "assertion"),
        ("question", "assertion"),
        ("question", "proposal"),
        ("warning", "assertion"),
        ("warning", "proposal"),
        ("unknown", "assertion"),
    ])
    def test_mismatched_kind_fails(self, claim_kind, finding_kind):
        ctx = self._run_kind_test(claim_kind, finding_kind)
        assert ctx.status == "failed"
        assert ctx.decisions == []
        assert len(ctx.incidents) == 1
        assert ctx.incidents[0]["severity"] == "high"
        assert ctx.incidents[0]["description"] == (
            f"Finding f1: finding_kind '{finding_kind}' "
            f"does not match claim kind '{claim_kind}'"
        )

    # -- mixed claim kinds in one finding --

    @pytest.mark.parametrize("kind_a,kind_b", [
        ("assertion", "proposal"),
        ("assertion", "opinion"),
        ("question", "assertion"),
        ("warning", "assertion"),
    ])
    def test_mixed_kinds_in_one_finding_fails(self, kind_a, kind_b):
        ctx = self._run_mixed_test(kind_a, kind_b)
        assert ctx.status == "failed"
        assert ctx.decisions == []
        assert len(ctx.incidents) == 1
        assert ctx.incidents[0]["severity"] == "high"
        sorted_kinds = sorted([kind_a, kind_b])
        assert ctx.incidents[0]["description"] == (
            f"Finding f1: mixed claim kinds {sorted_kinds} — "
            "all quoted claims must have the same claim_kind"
        )

    # -- B007 live-shaped regression --

    def test_b007_assertion_reported_as_proposal_fails(self):
        sid = "src-b007"
        canonical = [_make_epi_canonical(sid, "murphyhook",
                                         _B007_CANONICAL_CLAIM)]
        evidence = [{"source_id": sid, "claim_id": "c1",
                     "claim_kind": "assertion",
                     "span_ids": _ea_span_ids(sid)}]
        f = _make_finding("f-1",
            "murphyhook proposes logging permission escalation patterns.",
            [sid], "proposal",
            [{"source_id": sid, "claim_id": "c1"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn),
                       canonical)
        assert ctx.status == "failed"
        assert ctx.decisions == []
        assert len(ctx.incidents) == 1
        assert ctx.incidents[0]["severity"] == "high"
        assert ctx.incidents[0]["description"] == (
            "Finding f-1: finding_kind 'proposal' does not match "
            "claim kind 'assertion'")

    def test_b007_assertion_reported_as_assertion_passes(self):
        sid = "src-b007b"
        canonical = [_make_epi_canonical(sid, "murphyhook",
                                         _B007_CANONICAL_CLAIM)]
        evidence = [{"source_id": sid, "claim_id": "c1",
                     "claim_kind": "assertion",
                     "span_ids": _ea_span_ids(sid)}]
        f = _make_finding("f-1",
            "murphyhook reports that permission-escalation patterns "
            "are among the logs most commonly missed and describes them "
            "as multi-step access expansion.",
            [sid], "assertion",
            [{"source_id": sid, "claim_id": "c1"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn),
                       canonical)
        assert ctx.status == "completed"
        assert ctx.incidents == []
        assert len(ctx.decisions) == 1
        f1 = ctx.decisions[0]["synthesis"]["findings"][0]
        inj = f1["source_quotes"][0]["quote"]
        ev = next(e for e in ctx.accepted_evidence if e["claim_id"] == "c1")
        assert inj == ev["claim_text"]

    # -- prompt contract --

    def test_director_prompt_contains_claim_kind_instruction(self):
        from agency.model_client import DeepSeekClient
        class _Tx:
            def __call__(self, payload):
                return {"choices": [{"message": {"content": "{}"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}}
        client = DeepSeekClient(transport=_Tx())
        reg = build_role_registry(client=client)
        director = reg["agency_director"]
        prompt = director._adapter.system_prompt

        # Required new passage
        required = (
            "Set each finding_kind to the exact claim_kind shared by every "
            "quoted claim. A finding may quote only claims with one shared "
            "claim_kind. The research objective controls relevance, not "
            "source modality. Do not infer a proposal from an assertion or "
            "opinion. If evidence of different kinds is relevant, emit "
            "separate findings. Use unknown only for claims classified as "
            "unknown."
        )
        assert required in prompt, "Missing claim-kind fidelity instruction"

        # Existing safety phrases preserved
        assert "Report what sources assert, opine, propose, ask, or warn about." in prompt
        assert "Preserve those distinctions." in prompt
        assert "Never convert a question or warning into a requirement." in prompt
        assert "Never invent a solution." in prompt
        assert "Never recommend implementation work." in prompt

        # Forbidden reclassification language
        for forbidden in ("assertions may become proposals",
                          "opinions may become proposals",
                          "finding_kind is independent"):
            assert forbidden not in prompt, (
                f"Forbidden phrase: {forbidden}")
