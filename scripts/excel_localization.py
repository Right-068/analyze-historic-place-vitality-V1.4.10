#!/usr/bin/env python3
"""Translate user-facing workbook enums without changing machine ledgers."""

from __future__ import annotations


MAPS: dict[str, dict[str, str]] = {}


def register(fields: tuple[str, ...], values: dict[str, str]) -> None:
    for field in fields:
        MAPS[field] = values


register(
    (
        "是否明确到访/体验", "是否疑似推广", "是否有效样本", "是否纳入量化评分",
        "时间字段是否完整", "反讽或修辞风险", "正式评分候选资格",
        "是否实际进入平台评分", "是否相关", "是否用户来源", "是否触发登录",
        "是否触发访问限制", "explicit_visit", "suspected_promotion", "is_valid",
        "used_for_scoring", "time_complete", "figurative_risk", "formal_scoring_eligible",
        "included_in_platform_score", "is_relevant", "is_user_source", "login_triggered",
        "restriction_triggered",
    ),
    {"true": "是", "false": "否"},
)
register(
    ("情感倾向", "正式计分方向", "方向", "极性", "正负路径", "sentiment", "scoring_direction"),
    {"positive": "正向", "neutral": "中性", "negative": "负向", "mixed": "混合", "not_scored": "未计分", "na": "不适用"},
)
register(("语义语言", "语言", "semantic_language", "language"), {"zh": "中文", "en": "英文", "mixed": "中英混合", "unknown": "未知", "machine": "机器参数"})
register(
    ("来源类型", "目标来源类型", "source_category", "source_type"),
    {
        "official": "官方/公共机构", "government": "政府", "heritage": "遗产机构",
        "government_open_data": "政府开放数据", "heritage_registry": "遗产名录",
        "museum_venue": "博物馆与文化场馆",
        "academic": "学术研究", "professional": "专业资料", "news": "新闻媒体",
        "travel_ugc": "旅游用户生成内容", "map_review": "地图评价", "user_review": "用户评价",
        "social_content": "社交公开内容", "community_content": "社区公开内容",
        "local_forum": "地方论坛", "blog": "博客", "blog_travel": "博客与游记",
        "commercial_directory": "商业目录", "business_directory": "商业名录",
        "encyclopedia": "百科资料", "encyclopedic": "百科资料",
        "open_data": "开放数据", "mixed": "混合来源",
    },
)
register(
    ("读取状态", "access_status"),
    {"full": "完整读取", "partial": "部分读取", "snippet_only": "仅检索摘要", "metadata_only": "仅元数据", "inaccessible": "无法访问", "blocked": "访问受阻", "login_required": "需要登录", "captcha": "触发验证码", "no_results": "无结果"},
)
register(("实体层级", "entity_level"), {"area_direct": "地点直接证据", "area_aggregate": "区域聚合证据", "poi": "对象单体证据"})
register(("地点相关性", "place_relevance"), {"direct": "直接相关", "indirect": "间接相关", "high": "高", "medium": "中", "low": "低"})
register(("内容长度类别", "content_length_category"), {"short": "短文本", "medium": "中等文本", "long": "长文本"})
register(
    ("内容类型", "内容层级", "unit_type"),
    {"official_fact": "官方事实", "open_data_record": "开放数据记录", "heritage_record": "遗产档案", "news_report": "新闻报道", "academic_claim": "学术论述", "page_body": "页面正文", "user_post": "用户帖子", "user_review": "用户评价", "comment": "评论", "reply": "回复", "search_snippet": "检索摘要", "rating_only": "原生数值评价", "source_native_numeric": "来源原生数值"},
)
register(("评分范围", "score_scope"), {"direct": "地点直接评分范围", "not_scored": "不计分", "area_aggregate": "区域聚合范围", "aggregate_context": "聚合背景范围"})
register(
    ("选择机制", "selection_mechanism"),
    {"chronological": "按时间排序", "relevance_ranked": "按相关性排序", "platform_ai_selected": "平台算法筛选", "search_result": "检索结果", "direct_open": "直接打开", "citation_following": "引用追踪", "editorial": "编辑选择", "official": "官方选择", "user_provided": "用户提供", "unknown": "未知"},
)
register(("裁决状态", "adjudication_status"), {"human_confirmed": "人工确认", "confirmed": "已确认", "pending": "待裁决", "not_applicable": "不适用"})
register(("语义量化方法", "semantic_method"), {"source_native_numeric": "来源原生数值", "rule_codebook": "代码簿规则", "coding_parse_fallback": "编码解析候选", "assisted_semantic": "辅助语义编码", "manual_code": "人工编码"})
register(("语义复核状态", "semantic_review_status"), {"auto_eligible": "自动符合", "review_required": "需要复核", "human_confirmed": "人工确认", "not_applicable": "不适用"})
register(("平台样本状态", "platform_sample_status"), {"scored": "已计分", "not_eligible": "不符合计分资格", "insufficient_platform_samples": "平台样本不足", "not_scored": "未计分"})
register(("词表命中状态", "lexicon_match_status"), {"full_match": "维度与极性均命中", "dimension_only": "仅命中维度", "polarity_only": "仅命中极性", "zero_hit": "未命中", "not_applicable": "不适用"})
register(("编码解析状态", "coding_parse_status"), {"not_needed": "无需解析", "candidate_generated": "已生成候选", "started_unresolved": "已启动但未解决", "not_applicable": "不适用"})
register(("迭代模式", "iteration_mode"), {"baseline": "基础检索", "evidence_gap_fill": "证据缺口补检", "medium_enhancement": "中置信度增强"})
register(("记录类型", "record_type"), {"plan": "计划记录", "planned_query": "计划记录", "execution": "执行记录", "executed_query": "执行记录"})
register(("重试原因", "retry_reason"), {"transient_network_error": "瞬时网络错误", "temporary_service_error": "服务暂时异常", "rate_limit_released": "限流已解除", "host_tool_interruption": "宿主工具中断"})
register(("重试间隔策略", "retry_interval_policy"), {"host_managed_backoff": "宿主管理退避", "next_available_slot": "下一可用时隙", "manual_resume_after_transient_failure": "瞬时失败后人工恢复"})
register(("检索状态", "status", "termination_status"), {"completed": "已完成", "no_results": "无结果", "blocked": "访问受阻", "partial": "部分完成", "failed": "失败", "not_run": "尚未执行", "planned": "已规划", "queued": "已排队", "cancelled": "已取消", "skipped": "已跳过", "valid": "有效", "invalid": "无效", "target_met": "目标已达成", "exhausted": "经审计已穷尽", "query_budget_reached": "查询预算已达上限", "round_budget_reached": "轮次预算已达上限", "dimension_query_budgets_reached": "各维度查询预算已达上限", "source_blocked": "来源路径受阻", "evidence_shortfall": "证据仍不足", "retrieval_terminated_with_shortfall": "检索终止但证据仍不足", "unrecoverable_error": "不可恢复错误"})
register(("下一动作", "next_action"), {"target_met": "目标已达成", "continue": "继续检索", "exhausted": "进入穷尽审计", "review": "进入复核", "query_budget_reached": "查询预算已达上限", "round_budget_reached": "轮次预算已达上限", "dimension_query_budgets_reached": "各维度查询预算已达上限", "source_blocked": "来源路径受阻", "evidence_shortfall": "证据仍不足", "freeze_restricted_truth_then_score_eligible_dimensions": "冻结受限真值并仅评价合格维度", "unrecoverable_error": "不可恢复错误"})
register(("平台最低样本规则状态", "platform_minimum_rule_status"), {"all_platforms_scorable": "全部平台达到计分门槛", "partially_scorable": "部分平台达到计分门槛", "no_scorable_platform": "无平台达到计分门槛", "insufficient_platform_samples": "平台样本不足", "not_scored": "未形成正式评分"})
register(("提及等级",), {"A": "A级（很高）", "B": "B级（较高）", "C": "C级（中等）", "D": "D级（较低）", "E": "E级（很低）", "U": "U级（无法判定）"})
register(("发布校准模式",), {"online_updated": "发布前联网校准并更新", "online_verified_no_change": "发布前联网核验且无须变更"})
register(("动作",), {"retain": "保留", "modify": "修改", "add": "新增", "deprecate": "废止"})

