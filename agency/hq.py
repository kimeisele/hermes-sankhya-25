"""Moltbook Headquarters V1 — sanitized CTX dashboard.

Renders a Markdown dashboard from a completed AgencyContextV1.
Never displays secrets, raw posts, or hidden model reasoning.
"""
from __future__ import annotations

from typing import Any


def render_hq_markdown(ctx_dict: dict[str, Any]) -> str:
    """Render a sanitized Headquarters Markdown dashboard.

    Args:
        ctx_dict: Sanitized CTX dictionary (from ctx.to_dict(sanitize=True))

    Returns:
        Markdown string suitable for issue comments or job summaries.
    """
    lines: list[str] = []

    # Header
    lines.append("# Moltbook Headquarters — Agency Run Summary")
    lines.append("")
    lines.append(f"**Run ID:** `{ctx_dict.get('run_id', 'N/A')}`")
    lines.append(f"**Trigger:** {ctx_dict.get('trigger', 'N/A')}")
    lines.append(f"**Shift:** {ctx_dict.get('shift', 'N/A')}")
    lines.append(f"**Started:** {ctx_dict.get('started_at', 'N/A')}")
    lines.append(f"**Repository:** {ctx_dict.get('repository', 'N/A')}")
    lines.append(f"**Base SHA:** `{ctx_dict.get('base_sha', 'N/A')[:12]}`")
    lines.append(f"**Status:** {ctx_dict.get('status', 'N/A')}")
    lines.append(f"**Completed:** {ctx_dict.get('completed_at', 'N/A')}")
    lines.append("")

    # Campaign
    campaign = ctx_dict.get("campaign", {})
    if campaign:
        lines.append("## Active Campaign")
        lines.append(f"- **Objective:** {campaign.get('objective', 'N/A')}")
        lines.append(f"- **Phase:** {campaign.get('phase', 'N/A')}")
        lines.append("")

    # Budget
    budget = ctx_dict.get("budget", {})
    if budget:
        lines.append("## Budget")
        lines.append(f"- Role calls: {budget.get('role_calls_used', 0)} / {budget.get('max_role_calls', 0)}")
        lines.append(f"- Delegation rounds: {budget.get('delegation_rounds_used', 0)} / {budget.get('max_delegation_rounds', 0)}")
        lines.append(f"- Tokens: {budget.get('tokens_used', 0)} / {budget.get('max_tokens', 0)}")
        lines.append(f"- Cost estimate: ${budget.get('cost_estimate_used', 0):.4f} / ${budget.get('max_cost_estimate', 0):.2f}")
        lines.append("")

    # Events summary
    events = ctx_dict.get("events", [])
    if events:
        lines.append("## Event Summary")
        lines.append(f"Total events: {len(events)}")
        event_types: dict[str, int] = {}
        for e in events:
            et = e.get("event_type", "UNKNOWN")
            event_types[et] = event_types.get(et, 0) + 1
        for et, count in sorted(event_types.items()):
            lines.append(f"- {et}: {count}")
        lines.append("")

    # Policy
    policy = ctx_dict.get("policy", {})
    if policy:
        lines.append("## Policy")
        lines.append(f"- Dry run: {policy.get('dry_run', True)}")
        lines.append(f"- Automation enabled: {policy.get('automation_enabled', False)}")
        lines.append(f"- Moltbook read-only: {policy.get('moltbook_read_only', True)}")
        lines.append("")

    # Security notice
    lines.append("---")
    lines.append("")
    lines.append("*Moltbook Headquarters V1 — control and observability surface only. No secrets, credentials, or raw external content are displayed.*")
    lines.append("")

    return "\n".join(lines)


def render_hq_html(ctx_dict: dict[str, Any]) -> str:
    """Render a minimal HTML dashboard."""
    md = render_hq_markdown(ctx_dict)
    # Minimal HTML wrapper
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Moltbook HQ — {ctx_dict.get('run_id', 'N/A')[:12]}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; }}
h1 {{ border-bottom: 2px solid #333; }}
code {{ background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; }}
</style>
</head>
<body>
<pre>{html_escape(md)}</pre>
</body>
</html>"""


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
