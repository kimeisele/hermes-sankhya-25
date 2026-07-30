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
from agency.roles import (RoleResult, ScoutRole, AgencyDirectorRole)
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
    return {"choices": [{"message": {"content": json.dumps({
        "accepted": accepted_list, "rejected": rejected or [],
    })}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def _dir_resp(disposition, synthesis=None):
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


def _make_finding(fid, statement, src_ids, kind, quotes, reasoning="R"):
    return {"finding_id": fid, "statement": statement,
            "source_ids": src_ids, "finding_kind": kind,
            "source_quotes": quotes, "reasoning": reasoning}


def _run_epi(evidence_accepted, director_resp, canonical_items,
             call_log=None, internal_handles=None):
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
                return _ev_resp(evidence_accepted)
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
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "Model text"}]
        ctx = _run_epi(evidence, _dir_resp("RECORD_ONLY"), canonical)
        assert ctx.status == "completed"
        ev = next(e for e in ctx.accepted_evidence if e["source_id"] == sid)
        assert ev["author_handle"] == real_author
        assert ev["content_excerpt"] == real_excerpt
        assert ev["source_class"] == "external"

    # ── B: unknown accepted source + Director not called ──
    def test_b_unknown_accepted_source(self):
        call_log = []
        evidence = [{"source_id": "FAKE-SRC", "claim_id": "c1", "claim_kind": "assertion", "claim_text": "X"}]
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
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "X"}]
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
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "question", "claim_text": "Q"}]
        f = _make_finding("f1", "Binding is required.", [sid], "assertion",  # finding_kind=assertion
            [{"source_id": sid, "claim_id": "c1", "quote": "Should we bind"}])
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
        evidence = [{"source_id": sid1, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "A"},
                    {"source_id": sid2, "claim_id": "c2", "claim_kind": "question", "claim_text": "Q"}]
        f = _make_finding("f1", "Mixed finding.", [sid1, sid2], "assertion",
            [{"source_id": sid1, "claim_id": "c1", "quote": "Commit hash"},
             {"source_id": sid2, "claim_id": "c2", "quote": "Should receipts be"}])
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
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "X"}]
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
            {"source_id": sid_u, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "U"},
            {"source_id": sid_e, "claim_id": "c2", "claim_kind": "assertion", "claim_text": "E"},
        ]
        f = _make_finding("f1", "Mix.", [sid_u, sid_e], "assertion",
            [{"source_id": sid_u, "claim_id": "c1", "quote": "unknown text"},
             {"source_id": sid_e, "claim_id": "c2", "quote": "external text"}])
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
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "L"}]
        f = _make_finding("f1", "Long test.", [sid], "assertion",
            [{"source_id": sid, "claim_id": "c1", "quote": long_quote}])
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
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "H"}]
        f = _make_finding("f1", "Schema validated.", [sid], "assertion",
            [{"source_id": sid, "claim_id": "c1", "quote": "Enriched schema test text"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "completed"
        assert len(ctx.decisions) == 1
        sd = Path(__file__).resolve().parents[2] / "schemas"
        dec_schema = json.loads((sd / "agency-decision-v1.schema.json").read_text())
        import jsonschema as _js
        _js.validate(instance=ctx.decisions[0], schema=dec_schema)

    # ── I: bad quote fails closed ──
    def test_i_bad_quote_fails(self):
        sid = "src-i"
        canonical = [_make_epi_canonical(sid, "vantik", "exact canonical text here.")]
        evidence = [{"source_id": sid, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "I"}]
        f = _make_finding("f1", "Bad.", [sid], "assertion",
            [{"source_id": sid, "claim_id": "c1", "quote": "NOT IN EXCERPT"}])
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [f], "unresolved_questions": []}
        ctx = _run_epi(evidence, _dir_resp("READY_FOR_SYNTHESIS", syn), canonical)
        assert ctx.status == "failed"
        assert len(ctx.incidents) >= 1

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
            {"source_id": s_int, "claim_id": "c-int", "claim_kind": "assertion", "claim_text": "IA"},
            {"source_id": s_e1, "claim_id": "c-e1", "claim_kind": "assertion", "claim_text": "EA1"},
            {"source_id": s_e2, "claim_id": "c-e2", "claim_kind": "assertion", "claim_text": "EA2"},
        ]
        call_log = []
        f = _make_finding("f1", "Receipt fields and binding.", [s_e1, s_e2, s_int],
            "assertion",
            [{"source_id": s_e1, "claim_id": "c-e1", "quote": "commit_hash and timestamp"},
             {"source_id": s_e2, "claim_id": "c-e2", "quote": "test_run_id with pass/fail counts"},
             {"source_id": s_int, "claim_id": "c-int", "quote": "what binds the receipt to work"}])
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
                             "content_excerpt": sentinel}])
        orch.ctx.set_source_candidates([{"id": "item1", "url": "https://x.com/p/1",
            "author_handle": "test", "untrusted": True, "content_excerpt": sentinel,
            "content_type": "comment"}])
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
                             "content_excerpt": "Valid evidence content."}])
        orch.ctx.set_source_candidates([{"id": "item1", "url": "https://x.com/p/1",
            "author_handle": "test", "untrusted": True,
            "content_excerpt": "Valid evidence content.", "content_type": "comment"}])
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
                             "content_excerpt": "test content."}])
        orch.ctx.set_source_candidates([{"id": "item1", "url": "https://x.com/p/1",
            "author_handle": "test", "untrusted": True,
            "content_excerpt": "test content.", "content_type": "comment"}])
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
                             "content_excerpt": "Valid evidence."}])
        orch.ctx.set_source_candidates([{"id": "item1", "url": "https://x.com/p/1",
            "author_handle": "test", "untrusted": True,
            "content_excerpt": "Valid evidence.", "content_type": "comment"}])
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
                     "claim_kind": "assertion", "claim_text": "commit_hash is essential"}],
                    "rejected": []})}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=_Tx())
        class _R:
            def fetch_post(self, pid):
                return {"post": {"id": pid, "content": "Test content.", "author": {"name": "post_author"}}}
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
                return {"post": {"id": pid, "content": "body", "author": {"name": "op"}}}
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
        evidence = [{"source_id": accepted_src, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "S"}]
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "The answer is 42.",
               "findings": [_make_finding("f1", "Answer found.", [accepted_src], "assertion",
                   [{"source_id": accepted_src, "claim_id": "c1", "quote": "evidence text for " + accepted_src}])],
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
        evidence = [{"source_id": accepted_src, "claim_id": "c1", "claim_kind": "assertion", "claim_text": "L"}]
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [_make_finding("f1", "A" * 150 + "STAT-END", [accepted_src], "assertion",
                   [{"source_id": accepted_src, "claim_id": "c1", "quote": "evidence text for " + accepted_src}],
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
        evidence = [{"source_id": "real-src", "claim_id": "c1", "claim_kind": "assertion", "claim_text": "X"}]
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
        evidence = [{"source_id": "real-src", "claim_id": "c1", "claim_kind": "assertion", "claim_text": "X"}]
        syn = {"inquiry": _OBJECTIVE, "executive_answer": "A",
               "findings": [_make_finding("f1", "S", ["FAKE-SRC"], "assertion",
                   [{"source_id": "FAKE-SRC", "claim_id": "c1", "quote": "x"}])],
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
                         "claim_kind": "assertion", "claim_text": "X"}],
                        "rejected": []})}}],
                        "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}}
                raise RuntimeError(raw_error)
        from agency.model_client import DeepSeekClient
        client = DeepSeekClient(transport=_Tx())
        class _R:
            def fetch_post(self, pid):
                return {"post": {"id": pid, "content": "post", "author": {"name": "op"}}}
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
        assert ev["content_excerpt"] == "CANONICAL_EXCERPT_FOR_TEST_12345"
        assert ev["claim_id"] == "c1"
        assert ev["claim_kind"] == "assertion"

        ea_view = ctx.view_for("evidence_analyst")
        assert "source_candidates" in ea_view, (
            "Evidence Analyst must receive source_candidates")
        assert len(ea_view["source_candidates"]) == 1
        assert ea_view["source_candidates"][0]["id"] == "src-1"


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
                return {"post": {"id": pid, "content": "post", "author": {"name": "op"}}}
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
        assert set(acc_req) == {"source_id", "claim_id", "claim_kind", "claim_text"}
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
                         "claim_kind": "assertion", "claim_text": "Exact claim text."},
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
        assert acc[0]["claim_text"] == "Exact claim text."

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
                             "claim_kind": "assertion", "claim_text": "Exact claim text."},
                        ],
                        "rejected": [],
                        "rationale": "extra field",
                    })}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                  "total_tokens": 150}}
                else:
                    return {"choices": [{"message": {"content": json.dumps({
                        "accepted": [
                            {"source_id": "src-1", "claim_id": "claim-1",
                             "claim_kind": "assertion", "claim_text": "Exact claim text."},
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
                         "claim_kind": "assertion", "claim_text": "T"},
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

    def test_only_evidence_analyst_forces_thinking_mode(self):
        """EA adapter has thinking_enabled=False; all others are None."""
        from agency.model_client import DeepSeekClient

        class _Tx:
            def __call__(self, payload):
                return {"choices": [{"message": {"content": "{}"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

        client = DeepSeekClient(transport=_Tx())
        reg = build_role_registry(client=client)

        # Evidence Analyst is the only one with thinking_enabled set
        for role_name, adapter in reg.items():
            if not hasattr(adapter, '_adapter') or adapter._adapter is None:
                continue
            if role_name == "evidence_analyst":
                assert adapter._adapter.thinking_enabled is False, (
                    f"EA must have thinking_enabled=False, got {adapter._adapter.thinking_enabled}")
            else:
                assert adapter._adapter.thinking_enabled is None, (
                    f"{role_name} must have thinking_enabled=None, "
                    f"got {adapter._adapter.thinking_enabled}")
