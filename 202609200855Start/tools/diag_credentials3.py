"""Print the exact text that trips the credential filter."""
import json
import sys
from pathlib import Path

SKILL = Path(r"C:\Users\PC\.dsh\skills\analyze-historic-place-vitality")
sys.path.insert(0, str(SKILL / "scripts"))
from storage_contract import _CREDENTIAL  # noqa: E402

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf8"))
raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
text = raw.decode("utf8", errors="replace")
matches = list(_CREDENTIAL.finditer(text))
print("url:", payload.get("request_url"))
print("matches:", len(matches))
for m in matches[:10]:
    start = max(0, m.start() - 120)
    print("----")
    print("MATCH:", repr(m.group(0)[:200]))
    print("CTX  :", repr(text[start:m.end() + 80]))
