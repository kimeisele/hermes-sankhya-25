#!/usr/bin/env python3
"""Render Moltbook Headquarters from a saved CTX JSON file.

Usage:
    python scripts/render_moltbook_hq.py <ctx_file.json> [--format md|html]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo_root))

from agency.hq import render_hq_markdown, render_hq_html  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render Moltbook HQ from CTX JSON")
    parser.add_argument("ctx_file", type=str, help="Path to CTX JSON file")
    parser.add_argument("--format", choices=["md", "html"], default="md")
    parser.add_argument("--output", type=str, default="",
                        help="Write to file instead of stdout")
    args = parser.parse_args()

    ctx_path = Path(args.ctx_file)
    if not ctx_path.exists():
        print(f"Error: {ctx_path} not found", file=sys.stderr)
        return 1

    ctx_dict = json.loads(ctx_path.read_text())

    if args.format == "html":
        output = render_hq_html(ctx_dict)
    else:
        output = render_hq_markdown(ctx_dict)

    if args.output:
        Path(args.output).write_text(output)
        print(f"HQ rendered to {args.output}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
