"""Build a PAGE_CAPTURE envelope from an action file's own selectors.

- selectors / action ids come from the orchestrator's action record (no invention)
- page facts come from locally retained extracted text (host web_fetch capability)
- selectors not yet retained locally are fetched on demand by the same extractor
- every record is verified with the skill's own importer before submission
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
SKILL = Path(r"C:\Users\PC\.dsh\skills\analyze-historic-place-vitality")
sys.path.insert(0, str(SKILL / "scripts"))

from fetch_pages import fetch  # noqa: E402
from storage_contract import _CREDENTIAL  # noqa: E402
from host_adapters import imported_page  # noqa: E402
from temporal_fields import utc_now  # noqa: E402


def load_index(pages_dir: Path) -> dict:
    for name in ("_index.json", "index.json"):
        path = pages_dir / name
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8-sig"))
            return {row["request_url"]: row for row in rows}
    return {}


def save_index(pages_dir: Path, index: dict) -> None:
    rows = sorted(index.values(), key=lambda r: r["n"])
    for name in ("_index.json", "index.json"):
        (pages_dir / name).write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf8"
        )


def sanitize(body: str) -> tuple[str, bool]:
    """Drop credential-shaped fragments that the storage layer rejects."""
    changed = False
    kept = []
    for line in body.splitlines():
        if _CREDENTIAL.search(line):
            changed = True
            cleaned = _CREDENTIAL.sub("[已移除的凭据样式片段]", line)
            if _CREDENTIAL.search(cleaned):
                continue
            kept.append(cleaned)
        else:
            kept.append(line)
    return "\n".join(kept), changed


def store(pages_dir: Path, index: dict, url: str) -> dict:
    payload = fetch(url)
    n = len(index) + 1
    digest = hashlib.sha256(url.encode("utf8")).hexdigest()[:16]
    name = f"p{n:03d}_{digest}.json"
    (pages_dir / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf8"
    )
    row = {
        "n": n,
        "request_url": url,
        "access_status": payload.get("access_status"),
        "error_type": payload.get("error_type"),
        "body_chars": payload.get("body_chars", 0),
        "file": name,
    }
    index[url] = row
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action-file", type=Path, required=True)
    parser.add_argument("--pages-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-chars", type=int, default=0)
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args()

    action = json.loads(args.action_file.read_text(encoding="utf-8-sig"))
    selectors = action["selectors"]
    action_id = action["action_id"]
    args.pages_dir.mkdir(parents=True, exist_ok=True)
    index = load_index(args.pages_dir)

    fetched_now = []
    for _, url in selectors:
        if url in index or args.no_fetch:
            continue
        row = store(args.pages_dir, index, url)
        fetched_now.append((url, row["access_status"]))
        print(f"FETCH {row['access_status']} {row['body_chars']}c {url[:80]}")
    if fetched_now:
        save_index(args.pages_dir, index)

    records = []
    failures = []
    sanitized = []
    missing = []
    for execution_id, url in selectors:
        row = index.get(url)
        if not row:
            missing.append(url)
            records.append(
                {
                    "execution_id": execution_id,
                    "request_url": url,
                    "error_type": "page_not_retained_locally",
                }
            )
            continue
        payload = json.loads((args.pages_dir / row["file"]).read_text(encoding="utf8"))
        if payload.get("access_status") == "failed":
            failures.append(url)
            records.append(
                {
                    "execution_id": execution_id,
                    "request_url": url,
                    "error_type": payload.get("error_type", "fetch_failed"),
                }
            )
            continue
        body = payload.get("page_body") or ""
        if args.max_chars and len(body) > args.max_chars:
            body = body[: args.max_chars]
        body, changed = sanitize(body)
        if not body.strip() or not (payload.get("page_title") or "").strip():
            # Retained but no usable body/title: record the real failure, do not
            # submit a fabricated or empty capture.
            failures.append(url)
            records.append(
                {
                    "execution_id": execution_id,
                    "request_url": url,
                    "error_type": "page_body_empty_after_extraction",
                }
            )
            continue
        if changed:
            sanitized.append(url)
        record = {
            "execution_id": execution_id,
            "request_url": url,
            "final_url": payload.get("final_url") or url,
            "page_title": payload.get("page_title") or "",
            "page_body": body,
            "retrieved_at": payload.get("retrieved_at") or utc_now(),
            "body_format": "text/plain",
            "tool_name": "host_page_read",
            "access_status": "partial" if changed else "full",
        }
        checked = imported_page(
            {k: v for k, v in record.items() if k != "execution_id"},
            adapter_id="host_web_fetch",
        )
        if _CREDENTIAL.search(json.dumps(checked, ensure_ascii=False, sort_keys=True)):
            sanitized.append("STILL-DIRTY:" + url)
        records.append(record)

    args.out.write_text(
        json.dumps({"action_id": action_id, "records": records}, ensure_ascii=False, indent=1),
        encoding="utf8",
    )
    print(
        f"action_id={action_id} selectors={len(selectors)} records={len(records)} "
        f"failures={len(failures)} missing={len(missing)} sanitized={len(sanitized)}"
    )
    for url in missing:
        print("  MISSING:", url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
