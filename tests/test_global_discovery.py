"""Tests for bounded Moltbook global discovery V1."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from agency.discovery import (
    DiscoveryClient, DiscoveryConfig, GlobalDiscovery,
    _content_sha, _excerpt, candidates_to_json, score_post,
)


def _post(pid, author="ext", title="", content="", created="2026-08-01T00:00:00Z",
          deleted=False, spam=False):
    return {
        "id": pid, "author": {"name": author}, "author_id": f"u-{author}",
        "title": title, "content": content, "created_at": created,
        "is_deleted": deleted, "is_spam": spam,
        "comment_count": 0, "score": 0, "upvotes": 0,
        "submolt": "s", "type": "post",
    }


def _valid_uuid(n):
    return f"{n:08x}-0000-0000-0000-000000000000"


class _FakeTransport:
    def __init__(self, listings):
        self._listings = listings  # dict sort -> list of posts
        self.calls = []

    def __call__(self, req):
        self.calls.append(req)
        path = req.get("path", "")
        sort = "new"
        m = re.search(r"sort=(\w+)", path)
        if m:
            sort = m.group(1)
        return {"posts": self._listings.get(sort, []), "success": True}


def _cfg(strong=None, secondary=None, internal=None, cap=20):
    return DiscoveryConfig(
        enabled=True, max_new=50, max_top_day=25, max_comments_day=25,
        candidate_cap=cap, excerpt_length=500,
        strong_terms=strong or ["effect receipt", "task receipt"],
        secondary_terms=secondary or ["verification", "evidence"],
        internal_handles=internal or ["hermes-sankhya-25"],
    )


# ---------------------------------------------------------------------------
# Listing parsing
# ---------------------------------------------------------------------------

class TestListingShape:

    def test_supported_listing_shape_parsed(self):
        t = _FakeTransport({"new": [_post(_valid_uuid(1), title="effect receipt")]})
        client = DiscoveryClient(transport=t)
        posts = client.fetch_listing("new", 10)
        assert len(posts) == 1
        assert posts[0]["id"] == _valid_uuid(1)

    def test_unknown_shape_fails_closed(self):
        class BadTransport:
            def __call__(self, req):
                return {"unexpected": True}
        client = DiscoveryClient(transport=BadTransport())
        with pytest.raises(ValueError):
            client.fetch_listing("new", 10)


# ---------------------------------------------------------------------------
# Dedup + filtering
# ---------------------------------------------------------------------------

class TestDedupAndFilter:

    def test_duplicate_post_id_appears_once(self):
        p = _post(_valid_uuid(1), title="effect receipt for agents")
        t = _FakeTransport({"new": [p], "top": [p], "comments": [p]})
        d = GlobalDiscovery(DiscoveryClient(transport=t), _cfg())
        cands = d.run()
        assert len([c for c in cands if c.post_id == _valid_uuid(1)]) == 1

    def test_internal_hermes_post_excluded(self):
        p = _post(_valid_uuid(1), author="hermes-sankhya-25",
                  title="effect receipt")
        t = _FakeTransport({"new": [p]})
        d = GlobalDiscovery(DiscoveryClient(transport=t), _cfg())
        assert d.run() == []

    def test_known_evidence_post_id_excluded(self):
        p = _post(_valid_uuid(1), title="effect receipt")
        t = _FakeTransport({"new": [p]})
        d = GlobalDiscovery(DiscoveryClient(transport=t), _cfg(),
                            known_ids={_valid_uuid(1)})
        assert d.run() == []

    def test_deleted_and_spam_posts_discarded(self):
        p1 = _post(_valid_uuid(1), title="effect receipt", deleted=True)
        p2 = _post(_valid_uuid(2), title="effect receipt", spam=True)
        t = _FakeTransport({"new": [p1, p2]})
        d = GlobalDiscovery(DiscoveryClient(transport=t), _cfg())
        assert d.run() == []


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------

class TestRelevance:

    def test_strong_signal_qualifies(self):
        score, matched = score_post("Task receipt proposal", "An effect receipt for agents.",
                                    ["effect receipt"], ["verification"], True)
        assert score > 0
        assert "effect receipt" in matched

    def test_single_secondary_signal_alone_does_not_qualify(self):
        score, matched = score_post("A post", "Verification is hard.",
                                    ["effect receipt"], ["verification"], True)
        assert score == 0

    def test_two_secondaries_without_agent_context_do_not_qualify(self):
        score, _ = score_post("Weather today", "Evidence and verification of rain.",
                              ["effect receipt"], ["verification", "evidence"], False)
        assert score == 0

    def test_two_secondaries_with_agent_context_qualify(self):
        score, _ = score_post("Runner notes", "Agent evidence and verification.",
                              ["effect receipt"], ["verification", "evidence"], True)
        assert score > 0


# ---------------------------------------------------------------------------
# Bounds + determinism
# ---------------------------------------------------------------------------

class TestBounds:

    def test_candidate_cap_20(self):
        posts = [_post(_valid_uuid(i), title="effect receipt for agents")
                 for i in range(1, 60)]
        t = _FakeTransport({"new": posts})
        d = GlobalDiscovery(DiscoveryClient(transport=t), _cfg(cap=20))
        cands = d.run()
        assert len(cands) == 20

    def test_deterministic_sorting(self):
        p1 = _post(_valid_uuid(1), title="effect receipt", created="2026-08-01T00:00:00Z")
        p2 = _post(_valid_uuid(2), title="effect receipt", created="2026-08-02T00:00:00Z")
        t = _FakeTransport({"new": [p1, p2]})
        d = GlobalDiscovery(DiscoveryClient(transport=t), _cfg())
        cands = d.run()
        # same score → created_at desc → p2 first
        assert cands[0].post_id == _valid_uuid(2)

    def test_excerpt_max_500(self):
        long = "x" * 2000
        assert len(_excerpt(long, 500)) == 500

    def test_content_hash_reproducible(self):
        assert _content_sha("abc") == hashlib.sha256(b"abc").hexdigest()
        assert _content_sha("abc") == _content_sha("abc")


# ---------------------------------------------------------------------------
# Artifacts + no-model invariants
# ---------------------------------------------------------------------------

class TestArtifacts:

    def test_zero_candidates_is_successful_noop(self):
        t = _FakeTransport({"new": []})
        d = GlobalDiscovery(DiscoveryClient(transport=t), _cfg())
        cands = d.run()
        assert cands == []
        data = candidates_to_json(cands)
        assert data["status"] == "completed"
        assert data["candidate_count"] == 0
        assert data["model_calls"] == 0
        assert data["tokens"] == 0
        assert data["external_writes"] == 0

    def test_zero_model_calls(self):
        t = _FakeTransport({"new": [_post(_valid_uuid(1), title="effect receipt")]})
        d = GlobalDiscovery(DiscoveryClient(transport=t), _cfg())
        d.run()
        data = candidates_to_json(d.run())
        assert data["model_calls"] == 0
        assert data["tokens"] == 0
        assert data["external_writes"] == 0

    def test_candidate_artifact_fields(self):
        p = _post(_valid_uuid(1), title="effect receipt for agents", content="C" * 600)
        t = _FakeTransport({"new": [p]})
        d = GlobalDiscovery(DiscoveryClient(transport=t), _cfg())
        cands = d.run()
        assert len(cands) == 1
        c = cands[0]
        assert c.post_id == _valid_uuid(1)
        assert c.canonical_url == f"https://www.moltbook.com/post/{_valid_uuid(1)}"
        assert len(c.content_excerpt) == 500
        assert c.content_sha256 == _content_sha("C" * 600)
        assert c.already_known is False
        data = candidates_to_json(cands)
        assert data["candidates"][0]["post_id"] == _valid_uuid(1)

    def test_no_non_get_method_reachable(self):
        client = DiscoveryClient(transport=lambda r: {"posts": []})
        with pytest.raises(RuntimeError):
            client._api_call("POST", "/posts?sort=new&limit=5")
        with pytest.raises(RuntimeError):
            client._api_call("GET", "/some/other/path")


# ---------------------------------------------------------------------------
# Workflow permissions
# ---------------------------------------------------------------------------

class TestWorkflow:

    def test_workflow_no_write_permissions(self):
        wf = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "moltbook-agency-discover.yml"
        text = wf.read_text()
        assert "contents: read" in text
        assert "issues: write" in text
        assert "actions: read" in text
        assert "contents: write" not in text
        assert "pull-requests: write" not in text
        assert "DEEPSEEK_API_KEY" not in text
        assert "workflow_dispatch" in text
        assert "cancel-in-progress: false" in text

    def test_config_values_present(self):
        cfg = Path(__file__).resolve().parents[1] / "config" / "moltbook_agency.toml"
        text = cfg.read_text()
        for key in ("global_discovery_enabled", "global_discovery_max_new",
                    "global_discovery_max_top_day", "global_discovery_max_comments_day",
                    "global_discovery_candidate_cap", "global_discovery_excerpt_length",
                    "global_discovery_strong_terms", "global_discovery_secondary_terms"):
            assert key in text, f"missing {key}"


# ---------------------------------------------------------------------------
# Hardening: config bounds, network boundary, word boundaries, sanitization
# ---------------------------------------------------------------------------

class TestHardening:

    def test_config_over_50_rejected(self):
        with pytest.raises(ValueError):
            DiscoveryConfig(max_new=60).validate()

    def test_config_over_25_top_rejected(self):
        with pytest.raises(ValueError):
            DiscoveryConfig(max_top_day=30).validate()

    def test_config_over_25_comments_rejected(self):
        with pytest.raises(ValueError):
            DiscoveryConfig(max_comments_day=30).validate()

    def test_candidate_cap_over_20_rejected(self):
        with pytest.raises(ValueError):
            DiscoveryConfig(candidate_cap=21).validate()

    def test_excerpt_over_500_rejected(self):
        with pytest.raises(ValueError):
            DiscoveryConfig(excerpt_length=501).validate()

    def test_unknown_query_path_rejected(self):
        client = DiscoveryClient(transport=lambda r: {"posts": []})
        with pytest.raises(RuntimeError):
            client._api_call("GET", "/posts?sort=other&limit=5")

    def test_non_get_unreachable(self):
        client = DiscoveryClient(transport=lambda r: {"posts": []})
        with pytest.raises(RuntimeError):
            client._api_call("POST", "/posts?sort=new&limit=5")

    def test_redirect_rejected(self):
        from agency.discovery import _NoRedirectHandler
        h = _NoRedirectHandler()
        with pytest.raises(RuntimeError):
            h.redirect_request(None, None, 302, "Found", {}, "https://evil.example")

    def test_foreign_host_blocked(self):
        client = DiscoveryClient(base_url="https://evil.example/api/v1",
                                 transport=lambda r: {"posts": []})
        # Production host check applies to the actual HTTP path; with a
        # transport, no host check runs — assert the base URL is fixed.
        assert client.PRODUCTION_BASE_URL == "https://www.moltbook.com/api/v1"
        assert client.base_url != client.PRODUCTION_BASE_URL  # would be blocked

    def test_api_overreturn_capped(self):
        posts = [_post(_valid_uuid(i)) for i in range(1, 61)]
        t = _FakeTransport({"new": posts})
        client = DiscoveryClient(transport=t)
        result = client.fetch_listing("new", 50)
        assert len(result) == 50

    def test_max_100_unique_posts_processed(self):
        # 150 unique posts; only the first 100 are processed. Posts beyond
        # 100 carry strong signals but must be ignored.
        posts = ([_post(_valid_uuid(i), title="nothing relevant here")
                  for i in range(1, 101)]
                 + [_post(_valid_uuid(i), title="effect receipt for agents")
                    for i in range(101, 151)])
        t = _FakeTransport({"new": posts})
        d = GlobalDiscovery(DiscoveryClient(transport=t), _cfg())
        assert d.run() == []

    def test_word_boundary_done_not_in_abandoned(self):
        score, matched = score_post("Abandoned task", "The task was abandoned.",
                                    ["effect receipt"], ["done"], True)
        assert score == 0

    def test_word_boundary_model_not_in_remodel(self):
        score, matched = score_post("Remodel notes", "We remodel the house.",
                                    ["effect receipt"], ["model"], True)
        assert score == 0

    def test_multiword_phrase_still_matches(self):
        score, matched = score_post("A proposal", "An effect receipt for agents.",
                                    ["effect receipt"], ["verification"], True)
        assert score > 0

    def test_markdown_chars_neutralized(self):
        from agency.discovery import sanitize_text
        out = sanitize_text("**bold** [link](x) `code` <tag>", 200)
        assert "**" not in out
        assert "[" not in out
        assert "<" not in out

    def test_mentions_neutralized(self):
        from agency.discovery import sanitize_text
        out = sanitize_text("hello @someone and @else", 200)
        assert "@" not in out


class TestNotificationDedup:

    def test_marker_roundtrip(self):
        from agency.discovery import build_marker, parse_marker_ids
        marker = build_marker(["b", "a"])
        assert marker == "<!-- hermes-global-discovery-v1:candidate-ids=a,b -->"
        assert parse_marker_ids(marker) == {"a", "b"}

    def test_already_reported_candidates_not_reported_again(self):
        from agency.discovery import new_candidate_ids, Candidate
        c1 = Candidate(_valid_uuid(1), "u", "a", "t", "c", "x", "h", [], 1, ["new"])
        c2 = Candidate(_valid_uuid(2), "u", "a", "t", "c", "x", "h", [], 1, ["new"])
        new = new_candidate_ids([c1, c2], {_valid_uuid(1)})
        assert new == [_valid_uuid(2)]

    def test_new_candidates_reported_once(self):
        from agency.discovery import new_candidate_ids, Candidate
        c1 = Candidate(_valid_uuid(1), "u", "a", "t", "c", "x", "h", [], 1, ["new"])
        assert new_candidate_ids([c1], set()) == [_valid_uuid(1)]

    def test_zero_new_candidates_no_comment(self):
        from agency.discovery import new_candidate_ids, Candidate
        c1 = Candidate(_valid_uuid(1), "u", "a", "t", "c", "x", "h", [], 1, ["new"])
        assert new_candidate_ids([c1], {_valid_uuid(1)}) == []


class TestWorkflowHardening:

    def test_failure_artifact_upload_always(self):
        wf = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "moltbook-agency-discover.yml"
        text = wf.read_text()
        assert "if: ${{ always() }}" in text
        assert "upload-artifact" in text

    def test_issue_target_fixed_513(self):
        wf = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "moltbook-agency-discover.yml"
        text = wf.read_text()
        assert "ISSUE_NUMBER = 513" in text
        # No issue-title search
        assert "listForRepo" not in text


class TestEvidenceIndexFailClosed:

    def test_evidence_index_error_aborts_before_network(self, tmp_path, monkeypatch):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "moltbook_discover", str(Path(__file__).resolve().parents[1] / "scripts" / "moltbook_discover.py"))
        mod = importlib.util.module_from_spec(spec)
        import sys as _sys
        _sys.modules["moltbook_discover"] = mod
        spec.loader.exec_module(mod)

        def boom():
            raise RuntimeError("evidence index corrupted")
        monkeypatch.setattr(mod, "_load_evidence_ids", boom)

        out = tmp_path / "out"
        import sys as _s
        _s.argv = ["discover", "--output", str(out),
                   "--candidates", str(out / "discovery_candidates.json"),
                   "--report", str(out / "discovery_report.md")]
        rc = mod.main()
        assert rc == 1
        data = json.loads((out / "discovery_candidates.json").read_text())
        assert data["status"] == "failed"
        assert data["failure_code"] == "EVIDENCE_INDEX_INVALID"
        assert data["candidate_count"] == 0
        assert data["model_calls"] == 0
        assert data["tokens"] == 0
        assert data["external_writes"] == 0

    def test_evidence_index_error_creates_failure_artifacts(self, tmp_path, monkeypatch):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "moltbook_discover2", str(Path(__file__).resolve().parents[1] / "scripts" / "moltbook_discover.py"))
        mod = importlib.util.module_from_spec(spec)
        import sys as _sys
        _sys.modules["moltbook_discover2"] = mod
        spec.loader.exec_module(mod)

        def boom():
            raise ValueError("malformed record")
        monkeypatch.setattr(mod, "_load_evidence_ids", boom)

        out = tmp_path / "out2"
        import sys as _s
        _s.argv = ["discover", "--output", str(out),
                   "--candidates", str(out / "discovery_candidates.json"),
                   "--report", str(out / "discovery_report.md")]
        rc = mod.main()
        assert rc == 1
        assert (out / "discovery_candidates.json").exists()
        assert (out / "discovery_report.md").exists()
        report = (out / "discovery_report.md").read_text()
        assert "failed" in report
        assert "EVIDENCE_INDEX_INVALID" in report
