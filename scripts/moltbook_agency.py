#!/usr/bin/env python3
"""Moltbook Agency — run one agency shift locally.

Usage:
    python scripts/moltbook_agency.py --mode dry-run [--shift morning|evening]
    python scripts/moltbook_agency.py --mode observe [--shift morning|evening]

Modes:
    dry-run  — validate configuration, run deterministic stubs, no model calls
    observe  — run read-oriented shift (requires DEEPSEEK_API_KEY)

No mode enables Moltbook writes. Writes belong only to the gated Engage path.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo_root))

from agency.context import AgencyBudget, RepoStateProvider  # noqa: E402
from agency.orchestrator import AgencyOrchestrator  # noqa: E402
from agency.policy import load_policy_from_config  # noqa: E402
from agency.hq import render_hq_markdown  # noqa: E402


def _resolve_base_sha() -> str:
    """Resolve current git commit SHA."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=str(_repo_root))
        sha = result.stdout.strip()
        if len(sha) == 40:
            return sha
    except Exception:
        pass
    return hashlib.sha1(b"unknown").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Moltbook Agency — single shift runner")
    parser.add_argument("--mode", choices=["dry-run", "observe"],
                        default="dry-run",
                        help="dry-run: no model calls; observe: read-oriented shift")
    parser.add_argument("--shift", choices=["morning", "evening"],
                        default="morning")
    parser.add_argument("--trigger", choices=["manual", "scheduled", "dispatch"],
                        default="manual")
    parser.add_argument("--output", type=str, default="",
                        help="Write HQ report to file")
    args = parser.parse_args()

    # Load configuration from committed file
    _policy = load_policy_from_config()  # validates config is loadable
    base_sha = _resolve_base_sha()

    # Budget from config
    budget = AgencyBudget(
        max_role_calls=20,
        max_delegation_rounds=5,
        max_tokens=100000,
        max_cost_estimate=5.0,
        max_duration_seconds=600,
    )

    is_dry_run = (args.mode == "dry-run")

    # For dry-run, don't check staleness (use matching provider)
    if is_dry_run:
        class _FixedProvider(RepoStateProvider):
            def origin_main_sha(self):
                return base_sha

        repo_provider = _FixedProvider()
    else:
        repo_provider = RepoStateProvider()

    orchestrator = AgencyOrchestrator(
        trigger=args.trigger,
        shift=args.shift,
        base_sha=base_sha,
        repo_provider=repo_provider,
        policy_config={
            "dry_run": is_dry_run,
            "automation_enabled": not is_dry_run,
            "moltbook_read_only": True,  # CLI never enables writes
            "max_writes_per_run": 0,
            "require_approval_for_write": True,
            "allow_original_posts": False,
        },
        budget=budget,
    )

    ctx = orchestrator.run()

    # Render HQ
    report = render_hq_markdown(ctx.to_dict(sanitize=True))

    if args.output:
        Path(args.output).write_text(report)
        print(f"HQ report written to {args.output}")
    else:
        print(report)

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
