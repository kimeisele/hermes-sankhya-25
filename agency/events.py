"""Append-only event model for the Agency CTX.

Events are deeply immutable once appended. Data, provenance, and all
nested structures are deep-copied at append time. Returned representations
do not expose mutable internal state.
"""
from __future__ import annotations

import copy
import datetime as _dt
from typing import Any

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

RUN_STARTED = "RUN_STARTED"
CAMPAIGN_LOADED = "CAMPAIGN_LOADED"
SOURCE_OBSERVED = "SOURCE_OBSERVED"
SOURCE_REJECTED = "SOURCE_REJECTED"
SOURCE_ACCEPTED = "SOURCE_ACCEPTED"
ROLE_STARTED = "ROLE_STARTED"
ROLE_COMPLETED = "ROLE_COMPLETED"
ROLE_FAILED = "ROLE_FAILED"
DIRECTOR_DECISION = "DIRECTOR_DECISION"
REPLY_PROPOSED = "REPLY_PROPOSED"
ENGAGEMENT_APPROVED = "ENGAGEMENT_APPROVED"
WRITE_ATTEMPTED = "WRITE_ATTEMPTED"
WRITE_VERIFIED = "WRITE_VERIFIED"
WRITE_INDETERMINATE = "WRITE_INDETERMINATE"
ENGINEERING_PROPOSAL_CREATED = "ENGINEERING_PROPOSAL_CREATED"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
INCIDENT_RECORDED = "INCIDENT_RECORDED"
RUN_CLOSED = "RUN_CLOSED"

VALID_EVENT_TYPES = frozenset({
    RUN_STARTED, CAMPAIGN_LOADED,
    SOURCE_OBSERVED, SOURCE_REJECTED, SOURCE_ACCEPTED,
    ROLE_STARTED, ROLE_COMPLETED, ROLE_FAILED,
    DIRECTOR_DECISION, REPLY_PROPOSED, ENGAGEMENT_APPROVED,
    WRITE_ATTEMPTED, WRITE_VERIFIED, WRITE_INDETERMINATE,
    ENGINEERING_PROPOSAL_CREATED,
    BUDGET_EXHAUSTED, INCIDENT_RECORDED, RUN_CLOSED,
})


# ---------------------------------------------------------------------------
# Immutable event
# ---------------------------------------------------------------------------

class AgencyEvent:
    """An immutable event. All data is deep-copied at construction."""

    __slots__ = ("_event_type", "_timestamp", "_sequence", "_data", "_provenance")

    def __init__(self, event_type: str, sequence: int,
                 data: dict[str, Any] | None = None,
                 provenance: list[str] | None = None) -> None:
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"Unknown event type: {event_type}")
        self._event_type = event_type
        self._timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self._sequence = sequence
        self._data = copy.deepcopy(data) if data else {}
        self._provenance = list(provenance) if provenance else []

    @property
    def event_type(self) -> str:
        return self._event_type

    @property
    def timestamp(self) -> str:
        return self._timestamp

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def data(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    @property
    def provenance(self) -> list[str]:
        return list(self._provenance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self._event_type,
            "timestamp": self._timestamp,
            "sequence": self._sequence,
            "data": copy.deepcopy(self._data),
            "provenance": list(self._provenance),
        }

    def __repr__(self) -> str:
        return f"AgencyEvent({self._event_type}, seq={self._sequence})"


# ---------------------------------------------------------------------------
# Append-only event log
# ---------------------------------------------------------------------------

class EventLog:
    """Append-only event log. Events cannot be deleted, replaced, or
    mutated through any public API."""

    def __init__(self) -> None:
        self._events: list[AgencyEvent] = []
        self._frozen = False

    def append(self, event_type: str, data: dict[str, Any] | None = None,
               provenance: list[str] | None = None) -> AgencyEvent:
        if self._frozen:
            raise RuntimeError("Cannot append to frozen EventLog")
        event = AgencyEvent(event_type, len(self._events), data, provenance)
        self._events.append(event)
        return event

    @property
    def events(self) -> list[AgencyEvent]:
        return list(self._events)

    @property
    def count(self) -> int:
        return len(self._events)

    def to_list(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._events]

    def last(self) -> AgencyEvent | None:
        return self._events[-1] if self._events else None

    def has_event_type(self, event_type: str) -> bool:
        return any(e.event_type == event_type for e in self._events)

    def freeze(self) -> None:
        """Prevent further appends. Idempotent."""
        self._frozen = True
