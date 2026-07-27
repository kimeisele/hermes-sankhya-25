"""Tests for AgencyContextV1, budget, and events."""
from __future__ import annotations

import json
import pytest
from pathlib import Path

# Ensure agency is importable
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agency.context import AgencyContextV1, AgencyBudget
from agency.events import EventLog, AgencyEvent
from agency.events import (RUN_STARTED, RUN_CLOSED, ROLE_COMPLETED,
                           BUDGET_EXHAUSTED)


# ---------------------------------------------------------------------------
# Budget tests
# ---------------------------------------------------------------------------

class TestAgencyBudget:
    def test_default_budget_not_exhausted(self):
        b = AgencyBudget()
        assert not b.is_exhausted

    def test_role_call_limit_exhaustion(self):
        b = AgencyBudget(max_role_calls=3)
        b.record_role_call()
        b.record_role_call()
        b.record_role_call()
        assert b.is_exhausted
        assert b.role_calls_used == 3

    def test_token_budget_exhaustion(self):
        b = AgencyBudget(max_tokens=100)
        b.record_role_call(tokens=100)
        assert b.is_exhausted

    def test_cost_budget_exhaustion(self):
        b = AgencyBudget(max_cost_estimate=2.0)
        b.record_role_call(cost=2.0)
        assert b.is_exhausted

    def test_delegation_counting(self):
        b = AgencyBudget(max_delegation_rounds=2)
        b.record_delegation()
        b.record_delegation()
        assert b.delegation_rounds_used == 2
        assert b.is_exhausted

    def test_to_dict(self):
        b = AgencyBudget()
        d = b.to_dict()
        assert d["max_role_calls"] == 20
        assert d["role_calls_used"] == 0


# ---------------------------------------------------------------------------
# Event log tests
# ---------------------------------------------------------------------------

class TestEventLog:
    def test_append_increases_count(self):
        log = EventLog()
        assert log.count == 0
        log.append(RUN_STARTED, {"run_id": "r1"})
        assert log.count == 1

    def test_events_are_sequenced(self):
        log = EventLog()
        e1 = log.append(RUN_STARTED)
        e2 = log.append(ROLE_COMPLETED)
        assert e1.sequence == 0
        assert e2.sequence == 1

    def test_invalid_event_type_raises(self):
        with pytest.raises(ValueError):
            AgencyEvent("INVALID_TYPE", 0)

    def test_last_returns_most_recent(self):
        log = EventLog()
        log.append(RUN_STARTED)
        log.append(ROLE_COMPLETED)
        assert log.last().event_type == ROLE_COMPLETED

    def test_has_event_type(self):
        log = EventLog()
        log.append(RUN_STARTED)
        assert log.has_event_type(RUN_STARTED)
        assert not log.has_event_type(BUDGET_EXHAUSTED)

    def test_to_list_serializable(self):
        log = EventLog()
        log.append(RUN_STARTED, {"run_id": "r1"})
        lst = log.to_list()
        assert len(lst) == 1
        assert lst[0]["event_type"] == RUN_STARTED
        json.dumps(lst)  # must not raise


# ---------------------------------------------------------------------------
# CTX tests
# ---------------------------------------------------------------------------

class TestAgencyContextV1:
    def test_creation_defaults(self):
        ctx = AgencyContextV1()
        assert ctx.schema_version == "1.0"
        assert ctx.status == "initialized"
        assert ctx.run_id != ""

    def test_creation_with_params(self):
        ctx = AgencyContextV1(
            trigger="scheduled", shift="morning",
            base_sha="a" * 40)
        assert ctx.trigger == "scheduled"
        assert ctx.shift == "morning"
        assert ctx.base_sha == "a" * 40

    def test_run_started_event_appended(self):
        ctx = AgencyContextV1()
        assert ctx.events.has_event_type(RUN_STARTED)

    def test_close_sets_status(self):
        ctx = AgencyContextV1()
        ctx.close("completed")
        assert ctx.status == "completed"
        assert ctx.completed_at is not None
        assert ctx.events.has_event_type(RUN_CLOSED)

    def test_append_event(self):
        ctx = AgencyContextV1()
        ctx.append_event("INCIDENT_RECORDED", {"desc": "test"})
        assert ctx.events.has_event_type("INCIDENT_RECORDED")

    def test_record_incident(self):
        ctx = AgencyContextV1()
        ctx.record_incident("Test incident", severity="high")
        assert len(ctx.incidents) == 1
        assert ctx.incidents[0]["severity"] == "high"
        assert ctx.events.has_event_type("INCIDENT_RECORDED")

    def test_to_dict_sanitized_excludes_raw(self):
        ctx = AgencyContextV1()
        ctx.inbox.append({"raw": "untrusted content"})
        d = ctx.to_dict(sanitize=True)
        # Sanitized output should not have inbox
        assert "inbox" not in d
        assert "events" in d

    def test_to_dict_unsanitized_includes_raw(self):
        ctx = AgencyContextV1()
        ctx.inbox.append({"raw": "untrusted content"})
        d = ctx.to_dict(sanitize=False)
        assert "inbox" in d
        assert len(d["inbox"]) == 1

    def test_no_secrets_in_ctx(self):
        ctx = AgencyContextV1()
        ctx_dict = ctx.to_dict(sanitize=True)
        ctx_str = json.dumps(ctx_dict)
        # These must not appear as values or credential fields
        for secret_pattern in ["api_key", "Bearer", "Authorization",
                               "password", "MOLTBOOK_TOKEN",
                               "access_token", "client_secret"]:
            assert secret_pattern.lower() not in ctx_str.lower(), \
                f"Secret pattern '{secret_pattern}' found in CTX"

    def test_to_json_serializable(self):
        ctx = AgencyContextV1()
        j = ctx.to_json()
        assert isinstance(j, str)
        data = json.loads(j)
        assert data["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# Role context view tests
# ---------------------------------------------------------------------------

class TestRoleViews:
    def test_scout_view_includes_inbox(self):
        ctx = AgencyContextV1()
        ctx.inbox.append({"url": "https://example.com"})
        view = ctx.view_for("scout")
        assert "inbox" in view
        assert len(view["inbox"]) == 1

    def test_director_view_includes_budget(self):
        ctx = AgencyContextV1()
        view = ctx.view_for("agency_director")
        assert "budget" in view
        assert view["budget"]["max_role_calls"] == 20

    def test_auditor_view_includes_transactions(self):
        ctx = AgencyContextV1()
        view = ctx.view_for("auditor")
        assert "transactions" in view

    def test_engagement_lead_view_no_secrets(self):
        ctx = AgencyContextV1()
        view = ctx.view_for("engagement_lead")
        view_str = json.dumps(view)
        assert "token" not in view_str.lower()
        assert "secret" not in view_str.lower()

    def test_unknown_role_returns_base_view(self):
        ctx = AgencyContextV1()
        view = ctx.view_for("nonexistent_role")
        assert "run_id" in view
        assert "campaign" in view


# ---------------------------------------------------------------------------
# Provenance tests
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_event_with_provenance(self):
        log = EventLog()
        e = log.append("SOURCE_ACCEPTED", {"source_id": "s1"},
                       provenance=["src-001", "run-abc"])
        assert len(e.provenance) == 2

    def test_ctx_base_sha_bound(self):
        sha = "b" * 40
        ctx = AgencyContextV1(base_sha=sha)
        assert ctx.base_sha == sha
