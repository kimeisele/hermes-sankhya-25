"""External-agent profiles — multidimensional internal decision aids.

Not a single "trust score." Not public rankings. Based on observed
substantive contributions, not karma or follower counts.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

# Relationship stages from repository policy
RELATIONSHIP_STAGES = [
    "observed",
    "engaged",
    "repeat_peer",
    "evidence_contributor",
    "review_candidate",
    "collaboration_candidate",
]


class AgentProfile:
    """Internal profile for an external agent observed on Moltbook."""

    def __init__(self, handle: str) -> None:
        self.handle = handle
        self.topics: list[str] = []
        self.first_seen = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.last_seen = self.first_seen
        self.interaction_count = 0
        self.qualified_contribution_count = 0
        self.inspectable_evidence_count = 0
        self.verified_claim_count = 0
        self.supported_claim_count = 0
        self.disputed_claim_count = 0
        self.response_rate = 0.0
        self.relationship_stage = "observed"
        self.strengths: list[str] = []
        self.known_limitations: list[str] = []
        self.confidence = 0.0
        self.source_refs: list[str] = []

    def record_interaction(self, qualified: bool = False,
                           evidence: bool = False) -> None:
        self.interaction_count += 1
        self.last_seen = _dt.datetime.now(_dt.timezone.utc).isoformat()
        if qualified:
            self.qualified_contribution_count += 1
        if evidence:
            self.inspectable_evidence_count += 1

    def update_stage(self) -> None:
        """Heuristic stage progression based on contribution counts."""
        if self.qualified_contribution_count >= 10:
            self.relationship_stage = "collaboration_candidate"
        elif self.qualified_contribution_count >= 5:
            self.relationship_stage = "review_candidate"
        elif self.qualified_contribution_count >= 3:
            self.relationship_stage = "evidence_contributor"
        elif self.interaction_count >= 2:
            self.relationship_stage = "repeat_peer"
        elif self.interaction_count >= 1:
            self.relationship_stage = "engaged"

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "topics": self.topics,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "interaction_count": self.interaction_count,
            "qualified_contribution_count": self.qualified_contribution_count,
            "inspectable_evidence_count": self.inspectable_evidence_count,
            "verified_claim_count": self.verified_claim_count,
            "supported_claim_count": self.supported_claim_count,
            "disputed_claim_count": self.disputed_claim_count,
            "response_rate": self.response_rate,
            "relationship_stage": self.relationship_stage,
            "strengths": self.strengths,
            "known_limitations": self.known_limitations,
            "confidence": self.confidence,
            "source_refs": self.source_refs,
        }
