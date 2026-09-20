"""Reproduce the helper's receipt serialization check per record."""
import json
import sys
from pathlib import Path

SKILL = Path(r"C:\Users\PC\.dsh\skills\analyze-historic-place-vitality")
sys.path.insert(0, str(SKILL / "scripts"))

import storage_contract  # noqa: E402
from storage_contract import _CREDENTIAL  # noqa: E402

pages_dir = Path(sys.argv[1])
urls = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
index = json.loads((pages_dir / "_index.json").read_text(encoding="utf-8-sig"))
by_url = {row["request_url"]: row for row in index}

CRED_KEYS = ("authorization", "proxy-authorization", "cookie", "set-cookie",
             "api_key", "api-key", "access_token", "session_token",
             "session_credential", "password")


def scan_keys(value, path=""):
    hits = []
    stack = [(value, path)]
    while stack:
        item, here = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                lowered = str(key).lower()
                if lowered in CRED_KEYS and child not in (None, False, ""):
                    hits.append((here + "/" + str(key), repr(child)[:120]))
                stack.append((child, here + "/" + str(key)))
        elif isinstance(item, list):
            for i, child in enumerate(item):
                stack.append((child, f"{here}[{i}]"))
    return hits


for url in urls:
    row = by_url.get(url)
    if not row:
        print("NOFILE", url[:80])
        continue
    payload = json.loads((pages_dir / row["file"]).read_text(encoding="utf8"))
    for label, obj in (("full", payload),
                       ("nopage", {k: v for k, v in payload.items() if k != "page_body"})):
        raw = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()
        try:
            storage_contract.reject_credentials(raw)
            status = "clean"
        except Exception as exc:  # noqa: BLE001
            status = type(exc).__name__
        if status != "clean":
            text = raw.decode("utf8", errors="replace")
            pats = [m.group(0)[:150] for m in _CREDENTIAL.finditer(text)]
            print(f"HIT[{label}] {url[:70]} {status}")
            for p in pats[:4]:
                print("   PATTERN:", repr(p))
            for kp in scan_keys(obj)[:4]:
                print("   KEY:", kp)
    print(f"done {url[:70]} full={'clean'}")
