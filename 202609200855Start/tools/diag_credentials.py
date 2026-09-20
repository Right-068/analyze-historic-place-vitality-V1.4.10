"""Diagnose which fetched page trips the credential filter, and why."""
import json
import sys
from pathlib import Path

SKILL = Path(r"C:\Users\PC\.dsh\skills\analyze-historic-place-vitality")
sys.path.insert(0, str(SKILL / "scripts"))

import storage_contract  # noqa: E402
from storage_contract import _CREDENTIAL  # noqa: E402

pages_dir = Path(sys.argv[1])
index = json.loads((pages_dir / "_index.json").read_text(encoding="utf-8-sig"))

for row in index:
    payload = json.loads((pages_dir / row["file"]).read_text(encoding="utf8"))
    if payload.get("access_status") == "failed":
        continue
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf8")
    try:
        storage_contract.reject_credentials(raw)
        verdict = "clean"
    except Exception as exc:  # noqa: BLE001
        verdict = f"{type(exc).__name__}"
    if verdict != "clean":
        text = raw.decode("utf8", errors="replace")
        hits = [m.group(0)[:120] for m in _CREDENTIAL.finditer(text)]
        print(f"HIT {row['request_url'][:80]} -> {verdict}")
        for hit in hits[:5]:
            print("    ", repr(hit))
    else:
        print(f"ok  {row['request_url'][:80]}")
