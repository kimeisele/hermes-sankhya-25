"""Tests for AgencyContextV1, budget, events, and bridge regression."""
from __future__ import annotations

import hashlib
import json
import os
import time
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agency.context import AgencyContextV1, AgencyBudget, _sanitize_value
from agency.events import EventLog, RUN_STARTED, RUN_CLOSED


def _sha(s="t"):
    return hashlib.sha1(s.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class TestBudget:
    def test_default(self):
        b = AgencyBudget()
        assert not b.is_exhausted

    def test_exhaustion(self):
        b = AgencyBudget(max_role_calls=3)
        b.role_calls_used = 3
        assert b.is_exhausted

    def test_reserve_reconcile(self):
        b = AgencyBudget(max_tokens=1000)
        assert b.reserve(estimated_tokens=100)
        b.reconcile(100, 50, 0.01, 0.005)
        assert b.tokens_used == 50


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class TestEvents:
    def test_append(self):
        log = EventLog()
        log.append(RUN_STARTED)
        assert log.count == 1

    def test_mutation_safety(self):
        log = EventLog()
        data = {"k": "v"}
        log.append(RUN_STARTED, data)
        data["k"] = "changed"
        assert log.last().data["k"] == "v"

    def test_frozen(self):
        log = EventLog()
        log.freeze()
        with pytest.raises(RuntimeError):
            log.append(RUN_CLOSED)


# ---------------------------------------------------------------------------
# CTX
# ---------------------------------------------------------------------------

class TestCTX:
    def test_requires_40char_sha(self):
        from agency.context import RepoStateProvider
        class EmptyProvider(RepoStateProvider):
            def current_sha(self): return ""
        with pytest.raises(ValueError):
            AgencyContextV1(base_sha="", repo_provider=EmptyProvider())

    def test_accepts_40char(self):
        ctx = AgencyContextV1(base_sha=_sha())
        assert len(ctx.base_sha) == 40

    def test_double_close_idempotent(self):
        ctx = AgencyContextV1(base_sha=_sha())
        ctx.close("completed")
        ctx.close("failed")
        assert ctx.status == "completed"

    def test_immutable_view(self):
        ctx = AgencyContextV1(base_sha=_sha())
        ctx.add_inbox([{"url": "x"}])
        v = ctx.view_for("scout")
        v["inbox"].clear()
        assert len(ctx.inbox) == 1


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

class TestSanitization:
    def test_api_key_redacted(self):
        assert _sanitize_value({"api_key": "sk"}, "") == {"api_key": "[REDACTED]"}

    def test_nested_redacted(self):
        r = _sanitize_value({"a": {"b": {"token": "x"}}}, "")
        assert r["a"]["b"]["token"] == "[REDACTED]"

    def test_normal_preserved(self):
        assert _sanitize_value({"name": "x"}, "") == {"name": "x"}


# ---------------------------------------------------------------------------
# Bridge regression
# ---------------------------------------------------------------------------

class TestBridge:
    def _load_bridge(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "moltbook_write",
            Path(__file__).resolve().parents[2] / "scripts" / "moltbook_write.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["moltbook_write"] = mod
        spec.loader.exec_module(mod)
        return mod

    def _fixture(self, name):
        return json.loads((Path(__file__).resolve().parents[1] / "fixtures" / name).read_text())

    def _tmp_store(self, m):
        p = Path("/tmp") / f"bridge_test_{time.time()}_{os.getpid()}"
        store = m.TransactionStore(p)
        os.makedirs(store._dir, exist_ok=True)
        return store

    def test_post_id_shape_succeeds(self):
        m = self._load_bridge()
        r = m._extract_content_identity(
            {"success": True, "comment": {"id": "c1", "post_id": "p123"}}, "comment")
        assert r["parent_post_id"] == "p123"

    def test_parent_post_id_succeeds(self):
        m = self._load_bridge()
        r = m._extract_content_identity(
            {"success": True, "comment": {"id": "c1", "parent_post_id": "p456"}}, "comment")
        assert r["parent_post_id"] == "p456"

    def test_both_equal_succeeds(self):
        m = self._load_bridge()
        r = m._extract_content_identity(
            {"success": True, "comment": {"id": "c1", "parent_post_id": "same", "post_id": "same"}},
            "comment")
        assert r["parent_post_id"] == "same"

    def test_both_different_fails(self):
        m = self._load_bridge()
        with pytest.raises(RuntimeError, match="Ambiguous"):
            m._extract_content_identity(
                {"success": True, "comment": {"id": "c1", "parent_post_id": "a", "post_id": "b"}},
                "comment")

    def test_neither_fails(self):
        m = self._load_bridge()
        with pytest.raises(RuntimeError, match="Missing parent"):
            m._extract_content_identity(
                {"success": True, "comment": {"id": "c1"}}, "comment")

    def test_post_no_post_id_parent(self):
        m = self._load_bridge()
        r = m._extract_content_identity(
            {"success": True, "post": {"id": "p1", "post_id": "not_parent"}}, "post")
        assert r["parent_post_id"] == ""

    def test_full_cycle(self):
        m = self._load_bridge()
        pytest.MonkeyPatch().setattr(m, "_get_token", lambda: "tok")
        from tests.test_moltbook_write import _MockClient

        create = dict(self._fixture("comment_create_real_shape.json"))
        create["comment"]["verification"]["expires_at"] = "2099-07-27T10:05:00Z"

        client = _MockClient(
            create_comment_resp=create,
            verify_resp=self._fixture("verify_accepted.json"),
            fetch_post_resp=self._fixture("comment_fetch_real_shape_verified.json"))
        store = self._tmp_store(m)

        assert m.cmd_create(client, store, json.dumps(
            {"content": "t", "type": "comment", "parent_post_id": "parent_post_fixture_id"})) == 0
        stored = json.loads(store._path.read_text())
        txn_id = next(iter(stored))
        assert m.cmd_verify(client, store, txn_id, "4") == 0
        assert store.load(txn_id).state == "verified"

    def test_second_verify_blocked(self):
        m = self._load_bridge()
        pytest.MonkeyPatch().setattr(m, "_get_token", lambda: "tok")
        from tests.test_moltbook_write import _MockClient

        create = dict(self._fixture("comment_create_real_shape.json"))
        create["comment"]["verification"]["expires_at"] = "2099-07-27T10:05:00Z"
        client = _MockClient(
            create_comment_resp=create,
            verify_resp=self._fixture("verify_accepted.json"),
            fetch_post_resp=self._fixture("comment_fetch_real_shape_verified.json"))
        store = self._tmp_store(m)

        m.cmd_create(client, store, json.dumps(
            {"content": "t", "type": "comment", "parent_post_id": "parent_post_fixture_id"}))
        txn_id = next(iter(json.loads(store._path.read_text())))
        assert m.cmd_verify(client, store, txn_id, "4") == 0
        assert m.cmd_verify(client, store, txn_id, "4") == 1

    def test_attempted_no_resubmit(self):
        m = self._load_bridge()
        pytest.MonkeyPatch().setattr(m, "_get_token", lambda: "tok")
        from tests.test_moltbook_write import _MockClient

        store = self._tmp_store(m)
        txn = m.Transaction(
            transaction_id="t_att", content_id="comment_fixture_real_shape",
            content_type="comment", parent_post_id="parent_post_fixture_id",
            url="https://x", raw_challenge_text="q", verification_code="c",
            challenge_instructions="", expires_at=time.time()+9999,
            raw_create_response={}, state="attempted", submitted_answer="4",
            attempted_at=time.time())
        store.save(txn)

        client = _MockClient(
            fetch_post_resp=self._fixture("comment_fetch_real_shape_verified.json"))
        assert m.cmd_verify(client, store, "t_att", "4") == 0
        assert len(client.verify_calls) == 0
