"""Build a TRIAGE envelope by classifying the actual visible page content.

Items are reconstructed exactly as the orchestrator's own batch packet does:
untriaged pages in the page-list order, with the same compact view. Decisions
classify the observed content and author, never the search-query target.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SKILL = Path(r"C:\Users\PC\.dsh\skills\analyze-historic-place-vitality")
sys.path.insert(0, str(SKILL / "scripts"))

from run_research import Workflow  # noqa: E402
from batch_pipeline import config as batch_config  # noqa: E402
from page_views import compact_view, bounded  # noqa: E402

OFFICIAL_HOSTS = (
    "gz.gov.cn", "yuexiu.gov.cn", "mzj.gz.gov.cn", "wglj.gz.gov.cn",
    "dfz.gd.gov.cn", "gd.gov.cn", "moj.gov.cn", "gzszfw.gov.cn",
    "sthjj.gz.gov.cn", "gdzwfw.gov.cn", "gzggzy.cn", "nanyueguyidao.cn",
    "tqyb.com.cn", "gzyxlib.cn", "gdql.org.cn",
)
NEWS_HOSTS = (
    "nfnews.com", "ycwb.com", "xkb.com.cn", "dayoo.com", "thepaper.cn",
    "sohu.com", "163.com", "qq.com", "sina.com.cn", "sina.cn", "oeeee.com",
    "toutiao.com", "jingjiribao.cn", "china.com.cn", "chinanews.com",
    "cnr.cn", "gmw.cn", "neweekly.com.cn", "zgcsb.com", "tidenews.com.cn",
    "cssn.cn", "gzass.gd.cn", "huacheng.gz-cmc.com", "nfncb.cn",
    "modaily.cn", "xiangyu-macau", "gz-cmc.com", "rednet.cn",
)
ACADEMIC_HOSTS = (
    "cnki.net", "wanfangdata.com.cn", "nature.com", "semanticscholar.org",
    "opaj.napstic.cn", "cnki.com.cn", "pishu.com.cn", "archive.org",
    "hkiud.org",
)
ENCYCLOPEDIC_HOSTS = ("baike.baidu.com", "wikisource.org", "protopal.jp")
USER_HOSTS = ("trip.com", "douyin.com", "bilibili.com", "meipian.cn",
              "gzmama.com", "fliphtml5.com", "zhihu.com", "xiaohongshu.com")

USER_LAYERS = {"user_post", "user_review", "comment", "reply"}
FIRST_PERSON = re.compile(
    r"(我(?:選定|走進|見到|喜歡|特別留意|認為|覺得|去|在)|讓我最|對我而言|简直|簡直)"
)
PROMO = re.compile(r"(直播間|直播间|優惠|优惠|下單|下单|領券|领券|廣告|广告|贊助|合作推廣|招募|報名|抽獎)")


def any_host(host: str, hosts) -> bool:
    return any(host == h or host.endswith("." + h) or h in host for h in hosts)


def classify(url: str, view: str) -> tuple[str, str, str, bool, str]:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if any_host(host, USER_HOSTS):
        return "travel_ugc", "user_post", "area_direct", True, "not_suspected"
    if any_host(host, ACADEMIC_HOSTS):
        return "academic", "page_body", "area_direct", True, "not_suspected"
    if any_host(host, ENCYCLOPEDIC_HOSTS):
        return "encyclopedic", "page_body", "area_direct", True, "not_suspected"
    if any_host(host, OFFICIAL_HOSTS):
        return "official", "official_fact", "area_direct", True, "not_suspected"
    # A clearly first-person individual account on a media host is user expression.
    if len(FIRST_PERSON.findall(view)) >= 3:
        return "travel_ugc", "user_post", "area_direct", True, "not_suspected"
    if any_host(host, NEWS_HOSTS):
        return "news", "page_body", "area_direct", True, "not_suspected"
    return "news", "page_body", "area_direct", True, "not_suspected"


def build_basis(view: str) -> dict | None:
    lines = [ln.strip() for ln in view.splitlines() if ln.strip()]
    author_line = ""
    for line in lines[:14]:
        if re.search(r"(分享的|發布|发布|作者|撰稿|文/|記者|记者)", line):
            author_line = line
            break
    if not author_line or author_line not in view:
        return None
    body_lines = [ln for ln in lines if len(ln) >= 40 and not ln.startswith(("📍", "🚇", "🕒", "#"))]
    if not body_lines:
        return None
    content = max(body_lines, key=len)
    if content not in view:
        return None
    return {
        "author_type": "individual",
        "basis_excerpt": author_line[:160],
        "user_content_excerpt": content[:600],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    w = Workflow(args.run)
    cfg = batch_config()
    state = w.pipeline_read()
    place = state.get("place") or w.state()["place"]
    items = []
    for page_id in state["pages"]:
        page = w.pipeline_page(page_id)
        if "triage" in page:
            continue
        view = compact_view(page["clean_body"], [place], cfg["page_view_chars"])
        item = {
            "page_id": page_id,
            "url": page["facts"]["final_url"],
            "title": page["facts"]["page_title"],
            "domain": page["domain"],
            "analysis_fingerprint": page["analysis_fingerprint"],
            "view": view["text"],
            "has_more": view["has_more"],
            "offset": view["offset"],
        }
        if page.get("duplicate_of"):
            item["duplicate_of"] = page["duplicate_of"]
            item["similarity_hint"] = page["similarity_hint"]
            if page["similarity_hint"] == 1.0:
                item["view"] = (
                    "Same retained text as " + page["duplicate_of"]
                    + ". Verify this source classification separately."
                )
        items.append(item)

    # Mirror the orchestrator's own visible-window bounding.
    visible = bounded(items, cfg["triage_batch_size"], cfg["max_agent_visible_chars"])

    records = []
    stats: dict[str, int] = {}
    for item in visible:
        view = item.get("view") or ""
        category, layer, entity, relevant, _promo = classify(item["url"], view)
        promo_status = "suspected" if PROMO.search(view) else "not_suspected"
        decision: dict = {
            "relevant": bool(relevant),
            "source_category": category,
            "content_layer": layer,
            "entity_level": entity,
            "promotion_status": promo_status,
        }
        if layer in USER_LAYERS:
            basis = build_basis(view)
            if basis is None:
                decision["content_layer"] = "page_body"
            else:
                decision["classification_basis"] = basis
        records.append(
            {
                "page_id": item["page_id"],
                "analysis_fingerprint": item["analysis_fingerprint"],
                "decision": decision,
            }
        )
        key = f"{decision['source_category']}/{decision['content_layer']}"
        stats[key] = stats.get(key, 0) + 1

    args.out.write_text(
        json.dumps({"action_id": args.action_id, "records": records}, ensure_ascii=False, indent=1),
        encoding="utf8",
    )
    print(f"action_id={args.action_id} pending={len(items)} issued={len(records)}")
    for key in sorted(stats):
        print(f"   {key}: {stats[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
