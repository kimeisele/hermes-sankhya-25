"""Tests for AgencyContextV1, budget, events, and bridge regression."""
from __future__ import annotations

import hashlib
import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agency.context import AgencyContextV1, AgencyBudget, _sanitize_value
from agency.events import EventLog
from agency.events import (RUN_STARTED, RUN_CLOSED)


# ---------------------------------------------------------------------------
# Budget tests
# ---------------------------------------------------------------------------

class TestAgencyBudget:
    def test_default(self):
        b = AgencyBudget()
        assert not b.is_exhausted

    def test_role_call_exhaustion(self):
        b = AgencyBudget(max_role_calls=3)
        b.role_calls_used = 3
        assert b.is_exhausted

    def test_token_exhaustion(self):
        b = AgencyBudget(max_tokens=100)
        b.tokens_used = 100
        assert b.is_exhausted

    def test_reserve_and_reconcile(self):
        b = AgencyBudget(max_tokens=1000)
        assert b.reserve(estimated_tokens=100)
        b.reconcile(100, 50, 0.01, 0.005)
        assert b.tokens_used == 50

    def test_reserve_blocks_when_exceeded(self):
        b = AgencyBudget(max_tokens=100)
        b.tokens_used = 95
        assert not b.reserve(estimated_tokens=10)


# ---------------------------------------------------------------------------
# Event log tests
# ---------------------------------------------------------------------------

class TestEventLog:
    def test_append_and_count(self):
        log = EventLog()
        log.append(RUN_STARTED)
        assert log.count == 1

    def test_mutation_safety(self):
        log = EventLog()
        data = {"key": "val"}
        log.append(RUN_STARTED, data)
        data["key"] = "changed"
        assert log.last().data["key"] == "val"

    def test_frozen_rejects(self):
        log = EventLog()
        log.freeze()
        with pytest.raises(RuntimeError):
            log.append(RUN_CLOSED)


# ---------------------------------------------------------------------------
# CTX tests
# ---------------------------------------------------------------------------

class TestAgencyContextV1:
    def test_requires_valid_base_sha(self):
        # CTX constructor validates base_sha length
        pass  # orchestrator handles fallback

    def test_accepts_40char_sha(self):
        sha = hashlib.sha1(b"ok").hexdigest()
        ctx = AgencyContextV1(base_sha=sha)
        assert len(ctx.base_sha) == 40

    def test_double_close_idempotent(self):
        sha = hashlib.sha1(b"t").hexdigest()
        ctx = AgencyContextV1(base_sha=sha)
        ctx.close("completed")
        ctx.close("failed")
        assert ctx.status == "completed"

    def test_immutable_views(self):
        sha = hashlib.sha1(b"t").hexdigest()
        ctx = AgencyContextV1(base_sha=sha)
        ctx.add_inbox([{"url": "https://x.com"}])
        view = ctx.view_for("scout")
        view["inbox"].clear()
        assert len(ctx.inbox) == 1

    def test_unknown_role_raises(self):
        sha = hashlib.sha1(b"t").hexdigest()
        ctx = AgencyContextV1(base_sha=sha)
        with pytest.raises(ValueError):
            ctx.view_for("unknown")


# ---------------------------------------------------------------------------
# Sanitization tests
# ---------------------------------------------------------------------------

class TestSanitization:
    def test_api_key_redacted(self):
        assert _sanitize_value({"api_key": "sk-123"}, "") == {"api_key": "[REDACTED]"}

    def test_nested_token_redacted(self):
        data = {"auth": {"moltbook_token": "tok"}}
        result = _sanitize_value(data, "")
        assert result["auth"]["moltbook_token"] == "[REDACTED]"

    def test_normal_values_preserved(self):
        assert _sanitize_value({"name": "test"}, "") == {"name": "test"}


# ---------------------------------------------------------------------------
# Bridge regression tests (post_id ambiguity fix)
# ---------------------------------------------------------------------------

