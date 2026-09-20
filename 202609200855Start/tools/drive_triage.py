"""In-process driver for the judgement stages (TRIAGE / EXTRACTION / CODING).

Why in-process: the orchestrator's issued action lives at
`staging/pipeline/operational/action-<id>.json`, but committing that action
replaces the file with a confidential storage reference. Reading the run's own
`Workflow` API is the supported way to see the *exact* issued selector set, and
the contract requires submitting every assigned item.

The driver still goes through the same released code path
(`execute_next_action` -> `record_judgments_batch`) and never writes formal
evidence, admission, scores or audit state itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL = Path(r"C:\Users\PC\.dsh\skills\analyze-historic-place-vitality")
sys.path.insert(0, str(SKILL / "scripts"))

from run_research import Workflow  # noqa: E402
from action_dispatch import contract  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_triage_batch import classify, build_basis  # noqa: E402


def items_from_packet(packet: dict) -> list[dict]:
    return packet.get("items") or [packet]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--max-rounds", type=int, default=6)
    args = parser.parse_args()

    w = Workflow(args.run)
    for round_no in range(1, args.max_rounds + 1):
        packet = w._execute_next_action()
        operation = packet.get("operation") or packet.get("action")
        print(
            f"round {round_no}: {packet.get('status')} {packet.get('continuation')} "
            f"op={operation} phase={packet.get('phase')} counts={packet.get('counts')}"
        )
        if packet.get("operation") != "record-triage-batch":
            print("stopped: current operation is", operation)
            return 0

        assigned = packet.get("items") or []
        if not assigned:
            print("stopped: no assigned triage items")
            return 0

        records = []
        stats: dict[str, int] = {}
        for item in assigned:
            view = item.get("view") or ""
            category, layer, entity, relevant, _promo = classify(item["url"], view)
            decision = {
                "relevant": bool(relevant),
                "source_category": category,
                "content_layer": layer,
                "entity_level": entity,
                "promotion_status": _promo,
            }
            if layer in ("user_post", "user_review", "comment", "reply"):
                basis = build_basis(view)
                if basis is None:
                    decision["content_layer"] = "page_body"
                else:
                    decision["classification_basis"] = basis
            records.append(
                {
                    "page_id": item["page_id"],
                    "analysis_fingerprint": item["analysis_fingerprint"],
                    "decision": decision,
                }
            )
            key = f"{decision['source_category']}/{decision['content_layer']}"
            stats[key] = stats.get(key, 0) + 1

        action_id = packet.get("action_id")
        result = w.execute_next_action({"action_id": action_id, "records": records})
        print(
            f"round {round_no}: submitted {len(records)} -> "
            f"{result.get('status')} {result.get('continuation')} "
            f"op={result.get('operation')} counts={result.get('counts')}"
        )
        for key in sorted(stats):
            print(f"    {key}: {stats[key]}")
        if result.get("continuation") not in ("AUTO_CONTINUE", "TURN_CHECKPOINT"):
            print("stopped on continuation", result.get("continuation"))
            return 0
    print("reached max rounds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