EXCLUSION_REASON_MAP = {
    "not_marked_for_scoring": "未标记用于研究评分",
    "invalid_evidence": "证据无效",
    "not_direct_place_evidence": "非地点直接证据",
    "score_scope_not_direct": "评分范围非直接对象",
    "aggregate_area_evidence": "区域聚合证据",
    "unit_type_not_scorable": "证据单元类型不可计分",
    "invalid_primary_dimension": "主要维度无效",
    "missing_evidence_id": "缺少证据编号",
    "missing_dedup_key": "缺少正式去重键",
    "semantic_method_not_formally_scorable": "语义方法不具正式计分资格",
    "evidence_reliability_below_threshold": "证据可靠性低于门槛",
    "missing_or_invalid_scoring_value": "评分值缺失或无效",
    "incomplete_native_numeric_scale": "原生数值量表不完整",
    "missing_aggregation_weight": "缺少平台内聚合权重",
    "semantic_review_not_confirmed": "语义复核未确认",
    "semantic_confidence_below_threshold": "语义置信度低于门槛",
    "dimension_confidence_below_threshold": "维度置信度低于门槛",
    "aggregation_weight_mismatch": "平台内聚合权重不一致",
    "semantic_final_score_mismatch": "语义最终分不一致",
    "figurative_risk_not_human_confirmed": "修辞风险未经人工确认",
    "missing_semantic_unit_text": "缺少语义分析单元",
    "missing_semantic_rule_trace": "缺少语义规则轨迹",
    "missing_original_summary_text": "缺少摘要原文",
    "missing_excerpt_or_summary": "缺少必要摘录或摘要",
    "missing_linked_source": "缺少关联来源",
    "source_not_marked_for_scoring": "来源未标记用于研究评分",
    "source_not_relevant": "来源不相关",
    "source_not_user_evidence": "来源不是用户证据",
    "source_promotional_or_unknown": "来源推广状态不合格或未知",
    "source_not_readable": "来源正文不可读",
    "source_scope_not_direct": "来源评分范围非直接对象",
    "source_is_area_aggregate": "来源为区域聚合背景",
}


def display_value(field: str, value: object) -> object:
    if not isinstance(value, str):
        return value
    if field == "正式评分排除原因" and value.strip():
        return "；".join(
            EXCLUSION_REASON_MAP.get(item.strip(), item.strip())
            for item in value.split(";")
            if item.strip()
        )
    return MAPS.get(field, {}).get(value.strip(), value)


def machine_value(field: str, value: str) -> str:
    reverse = {display: machine for machine, display in MAPS.get(field, {}).items()}
    return reverse.get(value.strip(), value)


def normalize_matrix_for_validation(matrix: list[list[str]]) -> list[list[str]]:
    if not matrix:
        return matrix
    headers = matrix[0]
    return [
        list(headers),
        *[
            [machine_value(headers[index], value) if index < len(headers) else value for index, value in enumerate(row)]
            for row in matrix[1:]
        ],
    ]
