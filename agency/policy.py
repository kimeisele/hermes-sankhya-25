"""Agency policy — hard rules for the V1 agency.

All policy is loaded from committed configuration. No runtime policy
mutation is permitted within a single run.
"""
from __future__ import annotations

from typing import Any


class AgencyPolicy:
    """Immutable policy for a single agency run."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.dry_run = cfg.get("dry_run", True)
        self.automation_enabled = cfg.get("automation_enabled", False)
        self.max_active_inquiries = cfg.get("max_active_inquiries", 1)
        self.max_writes_per_run = cfg.get("max_writes_per_run", 1)
        self.require_approval_for_write = cfg.get("require_approval_for_write", True)
        self.allow_original_posts = cfg.get("allow_original_posts", False)
        self.moltbook_read_only = cfg.get("moltbook_read_only",
                                           True)  # default safe

    def can_write(self) -> bool:
        return (not self.dry_run and
                not self.moltbook_read_only and
                self.automation_enabled)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "automation_enabled": self.automation_enabled,
            "max_active_inquiries": self.max_active_inquiries,
            "max_writes_per_run": self.max_writes_per_run,
            "require_approval_for_write": self.require_approval_for_write,
            "allow_original_posts": self.allow_original_posts,
            "moltbook_read_only": self.moltbook_read_only,
        }
