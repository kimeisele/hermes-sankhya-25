#!/usr/bin/env python3
"""Moltbook Agency — run one agency shift locally.

Usage:
    python scripts/moltbook_agency.py --mode dry-run [--shift morning|evening]
    python scripts/moltbook_agency.py --mode observe [--shift morning|evening]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo_root))

from agency.context import AgencyBudget, RepoStateProvider  # noqa: E402
from agency.orchestrator import AgencyOrchestrator, build_role_registry  # noqa: E402
from agency.policy import load_policy_from_config  # noqa: E402
from agency.hq import render_hq_markdown  # noqa: E402


def _resolve_base_sha() -> str:
    import subprocess
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=10, cwd=str(_repo_root))
        return r.stdout.strip()
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Moltbook Agency — single shift runner")
    parser.add_argument("--mode", choices=["dry-run", "observe"], default="dry-run")
    parser.add_argument("--shift", choices=["morning", "evening"], default="morning")
    parser.add_argument("--trigger", choices=["manual", "scheduled", "dispatch"], default="manual")
    parser.add_argument("--output", type=str, default="")
    args = parser.parse_args()

    _policy = load_policy_from_config()  # validates config loadable
    base_sha = _resolve_base_sha()
    if not base_sha or len(base_sha) != 40:
        print("Error: cannot resolve valid base SHA", file=sys.stderr)
        return 1

    is_dry = args.mode == "dry-run"

    # Per-run role factory
    if is_dry:
        role_registry = build_role_registry(client=None)
        # For dry-run, don't fail on stale check
        class _Fixed(RepoStateProvider):
            def current_sha(self): return base_sha
            def origin_main_sha(self): return base_sha
        repo_provider = _Fixed()
    else:
        from agency.model_client import DeepSeekClient  # noqa: E402
        import os
        if not os.environ.get("DEEPSEEK_API_KEY"):
            print("Error: DEEPSEEK_API_KEY required for observe mode", file=sys.stderr)
            return 1
        client = DeepSeekClient()
        role_registry = build_role_registry(client=client)
        repo_provider = RepoStateProvider()

    budget = AgencyBudget(max_role_calls=20, max_delegation_rounds=5,
                          max_tokens=100000, max_cost_estimate=5.0,
                          max_duration_seconds=600)

    orch = AgencyOrchestrator(
        trigger=args.trigger, shift=args.shift, base_sha=base_sha,
        policy_config={"dry_run": is_dry, "automation_enabled": not is_dry,
                       "moltbook_read_only": True, "max_writes_per_run": 0,
                       "require_approval_for_write": True, "allow_original_posts": False},
        budget=budget, repo_provider=repo_provider, role_registry=role_registry)

    ctx = orch.run()
    report = render_hq_markdown(ctx.to_dict(sanitize=True))

    if args.output:
        Path(args.output).write_text(report)
        print(f"HQ report written to {args.output}")
    else:
        print(report)

    if ctx.status == "completed":
        return 0
    print(f"\nRun ended: {ctx.status}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
