"""Run the helper's own fetch() then reject_credentials() to find the trigger."""
import json
import sys
import os
from pathlib import Path

SKILL = Path(r"C:\Users\PC\.dsh\skills\analyze-historic-place-vitality")
sys.path.insert(0, str(SKILL / "scripts"))
os.chdir(sys.argv[2])

import prepare_host_input  # noqa: E402
import storage_contract  # noqa: E402
from storage_contract import _CREDENTIAL  # noqa: E402

url = sys.argv[1]
record = prepare_host_input.fetch(url, "curl")
print("fetched:", record.get("final_url", "")[:90])
print("title:", (record.get("page_title") or "")[:80])
raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
text = raw.decode("utf8", errors="replace")
try:
    storage_contract.reject_credentials(raw)
    print("reject_credentials: CLEAN")
except Exception as exc:  # noqa: BLE001
    print("reject_credentials:", type(exc).__name__, exc)
    for m in list(_CREDENTIAL.finditer(text))[:5]:
        print("   MATCH:", repr(m.group(0)[:200]))
        print("   CTX  :", repr(text[max(0, m.start() - 120):m.end() + 80]))
