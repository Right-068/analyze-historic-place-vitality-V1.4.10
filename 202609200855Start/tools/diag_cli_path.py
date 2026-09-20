"""Diagnostic: run execute_next_action with the envelope exactly as the CLI does."""
import json
import sys
import traceback
from pathlib import Path

SKILL = Path(r"C:\Users\PC\.dsh\skills\analyze-historic-place-vitality")
sys.path.insert(0, str(SKILL / "scripts"))

run = Path(sys.argv[1])
envelope = json.loads(Path(sys.argv[2]).read_text(encoding="utf8"))

from run_research import Workflow  # noqa: E402

w = Workflow(run)
print("dispatch enabled:", w.action_dispatch_enabled())
try:
    result = w.execute_next_action(envelope)
    print("OK status:", result.get("status"), result.get("continuation"), result.get("operation"))
    print("counts:", result.get("counts"))
except Exception as exc:  # noqa: BLE001
    print("TYPE:", type(exc).__name__)
    print("MSG :", str(exc)[:1000])
    traceback.print_exc()
