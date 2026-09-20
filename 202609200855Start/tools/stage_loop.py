"""Drive TRIAGE / EVIDENCE_EXTRACTION batches through the action entry point.

Each round asks the orchestrator for the current action, dispatches to the
matching builder, submits the envelope, then reads the continuation. Stops when
the module advances or a non-AUTO_CONTINUE continuation is reported.
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

BUILDERS = {
    "record-triage-batch": "build_triage_batch.py",
}


def run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [str(PY), "-X", "utf8", "-B", *args],
        capture_output=True,
        text=True,
        encoding="utf8",
        errors="replace",
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def parse_payload(out: str):
    payload = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--stage", required=True, help="operation name to drive")
    parser.add_argument("--max-rounds", type=int, default=8)
    args = parser.parse_args()

    for round_no in range(1, args.max_rounds + 1):
        code, out = run(
            [
                str(SKILL / "scripts" / "run_research.py"),
                "execute-next-action",
                "--run-dir",
                str(args.run),
            ]
        )
        current = parse_payload(out)
        if current is None:
            print(f"round {round_no}: no state response\n{out[-1200:]}")
            return 1
        operation = current.get("operation")
        if current.get("continuation") != "AUTO_CONTINUE":
            print(
                f"round {round_no}: continuation={current.get('continuation')} "
                f"status={current.get('status')} op={operation} counts={current.get('counts')}"
            )
            return 0
        if operation != args.stage:
            print(f"round {round_no}: advanced to {operation} -> stop")
            return 0

        action_id = current.get("action_id") or ""
        action_file = (
            args.run / "staging" / "pipeline" / "operational" / f"action-{action_id}.json"
        )
        if not action_file.exists():
            print(f"round {round_no}: action file missing for {action_id}")
            return 1
        builder = TOOLS / BUILDERS[operation]
        envelope = args.run / "staging_in" / f"env-{args.stage}-{round_no:02d}.json"
        code, out = run(
            [
                str(builder),
                "--run",
                str(args.run),
                "--action-id",
                action_id,
                "--out",
                str(envelope),
            ]
        )
        if code != 0:
            print(f"round {round_no}: build failed\n{out[-1500:]}")
            return 1
        print(f"round {round_no}: {out.strip().splitlines()[0]}")

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
        payload = parse_payload(out)
        if payload is None:
            print(f"round {round_no}: unparsable submit response\n{out[-1500:]}")
            return 1
        counts = payload.get("counts", {})
        print(
            f"round {round_no}: {payload.get('status')} {payload.get('continuation')} "
            f"phase={payload.get('phase')} op={payload.get('operation')} "
            f"remaining={counts.get('remaining_items')}"
        )
        if payload.get("continuation") == "AUTO_CONTINUE":
            if payload.get("operation") != args.stage:
                print("module advanced to", payload.get("operation"), payload.get("phase"))
                return 0
            continue
        if payload.get("continuation") == "TURN_CHECKPOINT":
            # Soft scheduling budget: the next state read resumes the same cursor.
            print("turn checkpoint; continuing")
            continue
        if payload.get("error_code") in ("protected_artifact_tamper", "internal_invariant_failure"):
            # Treated as possibly transient: re-read state and retry this round.
            code, out = run(
                [
                    str(SKILL / "scripts" / "run_research.py"),
                    "execute-next-action",
                    "--run-dir",
                    str(args.run),
                ]
            )
            again = parse_payload(out)
            if (
                again
                and again.get("continuation") == "AUTO_CONTINUE"
                and again.get("operation") == args.stage
            ):
                print(f"round {round_no}: transient blocker, retrying")
                continue
        print("stopping on continuation:", payload.get("continuation"))
        return 0

    print("reached max rounds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