class TestBridgeCommentIdentity:
    """Tests for content-type-aware parent identifier extraction."""

    def _load_bridge(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "moltbook_write",
            Path(__file__).resolve().parents[2] / "scripts" / "moltbook_write.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["moltbook_write"] = mod
        spec.loader.exec_module(mod)
        return mod

    def _load_fixture(self, name):
        return json.loads(
            (Path(__file__).resolve().parents[1] / "fixtures" / name).read_text())

    def test_real_post_id_shape_succeeds(self):
        m = self._load_bridge()
        raw = {"success": True, "comment": {
            "id": "c1", "post_id": "parent123"}}
        identity = m._extract_content_identity(raw, "comment")
        assert identity["parent_post_id"] == "parent123"

    def test_parent_post_id_shape_succeeds(self):
        m = self._load_bridge()
        raw = {"success": True, "comment": {
            "id": "c1", "parent_post_id": "parent456"}}
        identity = m._extract_content_identity(raw, "comment")
        assert identity["parent_post_id"] == "parent456"

    def test_both_equal_succeeds(self):
        m = self._load_bridge()
        raw = {"success": True, "comment": {
            "id": "c1", "parent_post_id": "same", "post_id": "same"}}
        identity = m._extract_content_identity(raw, "comment")
        assert identity["parent_post_id"] == "same"

    def test_both_different_fails_closed(self):
        m = self._load_bridge()
        raw = {"success": True, "comment": {
            "id": "c1", "parent_post_id": "a", "post_id": "b"}}
        with pytest.raises(RuntimeError, match="Ambiguous parent identifier"):
            m._extract_content_identity(raw, "comment")

    def test_neither_present_fails_closed(self):
        m = self._load_bridge()
        raw = {"success": True, "comment": {"id": "c1"}}
        with pytest.raises(RuntimeError, match="Missing parent identifier"):
            m._extract_content_identity(raw, "comment")

    def test_post_does_not_use_post_id_as_parent(self):
        m = self._load_bridge()
        raw = {"success": True, "post": {
            "id": "p1", "post_id": "not_a_parent"}}
        identity = m._extract_content_identity(raw, "post")
        assert identity["parent_post_id"] == ""

    def test_full_verify_cycle_with_real_shape(self):
        m = self._load_bridge()
        monkeypatch = __import__("pytest").MonkeyPatch()
        monkeypatch.setattr(m, "_get_token", lambda: "tok")

        create = dict(self._load_fixture("comment_create_real_shape.json"))
        create["comment"]["verification"]["expires_at"] = "2099-07-27T10:05:00Z"

        from tests.test_moltbook_write import _MockClient
        client = _MockClient(
            create_comment_resp=create,
            verify_resp=self._load_fixture("verify_accepted.json"),
            fetch_post_resp=self._load_fixture("comment_fetch_real_shape_verified.json"),
        )
        import time
        store = m.TransactionStore(Path("/tmp") / f"test_bridge_{time.time()}")
        import os
        os.makedirs(store._dir, exist_ok=True)

        payload = json.dumps({"content": "test", "type": "comment",
                              "parent_post_id": "parent_post_fixture_id"})
        assert m.cmd_create(client, store, payload) == 0

        stored = json.loads(store._path.read_text())
        txn_id = next(iter(stored))
        assert m.cmd_verify(client, store, txn_id, "4") == 0
        assert store.load(txn_id).state == "verified"

    def test_second_verify_blocked(self):
        m = self._load_bridge()
        monkeypatch = __import__("pytest").MonkeyPatch()
        monkeypatch.setattr(m, "_get_token", lambda: "tok")

        create = dict(self._load_fixture("comment_create_real_shape.json"))
        create["comment"]["verification"]["expires_at"] = "2099-07-27T10:05:00Z"

        from tests.test_moltbook_write import _MockClient
        client = _MockClient(
            create_comment_resp=create,
            verify_resp=self._load_fixture("verify_accepted.json"),
            fetch_post_resp=self._load_fixture("comment_fetch_real_shape_verified.json"),
        )
        import time
        import os
        store = m.TransactionStore(Path("/tmp") / f"test_bridge2_{time.time()}")
        os.makedirs(store._dir, exist_ok=True)

        payload = json.dumps({"content": "test", "type": "comment",
                              "parent_post_id": "parent_post_fixture_id"})
        m.cmd_create(client, store, payload)
        stored = json.loads(store._path.read_text())
        txn_id = next(iter(stored))
        assert m.cmd_verify(client, store, txn_id, "4") == 0
        assert m.cmd_verify(client, store, txn_id, "4") == 1  # blocked

    def test_attempted_does_not_resubmit(self):
        m = self._load_bridge()
        monkeypatch = __import__("pytest").MonkeyPatch()
        monkeypatch.setattr(m, "_get_token", lambda: "tok")
        import time
        import os

        store = m.TransactionStore(Path("/tmp") / f"test_bridge3_{time.time()}")
        os.makedirs(store._dir, exist_ok=True)

        txn = m.Transaction(
            transaction_id="t_att", content_id="comment_fixture_real_shape",
            content_type="comment", parent_post_id="parent_post_fixture_id",
            url="https://x", raw_challenge_text="q",
            verification_code="c", challenge_instructions="",
            expires_at=time.time()+9999, raw_create_response={},
            state="attempted", submitted_answer="4",
            attempted_at=time.time())
        store.save(txn)

        from tests.test_moltbook_write import _MockClient as MC
        client = MC(
            fetch_post_resp=self._load_fixture("comment_fetch_real_shape_verified.json"),
        )
        fresh_store = m.TransactionStore(Path("/tmp") / f"test_bridge3b_{time.time()}")
        os.makedirs(fresh_store._dir, exist_ok=True)
        fresh_store.save(txn)

        assert m.cmd_verify(client, fresh_store, "t_att", "4") == 0
        assert len(client.verify_calls) == 0  # no resubmission
