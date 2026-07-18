#!/usr/bin/env python3
"""NADI federation daemon — heartbeat + inbox sync for new nodes."""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nadi_kit import NadiNode

log = logging.getLogger("nadi_daemon")

def handle_heartbeat(msg):
    log.info("heartbeat from %s (health=%.2f)", msg.source, msg.payload.get("health", 0))

def handle_default(msg):
    log.info("received op=%s from %s", msg.operation, msg.source)

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="NADI federation daemon")
    parser.add_argument("--once", action="store_true", help="Single sync cycle")
    parser.add_argument("--interval", type=int, default=900, help="Seconds between cycles")
    parser.add_argument("--health", type=float, default=1.0, help="Health score to report")
    parser.add_argument(
        "--head-agent",
        type=str,
        default=None,
        help="Dotted path to a HeadAgent subclass (e.g. my_agents.MyAgent). "
             "If specified, runs its heartbeat() each cycle instead of a plain NADI heartbeat.",
    )
    args = parser.parse_args()

    peer_json = Path("data/federation/peer.json")
    if not peer_json.exists():
        print("ERROR: data/federation/peer.json not found. Run scripts/setup_node.py first.")
        return 1

    node = NadiNode.from_peer_json(peer_json)
    node.on("heartbeat", handle_heartbeat)
    log.info("NADI daemon started for %s", node.agent_id)

    # Load HeadAgent subclass if specified
    head_agent_instance = None
    if args.head_agent:
        try:
            import importlib
            module_path, class_name = args.head_agent.rsplit(".", 1)
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            head_agent_instance = cls(node)
            log.info("HeadAgent loaded: %s (%s)", class_name, head_agent_instance.agent_type)
        except Exception as exc:
            log.error("Failed to load HeadAgent %s: %s — falling back to plain heartbeat", args.head_agent, exc)

    cycle = 0
    while True:
        cycle += 1
        log.info("=== sync cycle %d ===", cycle)

        # HeadAgent cognitive cycle (perceive → judge → act → learn → emit)
        # or plain heartbeat if no HeadAgent specified
        if head_agent_instance is not None:
            try:
                result = head_agent_instance.heartbeat()
                log.info("HeadAgent: %s", result)
            except Exception as exc:
                log.warning("HeadAgent heartbeat failed, falling back: %s", exc)
                node.heartbeat(health=args.health)
        else:
            node.heartbeat(health=args.health)

        # Full sync: pull → process → flush → push
        stats = node.sync()
        log.info("pulled=%d processed=%d pushed=%d expired=%d",
                 stats["pulled"], stats["processed"], stats["pushed"], stats["expired"])

        if args.once:
            break
        time.sleep(args.interval)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
