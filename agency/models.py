"""Agency model routing — Flash vs Pro tier assignment."""
from __future__ import annotations

FLASH = "deepseek-v4-flash"
PRO = "deepseek-v4-pro"

ROLE_MODEL_MAP: dict[str, str] = {
    "scout": FLASH,
    "records_clerk": FLASH,
    "evidence_analyst": FLASH,
    "agency_director": PRO,
    "engagement_lead": PRO,
    "bridge_executor": "deterministic",
    "auditor": FLASH,
    "engineering_planner": PRO,
}


def model_for_role(role: str) -> str:
    return ROLE_MODEL_MAP[role]


def is_write_critical(role: str) -> bool:
    return role in ("engagement_lead", "bridge_executor", "agency_director",
                    "engineering_planner")
