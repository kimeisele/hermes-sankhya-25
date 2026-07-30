"""Moltbook Headquarters V1 — sanitized CTX dashboard."""
from __future__ import annotations

from typing import Any


def render_hq_markdown(ctx_dict: dict[str, Any]) -> str:
    """Render a sanitized Headquarters Markdown dashboard."""
    lines: list[str] = []

    lines.append("# Moltbook Headquarters — Agency Run Summary")
    lines.append("")
    lines.append(f"**Run ID:** `{ctx_dict.get('run_id', 'N/A')[:12]}`")
    lines.append(f"**Trigger:** {ctx_dict.get('trigger', 'N/A')}")
    lines.append(f"**Shift:** {ctx_dict.get('shift', 'N/A')}")
    lines.append(f"**Started:** {ctx_dict.get('started_at', 'N/A')}")
    lines.append(f"**Base SHA:** `{ctx_dict.get('base_sha', 'N/A')[:12]}`")
    lines.append(f"**Status:** {ctx_dict.get('status', 'N/A')}")
    lines.append(f"**Completed:** {ctx_dict.get('completed_at', 'N/A')}")
    lines.append("")

    # Campaign
    campaign = ctx_dict.get("campaign", {})
    if campaign:
        lines.append("## Active Inquiry")
        lines.append(f"- {campaign.get('objective', campaign.get('title', 'N/A'))}")
        lines.append("")

    # Evidence
    evidence = ctx_dict.get("accepted_evidence", [])
    if evidence:
        lines.append(f"## Accepted Evidence ({len(evidence)})")
        for e in evidence[:10]:
            if isinstance(e, dict):
                lines.append(f"- `{e.get('source_id', e.get('url', '?'))[:40]}` — "
                             f"{e.get('author_handle', '?')}")
        lines.append("")

    # Decisions
    decisions = ctx_dict.get("decisions", [])
    if decisions:
        lines.append("## Director Decisions")
        for d in decisions:
            if isinstance(d, dict):
                lines.append(f"- {d.get('disposition', '?')}: "
                             f"{d.get('rationale', '')[:80]}")
        lines.append("")

    # Research Synthesis (from Director when READY_FOR_SYNTHESIS)
    for d in decisions:
        if not isinstance(d, dict):
            continue
        synthesis = d.get("synthesis")
        if not synthesis:
            continue
        lines.append("## Research Synthesis")
        lines.append("")
        lines.append(f"**Inquiry:** {synthesis.get('inquiry', 'N/A')}")
        lines.append("")
        lines.append(f"**Executive Answer:** {synthesis.get('executive_answer', 'N/A')}")
        lines.append("")
        findings = synthesis.get("findings", [])
        if findings:
            lines.append(f"### Findings ({len(findings)})")
            lines.append("")
            for f in findings:
                if isinstance(f, dict):
                    lines.append(f"- **{f.get('finding_id', '?')}** "
                                 f"[{f.get('finding_kind', '?')}] — "
                                 f"{f.get('statement', '')}")
                    srcs = f.get("source_ids", [])
                    lines.append(f"  - Sources: {', '.join(srcs)}")
                    sqs = f.get("source_quotes", [])
                    for sq in sqs:
                        if isinstance(sq, dict):
                            lines.append(f"    > `{sq.get('source_id','')[:12]}`: "
                                         f"{sq.get('quote','')[:200]}")
                    lines.append(f"  - Confidence: {f.get('confidence', '?')}")
                    lines.append(f"  - Source basis: {f.get('source_basis', '?')}")
                    lines.append(f"  - Distinct authors: "
                                 f"{f.get('distinct_author_count', '?')}")
                    lines.append(f"  - Independent external contributors: "
                                 f"{f.get('independent_external_contributor_count', '?')}")
                    lines.append(f"  - Reasoning: {f.get('reasoning', '')}")
                    lines.append("")
        questions = synthesis.get("unresolved_questions", [])
        if questions:
            lines.append("### Unresolved Questions")
            for q in questions:
                lines.append(f"- {q}")
            lines.append("")

    # Engagement proposals
    eng_props = ctx_dict.get("engagement_proposals", [])
    if eng_props:
        lines.append(f"## Engagement Proposals ({len(eng_props)})")
        for p in eng_props:
            if isinstance(p, dict):
                lines.append(f"- `{p.get('proposal_id', p.get('id', '?'))[:20]}`")
        lines.append("")

    # Engineering proposals
    engr_props = ctx_dict.get("engineering_proposals", [])
    if engr_props:
        lines.append(f"## Engineering Proposals ({len(engr_props)})")
        for p in engr_props:
            if isinstance(p, dict):
                lines.append(f"- {p.get('title', p.get('problem', '?'))[:80]}")
        lines.append("")

    # Transactions
    transactions = ctx_dict.get("transactions", [])
    if transactions:
        lines.append(f"## Moltbook Transactions ({len(transactions)})")
        for t in transactions:
            if isinstance(t, dict):
                lines.append(f"- `{t.get('transaction_id', '?')[:12]}` → "
                             f"{t.get('state', t.get('status', '?'))}")
        lines.append("")

    # Incidents
    incidents = ctx_dict.get("incidents", [])
    if incidents:
        lines.append(f"## Incidents ({len(incidents)})")
        for inc in incidents:
            if isinstance(inc, dict):
                lines.append(f"- [{inc.get('severity', '?')}] "
                             f"{inc.get('description', '?')[:100]}")
        lines.append("")

    # Profiles
    profiles = ctx_dict.get("agent_profiles", {})
    if profiles:
        lines.append(f"## External Agent Profiles ({len(profiles)})")
        for handle, prof in list(profiles.items())[:10]:
            if isinstance(prof, dict):
                stage = prof.get("relationship_stage", "?")
                contribs = prof.get("qualified_contribution_count", 0)
                lines.append(f"- **{handle}** — {stage} ({contribs} contributions)")
        lines.append("")

    # Budget
    budget = ctx_dict.get("budget", {})
    if budget:
        lines.append("## Budget")
        lines.append("| Resource | Used | Limit |")
        lines.append("|---|---|---|")
        lines.append(f"| Role calls | {budget.get('role_calls_used', 0)} | {budget.get('max_role_calls', 0)} |")
        lines.append(f"| Delegations | {budget.get('delegation_rounds_used', 0)} | {budget.get('max_delegation_rounds', 0)} |")
        lines.append(f"| Tokens | {budget.get('tokens_used', 0)} | {budget.get('max_tokens', 0)} |")
        lines.append(f"| Cost | ${budget.get('cost_estimate_used', 0):.4f} | ${budget.get('max_cost_estimate', 0):.2f} |")
        lines.append("")

    # Events summary
    events = ctx_dict.get("events", [])
    if events:
        lines.append(f"## Event Log ({len(events)} events)")
        event_counts: dict[str, int] = {}
        for e in events:
            et = e.get("event_type", "UNKNOWN")
            event_counts[et] = event_counts.get(et, 0) + 1
        for et, cnt in sorted(event_counts.items()):
            lines.append(f"- {et}: {cnt}")
        lines.append("")

    # Policy
    policy = ctx_dict.get("policy", {})
    if policy:
        lines.append("## Policy")
        lines.append(f"- Dry run: {policy.get('dry_run', True)}")
        lines.append(f"- Read-only: {policy.get('moltbook_read_only', True)}")
        lines.append("")

    # Audit
    audit = ctx_dict.get("audit", {})
    if audit:
        passed = audit.get("passed", False)
        findings = audit.get("findings", [])
        status_icon = "✅" if passed else "⚠️"
        lines.append(f"## Audit {status_icon}")
        if findings:
            for f in findings:
                lines.append(f"- {f}")
        lines.append("")

    # Next action
    lines.append("## Next Action")
    status = ctx_dict.get("status", "")
    if status == "completed":
        lines.append("- Review run artifact and accept/reject proposals")
    elif status == "budget_exhausted":
        lines.append("- Budget exhausted — increase limits or reduce scope")
    elif status == "failed":
        lines.append("- Investigate incidents and re-run")
    lines.append("")

    lines.append("---")
    lines.append("*Moltbook Headquarters V1 — sanitized control surface.*")
    lines.append("")

    return "\n".join(lines)


def render_hq_html(ctx_dict: dict[str, Any]) -> str:
    md = render_hq_markdown(ctx_dict)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Moltbook HQ — {ctx_dict.get('run_id', 'N/A')[:12]}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; }}
h1 {{ border-bottom: 2px solid #333; }}
code {{ background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; }}
table {{ border-collapse: collapse; }}
td, th {{ border: 1px solid #ddd; padding: 4px 8px; }}
</style>
</head>
<body>
<pre>{html_escape(md)}</pre>
</body>
</html>"""


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
