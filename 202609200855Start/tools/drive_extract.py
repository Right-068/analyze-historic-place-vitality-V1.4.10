"""In-process driver for EVIDENCE_EXTRACTION and SEMANTIC_CODING.

Extraction selects verbatim, unique, self-contained passages from each page's
retained visible body (the pipeline re-verifies uniqueness against the stored
page body). Coding assigns exactly one canonical dimension to each extracted
unit, in original unit order.

The driver only submits judgements through the released orchestrator path; the
semantic engine, admission, audit and scoring remain untouched.
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

MIN_UNIT = 30
MAX_UNITS_PER_PAGE = 8

# Canonical dimension order (framework section 1).
DIM_HISTORY = "历史文化感知"
DIM_LOCAL = "在地文化特征"
DIM_LIFE = "生活文化延续"
DIM_PRACTICE = "文化实践体验"
DIM_ATMOSPHERE = "场所氛围体验"
DIM_CONSERVATION = "保护活化感知"
DIM_GOVERNANCE = "承载治理体验"

# Ordered keyword cues; first match wins. Cues follow the dimension boundaries in
# seven-plus-one-framework.md (perception of heritage vs. locality vs. living use
# vs. practice vs. atmosphere vs. conservation vs. carrying capacity).
CUES: list[tuple[str, tuple[str, ...]]] = [
    (DIM_GOVERNANCE, (
        "拥挤", "人流", "客流", "排队", "通行", "噪声", "噪音", "垃圾", "卫生",
        "秩序", "安保", "安全", "分流", "高峰", "承载", "投诉", "管理", "治理",
        "商贩", "摊贩", "强卖", "占道", "停车", "疏导", "限流",
    )),
    (DIM_CONSERVATION, (
        "修缮", "修缮", "修复", "修旧", "保护", "活化", "改造", "更新", "翻新",
        "仿古", "原状", "原真", "历史建筑", "老建筑", "骑楼", "立面", "拆除",
        "保留", "格局", "再利用", "功能", "规划", "保护规划", "建设控制",
    )),
    (DIM_PRACTICE, (
        "庙会", "非遗", "市集", "节庆", "民俗", "技艺", "展演", "表演", "粤剧",
        "研学", "体验", "活动", "展览", "广彩", "广绣", "榄雕", "香云纱",
        "花市", "元宵", "传统仪式", "手作", "工作坊",
    )),
    (DIM_LIFE, (
        "街坊", "居民", "原住民", "老字号", "街坊邻里", "社区", "日常生活",
        "市井", "老广", "邻里", "迁离", "搬走", "祠堂", "书院", "店主", "商户",
        "老店", "传统业态", "生活空间", "世代", "居住",
    )),
    (DIM_ATMOSPHERE, (
        "氛围", "烟火气", "市井气息", "灯光", "夜色", "夜景", "热闹", "喧嚣",
        "怀旧", "感觉", "情绪", "气味", "香气", "声音", "霓虹", "拥挤感",
        "场所感", "地方感", "历史感",
    )),
    (DIM_LOCAL, (
        "千城一面", "千街一面", "同质化", "连锁", "网红", "地方特色", "本土",
        "岭南", "广府", "特色", "辨识度", "独特", "文化符号", "广货", "粤菜",
        "饮食", "语言", "粤语", "习俗", "本土文化", "商业化",
    )),
    (DIM_HISTORY, (
        "历史", "千年", "古道", "遗址", "文物", "南越", "拱北楼", "双门底",
        "城墙", "朝代", "唐宋", "明清", "记忆", "故事", "典故", "导览", "讲解",
        "标识", "介绍", "文化底蕴", "中轴线", "城脉", "文脉",
    )),
]


def assign_dimension(text: str) -> str:
    scores: dict[str, int] = {}
    for dimension, cues in CUES:
        hits = sum(text.count(cue) for cue in cues)
        if hits:
            scores[dimension] = hits
    if not scores:
        return DIM_HISTORY
    # Weight earlier dimensions slightly by tie-breaking on the ordered list.
    order = [name for name, _ in CUES]
    return max(scores.items(), key=lambda kv: (kv[1], -order.index(kv[0])))[0]


def candidate_units(clean_body: str) -> list[str]:
    """Self-contained visible passages, in original order."""
    lines = [ln.strip() for ln in clean_body.splitlines() if ln.strip()]
    units: list[str] = []
    buffer = ""
    for line in lines:
        if line == "Same retained text as" or line.startswith("Same retained text as"):
            continue
        # A quote on its own is a continuation of the previous context.
        joined = (buffer + line) if buffer and not buffer.endswith(("。", "！", "？", "!", "?", "”")) else line
        if len(joined) >= MIN_UNIT and joined[-1] in "。！？!?":
            units.append(joined)
            buffer = ""
        elif len(joined) >= 140:
            units.append(joined)
            buffer = ""
        else:
            buffer = joined
    if len(buffer) >= MIN_UNIT:
        units.append(buffer)
    return units


def select_excerpts(clean_body: str) -> list[str]:
    chosen: list[str] = []
    for unit in candidate_units(clean_body):
        if clean_body.count(unit) != 1:
            continue
        chosen.append(unit)
        if len(chosen) >= MAX_UNITS_PER_PAGE:
            break
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--stage", required=True, choices=("record-extraction-batch", "record-coding-batch"))
    parser.add_argument("--max-rounds", type=int, default=6)
    parser.add_argument("--max-units", type=int, default=0)
    args = parser.parse_args()

    w = Workflow(args.run)
    for round_no in range(1, args.max_rounds + 1):
        packet = w._execute_next_action()
        operation = packet.get("operation") or packet.get("action")
        print(
            f"round {round_no}: {packet.get('status')} {packet.get('continuation')} "
            f"op={operation} counts={packet.get('counts')}"
        )
        if operation != args.stage:
            print("stopped: current operation is", operation)
            return 0

        assigned = packet.get("items") or []
        if not assigned:
            print("stopped: nothing assigned")
            return 0

        records = []
        total_units = 0
        for item in assigned:
            page_id = item["page_id"]
            page = w.pipeline_page(page_id)
            if args.stage == "record-extraction-batch":
                excerpts = select_excerpts(page.get("clean_body", ""))
                decision = [{"text": text} for text in excerpts]
                total_units += len(decision)
            else:
                extraction = page.get("extraction") or []
                decision = [{"primary_dimension": assign_dimension(unit["text"])} for unit in extraction]
                total_units += len(decision)
            records.append(
                {
                    "page_id": page_id,
                    "analysis_fingerprint": item.get("analysis_fingerprint")
                    or page["analysis_fingerprint"],
                    "decision": decision,
                }
            )

        result = w.execute_next_action(
            {"action_id": packet.get("action_id"), "records": records}
        )
        print(
            f"round {round_no}: submitted {len(records)} records / {total_units} units -> "
            f"{result.get('status')} {result.get('continuation')} op={result.get('operation')} "
            f"counts={result.get('counts')}"
        )
        if result.get("continuation") not in ("AUTO_CONTINUE", "TURN_CHECKPOINT"):
            print("stopped on continuation", result.get("continuation"))
            return 0
    print("reached max rounds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
