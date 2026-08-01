#!/usr/bin/env python3
"""Moltbook global discovery — bounded read-only sweep.

Usage:
    python scripts/moltbook_discover.py [--output DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo_root))

from agency.discovery import (  # noqa: E402
    DiscoveryClient, DiscoveryConfig, GlobalDiscovery,
    candidates_to_json, render_report,
)

FAILURE_CODE = "EVIDENCE_INDEX_INVALID"


def _failure_artifacts() -> dict:
    return {
        "status": "failed",
        "failure_code": FAILURE_CODE,
        "candidate_count": 0,
        "model_calls": 0,
        "tokens": 0,
        "external_writes": 0,
        "candidates": [],
    }


def _load_config() -> dict:
    import tomllib
    config_path = _repo_root / "config" / "moltbook_agency.toml"
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    d = cfg.get("global_discovery", {})
    if not isinstance(d, dict):
        raise ValueError("global_discovery must be a table")
    required = ["global_discovery_max_new", "global_discovery_max_top_day",
                "global_discovery_max_comments_day",
                "global_discovery_candidate_cap",
                "global_discovery_excerpt_length"]
    for k in required:
        if k not in d:
            raise ValueError(f"Missing required discovery config: {k}")
    if "global_discovery_strong_terms" not in d or not isinstance(
            d.get("global_discovery_strong_terms"), list):
        raise ValueError("global_discovery_strong_terms must be a list")
    if "global_discovery_secondary_terms" not in d or not isinstance(
            d.get("global_discovery_secondary_terms"), list):
        raise ValueError("global_discovery_secondary_terms must be a list")
    return cfg


def _load_evidence_ids() -> set[str]:
    """Load the evidence index; ANY failure is fatal (fail-closed)."""
    from agency.evidence_index import load_evidence_index
    return load_evidence_index()


def main() -> int:
    parser = argparse.ArgumentParser(description="Moltbook global discovery")
    parser.add_argument("--output", type=str, default="/tmp",
                        help="directory for discovery artifacts")
    parser.add_argument("--report", type=str, default="",
                        help="explicit report path")
    parser.add_argument("--candidates", type=str, default="",
                        help="explicit candidates JSON path")
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates_path = Path(args.candidates) if args.candidates else out_dir / "discovery_candidates.json"
    report_path = Path(args.report) if args.report else out_dir / "discovery_report.md"

    # Config load + validation
    try:
        cfg = _load_config()
        dcfg = DiscoveryConfig.from_dict(cfg)
        dcfg.validate()
    except Exception as exc:
        print(f"Global discovery config error: {exc}", file=sys.stderr)
        candidates_path.write_text(json.dumps(_failure_artifacts(), indent=2))
        report_path.write_text("# Moltbook Global Discovery — FAILED\n\n"
                               f"- status: failed\n- failure_code: {FAILURE_CODE}\n"
                               "- candidate_count: 0\n- model_calls: 0\n- tokens: 0\n"
                               "- external_writes: 0\n")
        return 1

    if not dcfg.enabled:
        print("Global discovery disabled", file=sys.stderr)
        candidates_path.write_text(json.dumps(candidates_to_json([]), indent=2))
        report_path.write_text(render_report([], dcfg))
        return 0

    # Evidence index: fail-closed — any error aborts before any listing GET.
    try:
        known_ids = _load_evidence_ids()
    except Exception:
        print("Global discovery failed: evidence index invalid", file=sys.stderr)
        candidates_path.write_text(json.dumps(_failure_artifacts(), indent=2))
        report_path.write_text("# Moltbook Global Discovery — FAILED\n\n"
                               f"- status: failed\n- failure_code: {FAILURE_CODE}\n"
                               "- candidate_count: 0\n- model_calls: 0\n- tokens: 0\n"
                               "- external_writes: 0\n")
        return 1

    client = DiscoveryClient()
    discovery = GlobalDiscovery(client, dcfg, known_ids=known_ids)

    try:
        candidates = discovery.run()
    except Exception as exc:
        print(f"Global discovery failed: {exc}", file=sys.stderr)
        candidates_path.write_text(json.dumps(_failure_artifacts(), indent=2))
        report_path.write_text("# Moltbook Global Discovery — FAILED\n\n"
                               "- status: failed\n- failure_code: DISCOVERY_RUN_FAILED\n"
                               "- candidate_count: 0\n- model_calls: 0\n- tokens: 0\n"
                               "- external_writes: 0\n")
        return 1

    candidates_path.write_text(json.dumps(candidates_to_json(candidates),
                                          indent=2, ensure_ascii=False))
    report_path.write_text(render_report(candidates, dcfg))

    print(f"Candidates: {len(candidates)}")
    print(f"Wrote {candidates_path}")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
