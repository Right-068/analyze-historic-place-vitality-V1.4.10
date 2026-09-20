"""Drive PAGE_CAPTURE batches until the module advances or a real blocker appears.

Each round: read the current issued action from the run's own state, build the
envelope from the action's own selectors, submit it, and read the continuation.
Stops on any non-AUTO_CONTINUE continuation or when the module leaves
PAGE_CAPTURE. Prints one compact line per round.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
SKILL = Path(r"C:\Users\PC\.dsh\skills\analyze-historic-place-vitality")
PY = Path(sys.executable)


def run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [str(PY), "-X", "utf8", "-B", *args],
        capture_output=True,
        text=True,
        encoding="utf8",
        errors="replace",
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--pages-dir", type=Path, required=True)
    parser.add_argument("--max-rounds", type=int, default=8)
    args = parser.parse_args()

    state_path = args.run / "staging" / "pipeline" / "operational" / "machine-state.json"
    for round_no in range(1, args.max_rounds + 1):
        # Ask the orchestrator for the current action; never infer it.
        code, out = run(
            [
                str(SKILL / "scripts" / "run_research.py"),
                "execute-next-action",
                "--run-dir",
                str(args.run),
            ]
        )
        current = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    current = json.loads(line)
                except ValueError:
                    continue
        if current is None:
            print(f"round {round_no}: no state response\n{out[-1200:]}")
            return 1
        if current.get("continuation") not in ("AUTO_CONTINUE", "TURN_CHECKPOINT"):
            print(
                f"round {round_no}: continuation={current.get('continuation')} "
                f"status={current.get('status')} op={current.get('operation')} -> stop"
            )
            return 0
        if current.get("operation") != "record-pages-batch":
            print(f"round {round_no}: advanced to {current.get('operation')} -> stop")
            return 0

        action_id = current.get("action_id") or ""
        action_file = (
            args.run
            / "staging"
            / "pipeline"
            / "operational"
            / f"action-{action_id}.json"
        )
        if not action_file.exists():
            print(f"round {round_no}: action file missing for {action_id} -> stop")
            return 0
        action = json.loads(action_file.read_text(encoding="utf-8-sig"))

        envelope = args.run / "staging_in" / f"batch-pages-{round_no:02d}.json"
        code, out = run(
            [
                str(TOOLS / "build_page_batch.py"),
                "--action-file",
                str(action_file),
                "--pages-dir",
                str(args.pages_dir),
                "--out",
                str(envelope),
            ]
        )
        if code != 0:
            print(f"round {round_no}: build failed\n{out[-1500:]}")
            return 1
        print(f"round {round_no}: build -> {out.strip().splitlines()[0]}")

        code, out = run(
            [
                str(SKILL / "scripts" / "run_research.py"),
                "execute-next-action",
                "--run-dir",
                str(args.run),
                "--input",
                str(envelope),
            ]
        )
        payload = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
        if payload is None:
            print(f"round {round_no}: unparsable response\n{out[-1500:]}")
            return 1

        counts = payload.get("counts", {})
        print(
            f"round {round_no}: {payload.get('status')} {payload.get('continuation')} "
            f"phase={payload.get('phase')} pages={counts.get('pages')} "
            f"failed={counts.get('failed_items')} remaining={counts.get('remaining_pages')} "
            f"op={payload.get('operation')}"
        )
        if payload.get("continuation") != "AUTO_CONTINUE":
            print("stopping on continuation:", payload.get("continuation"))
            return 0
        if payload.get("operation") != "record-pages-batch":
            print("module advanced to", payload.get("operation"), "phase", payload.get("phase"))
            return 0
    print("reached max rounds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
