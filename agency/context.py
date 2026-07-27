"""Agency Context V1 — the shared typed state object for a single agency run.

No secrets. No tokens. No complete Moltbook threads. All external text
is explicitly marked untrusted. Deterministic transitions only.
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid as _uuid
from pathlib import Path
from typing import Any

from .events import EventLog, RUN_STARTED


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((_SCHEMA_DIR / name).read_text())


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class AgencyBudget:
    """Tracks resource consumption against hard limits."""

    def __init__(self, max_role_calls: int = 20, max_delegation_rounds: int = 5,
                 max_tokens: int = 100000, max_cost_estimate: float = 5.0,
                 max_duration_seconds: int = 600) -> None:
        self.max_role_calls = max_role_calls
        self.max_delegation_rounds = max_delegation_rounds
        self.max_tokens = max_tokens
        self.max_cost_estimate = max_cost_estimate
        self.max_duration_seconds = max_duration_seconds
        self.role_calls_used = 0
        self.delegation_rounds_used = 0
        self.tokens_used = 0
        self.cost_estimate_used = 0.0

    @property
    def is_exhausted(self) -> bool:
        return (self.role_calls_used >= self.max_role_calls or
                self.delegation_rounds_used >= self.max_delegation_rounds or
                self.tokens_used >= self.max_tokens or
                self.cost_estimate_used >= self.max_cost_estimate)

    def record_role_call(self, tokens: int = 0, cost: float = 0.0) -> None:
        self.role_calls_used += 1
        self.tokens_used += tokens
        self.cost_estimate_used += cost

    def record_delegation(self) -> None:
        self.delegation_rounds_used += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_role_calls": self.max_role_calls,
            "max_delegation_rounds": self.max_delegation_rounds,
            "max_tokens": self.max_tokens,
            "max_cost_estimate": self.max_cost_estimate,
            "max_duration_seconds": self.max_duration_seconds,
            "role_calls_used": self.role_calls_used,
            "delegation_rounds_used": self.delegation_rounds_used,
            "tokens_used": self.tokens_used,
            "cost_estimate_used": self.cost_estimate_used,
        }


# ---------------------------------------------------------------------------
# Agency Context
# ---------------------------------------------------------------------------

class AgencyContextV1:
    """Centralized, typed context for a single agency run.

    Roles communicate exclusively through this context. No direct
    peer-to-peer conversations. All transitions are deterministic.
    """

    __slots__ = (
        "schema_version", "run_id", "trigger", "shift", "started_at",
        "repository", "base_sha",
        "campaign", "policy", "budget",
        "inbox", "source_candidates", "accepted_evidence",
        "agent_profiles",
        "decisions", "work_queue", "handoffs",
        "engagement_proposals", "engineering_proposals",
        "transactions", "incidents",
        "audit", "status", "completed_at",
        "_event_log",
    )

    def __init__(self, trigger: str = "manual", shift: str = "morning",
                 repository: str = "kimeisele/hermes-sankhya-25",
                 base_sha: str = "",
                 campaign: dict[str, Any] | None = None,
                 policy: dict[str, Any] | None = None,
                 budget: AgencyBudget | None = None) -> None:
        self.schema_version = "1.0"
        self.run_id = str(_uuid.uuid4())
        self.trigger = trigger
        self.shift = shift
        self.started_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.repository = repository
        self.base_sha = base_sha
        self.campaign = campaign or {}
        self.policy = policy or {}
        self.budget = budget or AgencyBudget()

        # -- dynamic collections --
        self.inbox: list[dict[str, Any]] = []
        self.source_candidates: list[dict[str, Any]] = []
        self.accepted_evidence: list[dict[str, Any]] = []
        self.agent_profiles: dict[str, dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []
        self.work_queue: list[str] = []
        self.handoffs: list[dict[str, Any]] = []
        self.engagement_proposals: list[dict[str, Any]] = []
        self.engineering_proposals: list[dict[str, Any]] = []
        self.transactions: list[dict[str, Any]] = []
        self.incidents: list[dict[str, Any]] = []
        self.audit: dict[str, Any] = {}

        self.status = "initialized"
        self.completed_at: str | None = None

        self._event_log = EventLog()
        self._event_log.append(RUN_STARTED, {"run_id": self.run_id,
                                             "trigger": trigger,
                                             "base_sha": base_sha})

    # -- event access -------------------------------------------------------

    @property
    def events(self) -> EventLog:
        return self._event_log

    def append_event(self, event_type: str, data: dict[str, Any] | None = None,
                     provenance: list[str] | None = None) -> None:
        self._event_log.append(event_type, data, provenance)

    # -- role context views -------------------------------------------------

    def view_for(self, role: str) -> dict[str, Any]:
        """Return a role-specific filtered view of the CTX.

        Roles never receive the complete CTX. The orchestrator constructs
        these views deterministically based on the role.
        """
        base = {
            "run_id": self.run_id,
            "trigger": self.trigger,
            "shift": self.shift,
            "campaign": self.campaign,
        }
        # Scout: needs everything to discover new content
        if role == "scout":
            base.update({
                "inbox": self.inbox,
                "accepted_evidence_ids": [
                    e.get("source_id") for e in self.accepted_evidence
                ],
                "source_candidates": self.source_candidates,
            })
        # Records Clerk: needs candidates
        elif role == "records_clerk":
            base.update({"source_candidates": self.source_candidates})
        # Evidence Analyst: needs accepted evidence
        elif role == "evidence_analyst":
            base.update({"accepted_evidence": self.accepted_evidence})
        # Agency Director: full strategic view
        elif role == "agency_director":
            base.update({
                "source_candidates": self.source_candidates,
                "accepted_evidence": self.accepted_evidence,
                "agent_profiles": self.agent_profiles,
                "decisions": self.decisions,
                "work_queue": self.work_queue,
                "budget": self.budget.to_dict(),
                "policy": self.policy,
            })
        # Engagement Lead: inquiry and proposals
        elif role == "engagement_lead":
            base.update({
                "engagement_proposals": self.engagement_proposals,
                "accepted_evidence": self.accepted_evidence,
            })
        # Auditor: everything except secrets
        elif role == "auditor":
            base.update({
                "accepted_evidence": self.accepted_evidence,
                "transactions": self.transactions,
                "decisions": self.decisions,
                "budget": self.budget.to_dict(),
                "policy": self.policy,
                "incidents": self.incidents,
                "engagement_proposals": self.engagement_proposals,
            })
        # Engineering Planner: evidence + repo context
        elif role == "engineering_planner":
            base.update({
                "accepted_evidence": self.accepted_evidence,
                "engineering_proposals": self.engineering_proposals,
            })
        return base

    # -- serialization ------------------------------------------------------

    def to_dict(self, sanitize: bool = True) -> dict[str, Any]:
        """Serialize to dict. When sanitize=True, excludes internal runtime state."""
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "trigger": self.trigger,
            "shift": self.shift,
            "started_at": self.started_at,
            "repository": self.repository,
            "base_sha": self.base_sha,
            "campaign": self.campaign,
            "policy": self.policy,
            "budget": self.budget.to_dict(),
            "status": self.status,
            "completed_at": self.completed_at,
        }
        if not sanitize:
            d.update({
                "inbox": self.inbox,
                "source_candidates": self.source_candidates,
                "accepted_evidence": self.accepted_evidence,
                "agent_profiles": self.agent_profiles,
                "decisions": self.decisions,
                "work_queue": self.work_queue,
                "handoffs": self.handoffs,
                "engagement_proposals": self.engagement_proposals,
                "engineering_proposals": self.engineering_proposals,
                "transactions": self.transactions,
                "incidents": self.incidents,
                "audit": self.audit,
            })
        d["events"] = self._event_log.to_list()
        return d

    def to_json(self, sanitize: bool = True) -> str:
        return json.dumps(self.to_dict(sanitize=sanitize), indent=2,
                          default=str)

    # -- lifecycle ----------------------------------------------------------

    def close(self, status: str = "completed") -> None:
        self.status = status
        self.completed_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self._event_log.append("RUN_CLOSED", {"status": status,
                                              "completed_at": self.completed_at})

    def record_incident(self, description: str, severity: str = "low",
                        data: dict[str, Any] | None = None) -> None:
        incident = {
            "incident_id": _uuid.uuid4().hex[:12],
            "description": description,
            "severity": severity,
            "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "data": data or {},
        }
        self.incidents.append(incident)
        self._event_log.append("INCIDENT_RECORDED", incident)
