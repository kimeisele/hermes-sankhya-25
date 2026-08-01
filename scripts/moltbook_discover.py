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
    from agency.evidence_index import load_evidence_index
    try:
        return load_evidence_index()
    except Exception as exc:
        print(f"Error loading evidence index: {exc}", file=sys.stderr)
        return set()


def main() -> int:
    parser = argparse.ArgumentParser(description="Moltbook global discovery")
    parser.add_argument("--output", type=str, default="/tmp",
                        help="directory for discovery artifacts")
    parser.add_argument("--report", type=str, default="",
                        help="explicit report path")
    parser.add_argument("--candidates", type=str, default="",
                        help="explicit candidates JSON path")
    args = parser.parse_args()

    cfg = _load_config()
    dcfg = DiscoveryConfig.from_dict(cfg)
    dcfg.validate()

    if not dcfg.enabled:
        print("Global discovery disabled", file=sys.stderr)
        return 0

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates_path = Path(args.candidates) if args.candidates else out_dir / "discovery_candidates.json"
    report_path = Path(args.report) if args.report else out_dir / "discovery_report.md"

    client = DiscoveryClient()
    known_ids = _load_evidence_ids()
    discovery = GlobalDiscovery(client, dcfg, known_ids=known_ids)

    try:
        candidates = discovery.run()
    except Exception as exc:
        print(f"Global discovery failed: {exc}", file=sys.stderr)
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
