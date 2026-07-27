"""Agency policy — hard rules loaded from committed configuration."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _load_yaml_config(path: str | None = None) -> dict[str, Any]:
    """Load YAML config (no external dependency — simple parser)."""
    if path is None:
        path = str(Path(__file__).resolve().parents[1] / "config" /
                   "moltbook_agency.yaml")
    try:
        with open(path) as f:
            content = f.read()
    except FileNotFoundError:
        return {}

    # Minimal YAML parser for flat/simple nested config
    result: dict[str, Any] = {}
    current_section: dict[str, Any] | None = None
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                current_section = {}
                result[key] = current_section
            elif current_section is not None and not key.startswith("-"):
                # Try to parse value
                parsed: Any = val
                if val.lower() == "true":
                    parsed = True
                elif val.lower() == "false":
                    parsed = False
                elif val.isdigit():
                    parsed = int(val)
                elif val.replace(".", "").isdigit():
                    parsed = float(val)
                current_section[key] = parsed
            else:
                parsed = val
                if val.lower() == "true":
                    parsed = True
                elif val.lower() == "false":
                    parsed = False
                elif val.isdigit():
                    parsed = int(val)
                result[key] = parsed
    return result


def load_policy_from_config(config_path: str | None = None) -> "AgencyPolicy":
    """Load AgencyPolicy from the committed configuration file."""
    cfg = _load_yaml_config(config_path)
    budget_cfg = cfg.get("budget", {})
    return AgencyPolicy({
        "dry_run": cfg.get("dry_run", True),
        "automation_enabled": cfg.get("automation_enabled", False),
        "moltbook_read_only": cfg.get("moltbook_read_only", True),
        "max_active_inquiries": cfg.get("max_active_inquiries", 1),
        "max_writes_per_run": cfg.get("max_writes_per_run", 1),
        "require_approval_for_write": cfg.get("require_approval_for_write", True),
        "allow_original_posts": cfg.get("allow_original_posts", False),
        "budget_max_role_calls": budget_cfg.get("max_role_calls", 20),
        "budget_max_delegation_rounds": budget_cfg.get("max_delegation_rounds", 5),
        "budget_max_tokens": budget_cfg.get("max_tokens", 100000),
        "budget_max_cost_estimate": budget_cfg.get("max_cost_estimate", 5.0),
        "budget_max_duration_seconds": budget_cfg.get("max_duration_seconds", 600),
    })


class AgencyPolicy:
    """Immutable policy for a single agency run."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.dry_run = cfg.get("dry_run", True)
        self.automation_enabled = cfg.get("automation_enabled", False)
        self.moltbook_read_only = cfg.get("moltbook_read_only", True)
        self.max_active_inquiries = cfg.get("max_active_inquiries", 1)
        self.max_writes_per_run = cfg.get("max_writes_per_run", 1)
        self.require_approval_for_write = cfg.get("require_approval_for_write",
                                                   True)
        self.allow_original_posts = cfg.get("allow_original_posts", False)

    def can_write(self) -> bool:
        return (not self.dry_run and
                not self.moltbook_read_only and
                self.automation_enabled)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "automation_enabled": self.automation_enabled,
            "moltbook_read_only": self.moltbook_read_only,
            "max_active_inquiries": self.max_active_inquiries,
            "max_writes_per_run": self.max_writes_per_run,
            "require_approval_for_write": self.require_approval_for_write,
            "allow_original_posts": self.allow_original_posts,
        }
