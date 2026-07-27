#!/usr/bin/env python3
"""Moltbook Agency — run one agency shift locally.

Usage:
    python scripts/moltbook_agency.py [--shift morning|evening] [--trigger manual|scheduled]

This is the local execution path. The same logic runs in GitHub Actions
via the observe/engage/materialize workflows.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure agency package is importable
_repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo_root))

from agency.orchestrator import AgencyOrchestrator  # noqa: E402
from agency.hq import render_hq_markdown  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Moltbook Agency — single shift runner")
    parser.add_argument("--shift", choices=["morning", "evening"],
                        default="morning")
    parser.add_argument("--trigger", choices=["manual", "scheduled", "dispatch"],
                        default="manual")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--output", type=str, default="",
                        help="Write HQ report to file")
    args = parser.parse_args()

    # Load config (simplified — in workflow, config is injected)
    orchestrator = AgencyOrchestrator(
        trigger=args.trigger,
        shift=args.shift,
        repository="kimeisele/hermes-sankhya-25",
        base_sha="",  # In production, derived from git
        policy_config={"dry_run": args.dry_run,
                       "automation_enabled": False,
                       "moltbook_read_only": True},
    )

    ctx = orchestrator.run()

    # Render HQ
    report = render_hq_markdown(ctx.to_dict(sanitize=True))

    if args.output:
        Path(args.output).write_text(report)
        print(f"HQ report written to {args.output}")
    else:
        print(report)

    # Return code based on status
    if ctx.status == "completed":
        return 0
    elif ctx.status == "budget_exhausted":
        print("\n⚠️  Budget exhausted — run terminated early", file=sys.stderr)
        return 2
    else:
        print(f"\n❌ Run failed: {ctx.status}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
