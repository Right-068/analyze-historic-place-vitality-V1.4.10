"""Local page fetcher + main-content extractor for the vitality research run.

Writes one sanitized text file per page plus a machine index. Used as the
`curl`/`requests`-class local_subprocess adapter: request URL, final URL,
HTTP status, retrieval timestamp, title and body are all retained facts.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

DROP_BLOCKS = (
    "script", "style", "noscript", "svg", "form", "iframe", "nav",
    "header", "footer", "aside", "template",
)
# Structural containers that are almost always navigation/chrome rather than content.
CHROME_HINTS = (
    "nav", "menu", "footer", "header", "sidebar", "breadcrumb", "copyright",
    "share", "comment-list", "related", "recommend", "advert", "banner",
)

_WS = re.compile(r"[ \t\u00a0\u3000]+")


def strip_blocks(raw: str) -> str:
    for tag in DROP_BLOCKS:
        raw = re.sub(
            rf"<{tag}\b[^>]*>.*?</{tag}>", " ", raw, flags=re.I | re.S
        )
    return raw


def drop_chrome(raw: str) -> str:
    for hint in CHROME_HINTS:
        raw = re.sub(
            rf'<div[^>]*(?:class|id)="[^"]*{hint}[^"]*"[^>]*>.*?</div>',
            " ",
            raw,
            flags=re.I | re.S,
        )
    return raw


def extract_title(raw: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.I | re.S)
    if m:
        return html.unescape(_WS.sub(" ", m.group(1))).strip()
    m = re.search(r'<h1[^>]*>(.*?)</h1>', raw, flags=re.I | re.S)
    if m:
        return html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).strip()
    return ""


def to_text(raw: str) -> str:
    body = strip_blocks(raw)
    body = drop_chrome(body)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    body = re.sub(r"</(?:p|div|li|tr|h[1-6]|section|article)>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html.unescape(body)
    lines = []
    seen = set()
    for line in body.splitlines():
        line = _WS.sub(" ", line).strip()
        if len(line) < 12:
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


def fetch(url: str, timeout: int = 30) -> dict:
    started = time.time()
    request_url = url
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_bytes = response.read()
            final_url = response.geturl()
            status = response.status
            ctype = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return {
            "request_url": request_url,
            "final_url": getattr(exc, "url", url),
            "access_status": "failed",
            "error_type": f"http_{exc.code}",
            "elapsed_s": round(time.time() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001 - recorded as a real fetch failure
        return {
            "request_url": request_url,
            "final_url": url,
            "access_status": "failed",
            "error_type": type(exc).__name__,
            "error_detail": str(exc)[:200],
            "elapsed_s": round(time.time() - started, 2),
        }

    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype, flags=re.I)
    if m:
        charset = m.group(1)
    try:
        raw = raw_bytes.decode(charset, errors="replace")
    except LookupError:
        raw = raw_bytes.decode("utf-8", errors="replace")

    title = extract_title(raw)
    body = to_text(raw)
    return {
        "request_url": request_url,
        "final_url": final_url,
        "access_status": "full" if body else "partial",
        "http_status": status,
        "content_type": ctype,
        "page_title": title,
        "body_format": "text/html",
        "tool_name": "host_page_read",
        "actual_fetch_method": "urllib_browser_headers",
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "content_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "body_chars": len(body),
        "page_body": body,
        "elapsed_s": round(time.time() - started, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls", type=Path, required=True, help="JSON list of URLs")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--index-name", default="_index.json")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    urls = json.loads(args.urls.read_text(encoding="utf-8-sig"))
    if args.limit:
        urls = urls[: args.limit]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for i, url in enumerate(urls, 1):
        record = fetch(url, timeout=args.timeout)
        digest = hashlib.sha256(url.encode("utf8")).hexdigest()[:16]
        out = args.out_dir / f"p{i:03d}_{digest}.json"
        out.write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf8"
        )
        index.append(
            {
                "n": i,
                "request_url": url,
                "access_status": record.get("access_status"),
                "error_type": record.get("error_type"),
                "body_chars": record.get("body_chars", 0),
                "file": out.name,
            }
        )
        print(
            f"[{i}/{len(urls)}] {record.get('access_status')} "
            f"{record.get('body_chars', 0)}c {url[:90]}",
            flush=True,
        )
        time.sleep(args.delay)
    args.index.write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf8"
    )
    ok = sum(1 for r in index if r["access_status"] in ("full", "partial"))
    print(f"DONE ok={ok} fail={len(index) - ok} of {len(index)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
