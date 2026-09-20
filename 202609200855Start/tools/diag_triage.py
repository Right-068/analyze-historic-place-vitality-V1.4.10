"""Diagnostic: run the triage batch handler in-process to expose the real error."""
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
try:
    result = w.record_judgments_batch("triage", envelope["records"])
    print("OK:", json.dumps(result, ensure_ascii=False)[:600])
except Exception as exc:  # noqa: BLE001
    print("TYPE:", type(exc).__name__)
    print("MSG :", str(exc)[:800])
    detail = getattr(exc, "detail", None)
    if detail:
        print("DETAIL:", json.dumps(detail, ensure_ascii=False)[:1200])
    traceback.print_exc()
