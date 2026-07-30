#!/usr/bin/env python3
"""Moltbook Agency — run one agency shift locally.

Usage:
    python scripts/moltbook_agency.py --mode dry-run [--shift morning|evening]
    python scripts/moltbook_agency.py --mode observe [--shift morning|evening]
"""
from __future__ import annotations

import argparse
import json
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
    """Load config using tomllib. Fails on missing or invalid file."""
    import tomllib
    config_path = _repo_root / "config" / "moltbook_agency.toml"
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    required = ["automation_enabled", "moltbook_read_only", "models", "budget",
                "active_inquiry", "max_active_inquiries"]
    for field in required:
        if field not in cfg:
            raise ValueError(f"Missing required config field: {field}")
    VALID_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}
    models = cfg.get("models", {})
    if models.get("flash", "") not in VALID_MODELS:
        raise ValueError(f"Invalid flash model: {models.get('flash')}")
    if models.get("pro", "") not in VALID_MODELS:
        raise ValueError(f"Invalid pro model: {models.get('pro')}")
    VALID_TIERS = {"flash", "pro", "deterministic"}
    roles = cfg.get("roles", {})
    for role in ["scout", "records_clerk", "evidence_analyst"]:
        tier = roles.get(role, "")
        if tier not in VALID_TIERS:
            raise ValueError(f"Invalid role tier for {role}: {tier}")
    if cfg.get("max_active_inquiries", 1) != 1:
        raise ValueError("max_active_inquiries must be 1")
    if not cfg.get("moltbook_read_only", True):
        raise ValueError("moltbook_read_only must be true in Observe")
    if cfg.get("allow_original_posts", False):
        raise ValueError("allow_original_posts must be false")
    # active_inquiry_objective — required, non-empty, not equal to inquiry ID
    objective = (cfg.get("active_inquiry_objective") or "").strip()
    if not objective:
        raise ValueError("active_inquiry_objective must be a non-empty string")
    if objective == cfg.get("active_inquiry", ""):
        raise ValueError("active_inquiry_objective must not equal active_inquiry (UUID)")
    cfg["active_inquiry_objective"] = objective
    # internal_author_handles — required non-empty list of non-empty strings
    internal_authors = cfg.get("internal_author_handles", [])
    if not isinstance(internal_authors, list) or not internal_authors:
        raise ValueError("internal_author_handles must be a non-empty list")
    for h in internal_authors:
        if not isinstance(h, str) or not h.strip():
            raise ValueError("internal_author_handles entries must be non-empty strings")
    budget = cfg.get("budget", {})
    for k in ["max_role_calls", "max_tokens", "max_cost_estimate"]:
        v = budget.get(k)
        if v is None or v <= 0:
            raise ValueError(f"budget.{k} must be positive, got {v}")
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="Moltbook Agency — single shift runner")
    parser.add_argument("--mode", choices=["dry-run", "observe"], default="dry-run")
    parser.add_argument("--shift", choices=["morning", "evening"], default="morning")
    parser.add_argument("--trigger", choices=["manual", "scheduled", "dispatch"], default="manual")
    parser.add_argument("--output", type=str, default="", help="HQ report output path")
    parser.add_argument("--ctx-output", type=str, default="", help="CTX artifact output path")
    parser.add_argument("--workflow-run-id", type=str, default=None, help="GitHub Actions run ID")
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
        workflow_run_id=args.workflow_run_id,
        policy_config={"dry_run": is_dry,
                       "automation_enabled": cfg.get("automation_enabled", False),
                       "moltbook_read_only": True,
                       "max_writes_per_run": cfg.get("max_writes_per_run", 1),
                       "require_approval_for_write": True,
                       "allow_original_posts": False},
        budget=budget, repo_provider=repo_provider, role_registry=role_registry,
        campaign={"active_inquiry": cfg.get("active_inquiry", ""),
                  "objective": cfg["active_inquiry_objective"],
                  "internal_author_handles": cfg["internal_author_handles"]})

    # Load durable evidence index BEFORE execution
    from agency.evidence_index import load_evidence_index
    try:
        evidence_ids = load_evidence_index()
        orch.ctx.set_evidence_index(evidence_ids)
    except Exception as exc:
        print(f"Error loading evidence index: {exc}", file=sys.stderr)
        return 1

    ctx = orch.run()

    # Generate HQ report and CTX artifact from same run
    report = render_hq_markdown(ctx.to_dict(sanitize=True))
    if args.output:
        Path(args.output).write_text(report)
        print(f"HQ report written to {args.output}")
    else:
        print(report)

    if args.ctx_output:
        d = ctx.to_dict(sanitize=True)
        from agency.validate_ctx import validate_sanitized_ctx
        errs = validate_sanitized_ctx(d)
        if errs:
            for e in errs:
                print(f"CTX validation error: {e}", file=sys.stderr)
            return 1
        Path(args.ctx_output).write_text(json.dumps(d, indent=2, default=str))
        print(f"CTX artifact written to {args.ctx_output}")

    if ctx.status == "completed":
        return 0
    print(f"\nRun ended: {ctx.status}", file=sys.stderr)
    return 1 if ctx.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
