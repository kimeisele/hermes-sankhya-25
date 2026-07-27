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
from agency.hq import render_hq_markdown  # noqa: E402


def _resolve_base_sha() -> str:
    import subprocess
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=10, cwd=str(_repo_root))
        return r.stdout.strip()
    except Exception:
        return ""


def _load_config() -> dict:
    """Load config using tomllib (Python 3.11+) with fallback."""
    config_path = _repo_root / "config" / "moltbook_agency.toml"
    if config_path.exists():
        try:
            import tomllib
            with open(config_path, "rb") as f:
                return tomllib.load(f)
        except Exception:
            pass
    # Fallback: return safe defaults
    return {
        "dry_run": True,
        "automation_enabled": False,
        "moltbook_read_only": True,
        "max_active_inquiries": 1,
        "max_writes_per_run": 1,
        "allow_original_posts": False,
        "active_inquiry": "fd2c8049-5a16-417b-ab5d-8400a80d3ca7",
        "budget": {"max_role_calls": 20, "max_delegation_rounds": 5,
                   "max_tokens": 100000, "max_cost_estimate": 5.0,
                   "max_duration_seconds": 600},
        "models": {"flash": "deepseek-v4-flash", "pro": "deepseek-v4-pro"},
        "timeout": 60, "max_output_tokens": 4096,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Moltbook Agency — single shift runner")
    parser.add_argument("--mode", choices=["dry-run", "observe"], default="dry-run")
    parser.add_argument("--shift", choices=["morning", "evening"], default="morning")
    parser.add_argument("--trigger", choices=["manual", "scheduled", "dispatch"], default="manual")
    parser.add_argument("--output", type=str, default="", help="HQ report output path")
    parser.add_argument("--ctx-output", type=str, default="", help="CTX artifact output path")
    args = parser.parse_args()

    cfg = _load_config()
    base_sha = _resolve_base_sha()
    if not base_sha or len(base_sha) != 40:
        print("Error: cannot resolve valid base SHA", file=sys.stderr)
        return 1

    is_dry = args.mode == "dry-run"

    if is_dry:
        role_registry = build_role_registry(client=None, moltbook_reader=None)
        class _Fixed(RepoStateProvider):
            def current_sha(self): return base_sha
            def origin_main_sha(self): return base_sha
        repo_provider = _Fixed()
    else:
        import os
        if not os.environ.get("DEEPSEEK_API_KEY"):
            print("Error: DEEPSEEK_API_KEY required for observe mode", file=sys.stderr)
            return 1
        from agency.model_client import DeepSeekClient  # noqa: E402
        from agency.roles import MoltbookReadClient  # noqa: E402
        client = DeepSeekClient(
            flash_model=cfg["models"]["flash"],
            pro_model=cfg["models"]["pro"],
            timeout=cfg.get("timeout", 60),
            max_output_tokens=cfg.get("max_output_tokens", 4096))
        reader = MoltbookReadClient()
        role_registry = build_role_registry(client=client, moltbook_reader=reader)
        repo_provider = RepoStateProvider()

    budget_cfg = cfg.get("budget", {})
    budget = AgencyBudget(
        max_role_calls=budget_cfg.get("max_role_calls", 20),
        max_delegation_rounds=budget_cfg.get("max_delegation_rounds", 5),
        max_tokens=budget_cfg.get("max_tokens", 100000),
        max_cost_estimate=budget_cfg.get("max_cost_estimate", 5.0),
        max_duration_seconds=budget_cfg.get("max_duration_seconds", 600))

    orch = AgencyOrchestrator(
        trigger=args.trigger, shift=args.shift, base_sha=base_sha,
        policy_config={"dry_run": is_dry,
                       "automation_enabled": cfg.get("automation_enabled", False),
                       "moltbook_read_only": True,
                       "max_writes_per_run": cfg.get("max_writes_per_run", 1),
                       "require_approval_for_write": True,
                       "allow_original_posts": False},
        budget=budget, repo_provider=repo_provider, role_registry=role_registry,
        campaign={"active_inquiry": cfg.get("active_inquiry", ""),
                  "objective": cfg.get("active_inquiry_objective", "")})

    ctx = orch.run()

    # Generate HQ report and CTX artifact from same run
    report = render_hq_markdown(ctx.to_dict(sanitize=True))
    if args.output:
        Path(args.output).write_text(report)
        print(f"HQ report written to {args.output}")
    else:
        print(report)

    if args.ctx_output:
        Path(args.ctx_output).write_text(ctx.to_json(sanitize=True))
        print(f"CTX artifact written to {args.ctx_output}")

    if ctx.status == "completed":
        return 0
    print(f"\nRun ended: {ctx.status}", file=sys.stderr)
    return 1 if ctx.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
