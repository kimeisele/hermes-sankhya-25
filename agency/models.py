"""Agency model routing — Flash vs Pro tier assignment.

All model routing is explicit and deterministic. No dynamic model
selection at runtime. The Engagement Lead and Agency Director always
use Pro; routine roles always use Flash.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Model tier constants
# ---------------------------------------------------------------------------

FLASH = "deepseek-flash"
PRO = "deepseek-pro"

# ---------------------------------------------------------------------------
# Role → tier mapping
# ---------------------------------------------------------------------------

ROLE_MODEL_MAP: dict[str, str] = {
    "scout": FLASH,
    "records_clerk": FLASH,
    "evidence_analyst": FLASH,
    "agency_director": PRO,
    "engagement_lead": PRO,
    "bridge_executor": "deterministic",  # not a model at all
    "auditor": FLASH,                     # default; escalates to Pro
    "engineering_planner": PRO,
}

# ---------------------------------------------------------------------------
# Tier descriptions
# ---------------------------------------------------------------------------

TIER_DESCRIPTIONS: dict[str, dict[str, Any]] = {
    FLASH: {
        "description": "High-volume, pattern-oriented, bounded-context tasks",
        "roles": ["scout", "records_clerk", "evidence_analyst", "auditor"],
        "cost_profile": "low",
    },
    PRO: {
        "description": "Strategic decisions, engagement drafting, challenge interpretation",
        "roles": ["agency_director", "engagement_lead", "engineering_planner"],
        "cost_profile": "high",
    },
    "deterministic": {
        "description": "Deterministic code execution — no model discretion",
        "roles": ["bridge_executor"],
        "cost_profile": "negligible",
    },
}


def model_for_role(role: str) -> str:
    """Return the model tier for a given role. Raises KeyError for unknown roles."""
    return ROLE_MODEL_MAP[role]


def is_write_critical(role: str) -> bool:
    """True if this role participates in write-critical decisions."""
    return role in ("engagement_lead", "bridge_executor")
