"""Agency Context V1 — the shared typed state object.

No secrets. No tokens. No complete Moltbook threads. All external text
is explicitly marked untrusted. CTX views are deep copies — roles can
never mutate internal state through a view.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .events import EventLog, RUN_STARTED, RUN_CLOSED, INCIDENT_RECORDED

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"

# ---------------------------------------------------------------------------
# Secret-sensitive key patterns for recursive redaction
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    "token", "api_key", "apikey", "secret", "password", "credential",
    "authorization", "bearer", "access_key", "private_key",
    "verification_code", "moltbook_token", "deepseek_api_key",
)


def _is_secret_key(key: str) -> bool:
    kl = key.lower().replace("_", "").replace("-", "")
    for pat in _SECRET_PATTERNS:
        if pat.replace("_", "") in kl:
            return True
    return False


def _sanitize_value(value: Any, key: str = "") -> Any:
    """Recursively redact secret-like values."""
    if isinstance(value, dict):
        if _is_secret_key(key):
            return "[REDACTED]"
        return {k: _sanitize_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, key) for item in value]
    if isinstance(value, str) and _is_secret_key(key):
        return "[REDACTED]"
    return value


# ---------------------------------------------------------------------------
# Immutable source snapshot and span data
# ---------------------------------------------------------------------------

_MAX_SPAN_LENGTH = 1000


@dataclass(frozen=True)
class _FrozenSource:
    """Immutable canonical source snapshot — write-once, never mutated."""
    source_id: str
    url: str
    author_handle: str
    content_type: str
    raw_content: str
    source_content_hash: str
    ingested_at: str


@dataclass(frozen=True)
class _Span:
    """A single contiguous text span within a source snapshot."""
    span_id: str
    source_id: str
    source_content_hash: str
    span_index: int
    char_offset_start: int
    char_offset_end: int
    text: str


def _segment_spans(source_id: str, source_content_hash: str,
                   raw_content: str) -> tuple[_Span, ...]:
    """Segment raw_content into deterministic non-overlapping spans.

    Order: paragraph boundaries (``\\n\\n``) → sentence boundaries
    (``. ! ?`` followed by whitespace or end-of-text) → 1000-codepoint
    fallback split at last whitespace before limit.

    No character or whitespace is ever dropped or duplicated —
    concatenating all span texts in index order recovers the original
    ``raw_content`` byte-for-byte.
    """
    spans: list[_Span] = []
    span_index = 0

    paragraphs = raw_content.split("\n\n")
    for pi, para in enumerate(paragraphs):
        if pi > 0:
            para = "\n\n" + para

        # Split paragraph into sentences
        units = _split_sentences(para)

        for unit in units:
            if not unit:
                continue
            _emit_unit(source_id, source_content_hash, unit,
                       spans, span_index)
            span_index = len(spans)

    # Postcondition: concatenation recovers original
    reconstructed = "".join(s.text for s in spans)
    assert reconstructed == raw_content, (
        f"Span segmentation lost or duplicated characters: "
        f"expected {len(raw_content)}, got {len(reconstructed)}")
    return tuple(spans)


def _split_sentences(text: str) -> list[str]:
    """Split text at sentence boundaries (``. ! ?`` + whitespace/end).

    The whitespace following a sentence terminator belongs to the
    preceding sentence.
    """
    result: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ('.', '!', '?'):
            # Check if followed by whitespace or end-of-text
            if i + 1 >= n or text[i + 1] in (' ', '\t', '\n', '\r'):
                # Include trailing whitespace in this sentence
                j = i + 1
                while j < n and text[j] in (' ', '\t', '\n', '\r'):
                    j += 1
                result.append(text[start:j])
                start = j
                i = j
                continue
        i += 1
    # Remaining text (no sentence terminator at end)
    if start < n:
        result.append(text[start:])
    return result


def _emit_unit(source_id: str, source_content_hash: str, text: str,
               spans: list[_Span], span_index: int) -> None:
    """Emit one or more spans for a text unit, splitting at max length."""
    offset = sum(len(s.text) for s in spans)
    remaining = text
    while len(remaining) > _MAX_SPAN_LENGTH:
        split_at = _MAX_SPAN_LENGTH
        # Walk back to nearest whitespace boundary
        for k in range(_MAX_SPAN_LENGTH - 1, 0, -1):
            if remaining[k] in (' ', '\t', '\n', '\r'):
                split_at = k + 1
                break
        chunk = remaining[:split_at]
        if not chunk:
            chunk = remaining[:_MAX_SPAN_LENGTH]
            split_at = _MAX_SPAN_LENGTH
        spans.append(_Span(
            span_id=f"{source_id}/span/{span_index}",
            source_id=source_id,
            source_content_hash=source_content_hash,
            span_index=span_index,
            char_offset_start=offset,
            char_offset_end=offset + len(chunk),
            text=chunk,
        ))
        span_index += 1
        offset += len(chunk)
        remaining = remaining[split_at:]
    if remaining:
        spans.append(_Span(
            span_id=f"{source_id}/span/{span_index}",
            source_id=source_id,
            source_content_hash=source_content_hash,
            span_index=span_index,
            char_offset_start=offset,
            char_offset_end=offset + len(remaining),
            text=remaining,
        ))


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

    def would_exceed(self, tokens: int = 0, cost: float = 0.0) -> bool:
        """Check if a planned call would exceed budget."""
        return (self.role_calls_used + 1 > self.max_role_calls or
                self.tokens_used + tokens > self.max_tokens or
                self.cost_estimate_used + cost > self.max_cost_estimate)

    def reserve(self, estimated_tokens: int = 500,
                estimated_cost: float = 0.01) -> bool:
        """Reserve budget for a planned call. Returns False if would exceed."""
        if self.would_exceed(estimated_tokens, estimated_cost):
            return False
        self.role_calls_used += 1
        self.tokens_used += estimated_tokens
        self.cost_estimate_used += estimated_cost
        return True

    def reconcile(self, estimated_tokens: int, actual_tokens: int,
                  estimated_cost: float, actual_cost: float) -> None:
        """Reconcile reserved budget with actual usage."""
        self.tokens_used = self.tokens_used - estimated_tokens + actual_tokens
        self.cost_estimate_used = (self.cost_estimate_used -
                                   estimated_cost + actual_cost)

    def record_delegation(self) -> bool:
        if self.delegation_rounds_used >= self.max_delegation_rounds:
            return False
        self.delegation_rounds_used += 1
        return True

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
# Repository state provider (injectable for tests)
# ---------------------------------------------------------------------------

class RepoStateProvider:
    """Resolves current repository commit SHA. Injectable for tests."""

    def current_sha(self) -> str:
        import subprocess
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10)
            return result.stdout.strip()
        except Exception:
            return ""

    def origin_main_sha(self) -> str:
        import subprocess
        try:
            result = subprocess.run(
                ["git", "rev-parse", "origin/main"],
                capture_output=True, text=True, timeout=10)
            return result.stdout.strip()
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Agency Context
# ---------------------------------------------------------------------------

class AgencyContextV1:
    """Centralized, typed context for a single agency run."""

    __slots__ = (
        "schema_version", "run_id", "workflow_run_id", "trigger", "shift", "started_at",
        "repository", "base_sha",
        "campaign", "policy", "budget",
        "_inbox", "_source_candidates", "_accepted_evidence",
        "_agent_profiles",
        "_decisions", "_work_queue", "_handoffs",
        "_engagement_proposals", "_engineering_proposals",
        "_transactions", "_incidents",
        "_audit", "status", "completed_at",
        "_event_log", "_repo_provider", "_evidence_index",
        "_raw_sources", "_spans",
    )

    def __init__(self, trigger: str = "manual", shift: str = "morning",
                 repository: str = "kimeisele/hermes-sankhya-25",
                 base_sha: str | None = None,
                 campaign: dict[str, Any] | None = None,
                 policy: dict[str, Any] | None = None,
                 budget: AgencyBudget | None = None,
                 repo_provider: RepoStateProvider | None = None,
                 workflow_run_id: str | None = None) -> None:
        self.schema_version = "1.0"
        self.run_id = str(_uuid.uuid4())
        self.workflow_run_id = workflow_run_id  # GitHub Actions run ID (numeric) or None
        self.trigger = trigger
        self.shift = shift
        self.started_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.repository = repository

        # Resolve base SHA
        self._repo_provider = repo_provider or RepoStateProvider()
        if base_sha is None or base_sha == "":
            self.base_sha = self._repo_provider.current_sha()
        else:
            self.base_sha = base_sha
        if not self.base_sha or len(self.base_sha) != 40:
            raise ValueError(
                f"Invalid base_sha: '{self.base_sha}'. "
                f"Must be a 40-character commit SHA.")

        self.campaign = campaign or {}
        self.policy = policy or {}
        self.budget = budget or AgencyBudget()
        self._evidence_index: set[str] = set()  # durable cross-run dedup IDs
        self._raw_sources: dict[str, _FrozenSource] = {}
        self._spans: dict[str, tuple[_Span, ...]] = {}

        # -- private collections (never exposed directly) --
        self._inbox: list[dict[str, Any]] = []
        self._source_candidates: list[dict[str, Any]] = []
        self._accepted_evidence: list[dict[str, Any]] = []
        self._agent_profiles: dict[str, dict[str, Any]] = {}
        self._decisions: list[dict[str, Any]] = []
        self._work_queue: list[str] = []
        self._handoffs: list[dict[str, Any]] = []
        self._engagement_proposals: list[dict[str, Any]] = []
        self._engineering_proposals: list[dict[str, Any]] = []
        self._transactions: list[dict[str, Any]] = []
        self._incidents: list[dict[str, Any]] = []
        self._audit: dict[str, Any] = {}

        self.status = "initialized"
        self.completed_at: str | None = None

        self._event_log = EventLog()
        self._event_log.append(RUN_STARTED, {"run_id": self.run_id,
                                             "trigger": trigger,
                                             "base_sha": self.base_sha})

    def set_evidence_index(self, ids: set[str]) -> None:
        """Set durable cross-run evidence IDs from committed records."""
        self._evidence_index = set(ids)

    # -- safe accessors (return deep copies) -------------------------------

    @property
    def inbox(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._inbox)

    @property
    def source_candidates(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._source_candidates)

    @property
    def accepted_evidence(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._accepted_evidence)

    @property
    def agent_profiles(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._agent_profiles)

    @property
    def decisions(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._decisions)

    @property
    def work_queue(self) -> list[str]:
        return list(self._work_queue)

    @property
    def engagement_proposals(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._engagement_proposals)

    @property
    def engineering_proposals(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._engineering_proposals)

    @property
    def transactions(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._transactions)

    @property
    def incidents(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._incidents)

    @property
    def audit(self) -> dict[str, Any]:
        return copy.deepcopy(self._audit)

    # -- raw source & span management ------------------------------------

    def set_raw_source(self, source_id: str, url: str,
                       author_handle: str, content_type: str,
                       raw_content: str) -> None:
        """Store an immutable source snapshot and segment it into spans.

        Must only be called once per source_id per run.  A second call
        for the same source_id raises RuntimeError (FAIL_CLOSED).
        """
        if source_id in self._raw_sources:
            raise RuntimeError(
                f"Source snapshot {source_id} already ingested — "
                f"cannot re-populate in the same run")
        source_content_hash = hashlib.sha256(
            raw_content.encode("utf-8")).hexdigest()
        ingested_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self._raw_sources[source_id] = _FrozenSource(
            source_id=source_id, url=url, author_handle=author_handle,
            content_type=content_type, raw_content=raw_content,
            source_content_hash=source_content_hash,
            ingested_at=ingested_at)
        self._spans[source_id] = _segment_spans(
            source_id, source_content_hash, raw_content)

    @property
    def raw_sources(self) -> dict[str, _FrozenSource]:
        """Return a shallow copy — _FrozenSource is immutable."""
        return dict(self._raw_sources)

    def spans_for(self, source_id: str) -> tuple[_Span, ...]:
        """Return spans for a source. Returns empty tuple if unknown."""
        return self._spans.get(source_id, ())

    # -- mutation methods (append-only) ------------------------------------

    def add_inbox(self, items: list[dict[str, Any]]) -> None:
        self._inbox.extend(copy.deepcopy(items))

    def set_source_candidates(self, items: list[dict[str, Any]]) -> None:
        self._source_candidates = copy.deepcopy(items)

    def add_accepted_evidence(self, items: list[dict[str, Any]]) -> None:
        self._accepted_evidence.extend(copy.deepcopy(items))

    def update_agent_profile(self, handle: str,
                             profile: dict[str, Any]) -> None:
        self._agent_profiles[handle] = copy.deepcopy(profile)

    def add_decision(self, decision: dict[str, Any]) -> None:
        self._decisions.append(copy.deepcopy(decision))

    def set_work_queue(self, items: list[str]) -> None:
        self._work_queue = list(items)

    def add_engagement_proposal(self, proposal: dict[str, Any]) -> None:
        self._engagement_proposals.append(copy.deepcopy(proposal))

    def add_engineering_proposal(self, proposal: dict[str, Any]) -> None:
        self._engineering_proposals.append(copy.deepcopy(proposal))

    def add_transaction(self, txn: dict[str, Any]) -> None:
        self._transactions.append(copy.deepcopy(txn))

    def set_audit(self, audit: dict[str, Any]) -> None:
        self._audit = copy.deepcopy(audit)

    # -- event access -------------------------------------------------------

    @property
    def events(self) -> EventLog:
        return self._event_log

    def append_event(self, event_type: str, data: dict[str, Any] | None = None,
                     provenance: list[str] | None = None) -> None:
        self._event_log.append(event_type, data, provenance)

    # -- role context views (immutable deep copies) -------------------------

    def view_for(self, role: str) -> dict[str, Any]:
        """Return a deep-copied, role-specific filtered view."""
        base: dict[str, Any] = {
            "run_id": self.run_id,
            "trigger": self.trigger,
            "shift": self.shift,
            "campaign": copy.deepcopy(self.campaign),
            "base_sha": self.base_sha,
        }
        if role == "scout":
            known = set(e.get("source_id") for e in self._accepted_evidence if e.get("source_id"))
            # Merge with external evidence index (durable cross-run dedup)
            ext_ids = getattr(self, '_evidence_index', None)
            if ext_ids:
                known.update(ext_ids)
            base.update({
                "inbox": copy.deepcopy(self._inbox),
                "known_ids": list(known),
                "source_candidates": copy.deepcopy(self._source_candidates),
            })
        elif role == "records_clerk":
            base.update({"source_candidates": copy.deepcopy(self._source_candidates)})
        elif role == "evidence_analyst":
            # Model-facing view: per-source metadata + spans only.
            # No raw_content, content_excerpt, source_content_hash, offsets.
            sources = []
            for sc in self._source_candidates:
                sid = sc.get("id") or sc.get("source_id", "")
                src_spans = self._spans.get(sid, ())
                if not src_spans:
                    continue
                sources.append({
                    "source_id": sid,
                    "author_handle": sc.get("author_handle", "unknown"),
                    "content_type": sc.get("content_type", "unknown"),
                    "url": sc.get("url", ""),
                    "spans": [{"span_id": s.span_id, "text": s.text}
                              for s in src_spans],
                })
            base.update({"sources": sources})
        elif role == "agency_director":
            # Director sees only the public evidence fields, not internal
            # runtime data (source_content_hash, span_ids, content_excerpt, etc.)
            _DIRECTOR_EV_FIELDS = (
                "source_id", "claim_id", "claim_kind", "claim_text",
                "author_handle", "source_class", "content_type", "url",
            )
            filtered_evidence = [
                {k: v for k, v in ev.items() if k in _DIRECTOR_EV_FIELDS}
                for ev in self._accepted_evidence
            ]
            base.update({
                "accepted_evidence": filtered_evidence,
                "agent_profiles": copy.deepcopy(self._agent_profiles),
                "decisions": copy.deepcopy(self._decisions),
                "work_queue": list(self._work_queue),
                "budget": self.budget.to_dict(),
                "policy": copy.deepcopy(self.policy),
            })
        elif role == "engagement_lead":
            base.update({
                "engagement_proposals": copy.deepcopy(self._engagement_proposals),
                "accepted_evidence": copy.deepcopy(self._accepted_evidence),
            })
        elif role == "bridge_executor":
            base.update({
                "transactions": copy.deepcopy(self._transactions),
                "engagement_proposals": copy.deepcopy(self._engagement_proposals),
            })
        elif role == "auditor":
            base.update({
                "accepted_evidence": copy.deepcopy(self._accepted_evidence),
                "transactions": copy.deepcopy(self._transactions),
                "decisions": copy.deepcopy(self._decisions),
                "budget": self.budget.to_dict(),
                "policy": copy.deepcopy(self.policy),
                "incidents": copy.deepcopy(self._incidents),
                "engagement_proposals": copy.deepcopy(self._engagement_proposals),
            })
        elif role == "engineering_planner":
            base.update({
                "accepted_evidence": copy.deepcopy(self._accepted_evidence),
                "engineering_proposals": copy.deepcopy(self._engineering_proposals),
            })
        else:
            raise ValueError(f"Unknown role: {role}")
        return base

    # -- sanitized durable artifact ----------------------------------------

    def to_dict(self, sanitize: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "workflow_run_id": self.workflow_run_id,
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
            "events": self._event_log.to_list(),
        }
        if sanitize:
            d.update({
                "accepted_evidence": _sanitize_value(
                    copy.deepcopy(self._accepted_evidence)),
                "decisions": _sanitize_value(
                    copy.deepcopy(self._decisions)),
                "work_queue": list(self._work_queue),
                "engagement_proposals": _sanitize_value(
                    copy.deepcopy(self._engagement_proposals)),
                "engineering_proposals": _sanitize_value(
                    copy.deepcopy(self._engineering_proposals)),
                "transactions": _sanitize_value(
                    copy.deepcopy(self._transactions)),
                "incidents": _sanitize_value(
                    copy.deepcopy(self._incidents)),
                "audit": _sanitize_value(copy.deepcopy(self._audit)),
                "agent_profiles": _sanitize_value(
                    copy.deepcopy(self._agent_profiles)),
            })
        else:
            d.update({
                "inbox": copy.deepcopy(self._inbox),
                "source_candidates": copy.deepcopy(self._source_candidates),
                "accepted_evidence": copy.deepcopy(self._accepted_evidence),
                "agent_profiles": copy.deepcopy(self._agent_profiles),
                "decisions": copy.deepcopy(self._decisions),
                "work_queue": list(self._work_queue),
                "handoffs": copy.deepcopy(self._handoffs),
                "engagement_proposals": copy.deepcopy(self._engagement_proposals),
                "engineering_proposals": copy.deepcopy(self._engineering_proposals),
                "transactions": copy.deepcopy(self._transactions),
                "incidents": copy.deepcopy(self._incidents),
                "audit": copy.deepcopy(self._audit),
                "raw_sources": {sid: {"source_id": s.source_id,
                                      "source_content_hash": s.source_content_hash}
                                for sid, s in self._raw_sources.items()},
            })
        return d

    def to_json(self, sanitize: bool = True) -> str:
        return json.dumps(self.to_dict(sanitize=sanitize), indent=2, default=str)

    # -- lifecycle ----------------------------------------------------------

    def close(self, status: str = "completed") -> None:
        if self.status in ("completed", "failed", "budget_exhausted"):
            return  # already closed — idempotent
        self.status = status
        self.completed_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self._event_log.append(RUN_CLOSED, {"status": status,
                                            "completed_at": self.completed_at})
        self._event_log.freeze()

    def record_incident(self, description: str, severity: str = "low",
                        data: dict[str, Any] | None = None) -> None:
        incident = {
            "incident_id": _uuid.uuid4().hex[:12],
            "description": description,
            "severity": severity,
            "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "data": copy.deepcopy(data) if data else {},
        }
        self._incidents.append(incident)
        self._event_log.append(INCIDENT_RECORDED, incident)

    # -- stale-state check --------------------------------------------------

    def is_stale(self) -> bool:
        """Check if origin/main has advanced since run start."""
        current = self._repo_provider.origin_main_sha()
        if not current or not self.base_sha:
            return False
        return current != self.base_sha
