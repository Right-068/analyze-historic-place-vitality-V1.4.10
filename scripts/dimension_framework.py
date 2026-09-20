#!/usr/bin/env python3
"""Canonical 7+1 qualitative framework for historic-district living heritage."""

from __future__ import annotations


WEIGHT_SCHEME_NAME = "初始研究权重"
DIMENSION_WEIGHT_FORMULA = "S = 0.15H + 0.15L + 0.20C + 0.15P + 0.10A + 0.15R + 0.10G"
EXPECTED_DIMENSION_WEIGHTS = {
    "历史文化感知": 0.15,
    "在地文化特征": 0.15,
    "生活文化延续": 0.20,
    "文化实践体验": 0.15,
    "场所氛围体验": 0.10,
    "保护活化感知": 0.15,
    "承载治理体验": 0.10,
}


DIMENSIONS = (
    {
        "dimension_id": "historical_cultural_perception",
        "name": "历史文化感知",
        "name_en": "Historical and cultural perception",
        "weight": 0.15,
        "definition": "历史文化感知是指公众对于街区历史价值、文化底蕴、历史事件、人物故事、遗产资源、城市记忆和文化叙事的识别、理解、记忆及情感反应。",
        "boundary": "关注是否知晓并理解文化意义；客观资源存在但未被公众感知，不能直接作为正向感知证据。",
    },
    {
        "dimension_id": "local_cultural_character",
        "name": "在地文化特征",
        "name_en": "Local cultural character",
        "weight": 0.15,
        "definition": "在地文化特征是指街区文化表达与本地历史背景、地域文化、生活方式和社会环境之间形成的独特联系，以及公众对于这种地方辨识度、文化真实性和不可替代性的感知。",
        "boundary": "关注文化是否属于当地并具有独特性；是否仍由真实主体持续实践归入生活文化延续。",
    },
    {
        "dimension_id": "living_cultural_continuity",
        "name": "生活文化延续",
        "name_en": "Living cultural continuity",
        "weight": 0.20,
        "definition": "生活文化延续是指居民、商户、传承人等真实主体是否仍持续存在于街区之中，以及与地方文化相关的生活方式、生产经营、社会交往、传统业态和社区记忆是否仍在当代持续发生。",
        "boundary": "一般的人多、热闹、消费旺不构成生活文化延续证据；必须识别真实主体、日常使用和持续性。",
    },
    {
        "dimension_id": "cultural_practice_experience",
        "name": "文化实践体验",
        "name_en": "Cultural practice experience",
        "weight": 0.15,
        "definition": "文化实践体验是指传统技艺、节庆民俗、非遗活动、文化演艺、传统仪式、文化教育、研学、市集及其他文化实践是否真实发生，以及公众是否真正参与并形成互动、理解和文化获得。",
        "boundary": "关注具体实践是否发生及能否参与；一次性展示、纯表演和仅有活动宣传不能自动视为活态传承。",
    },
    {
        "dimension_id": "place_atmosphere_experience",
        "name": "场所氛围体验",
        "name_en": "Place atmosphere experience",
        "weight": 0.10,
        "definition": "场所氛围体验是指公众在实际使用街区过程中，由历史环境、地方文化、社会活动、感官刺激和空间情境共同形成的整体场所感受。",
        "boundary": "仅描述街宽、密度、围合或建筑数量等客观形态时不纳入；只有其转化为公众场所感受时才编码。",
    },
    {
        "dimension_id": "conservation_and_adaptive_reuse_perception",
        "name": "保护活化感知",
        "name_en": "Conservation and adaptive reuse perception",
        "weight": 0.15,
        "definition": "保护活化感知是指公众对历史文化资源保护、修缮、适应性利用、功能导入及传统与现代关系处理效果所形成的评价。",
        "boundary": "游客增加、商业繁荣或店铺增多不能直接作为正向证据；必须同时考察真实性、持续使用和真实主体承载。",
    },
    {
        "dimension_id": "carrying_capacity_and_governance_experience",
        "name": "承载治理体验",
        "name_en": "Carrying capacity and governance experience",
        "weight": 0.10,
        "definition": "承载治理体验是指公众对高强度使用状态下产生的人流、环境、社会和空间压力，以及相应秩序维护、安全管理、客流组织、环境治理和利益协调效果的综合体验。",
        "boundary": "地铁数量、路网中心性和客观步行距离不作为核心证据；压力如何被体验及治理是否有效才纳入。",
    },
)

DIMENSION_NAMES = [item["name"] for item in DIMENSIONS]
DIMENSION_IDS = [item["dimension_id"] for item in DIMENSIONS]
DIMENSION_WEIGHTS = {item["name"]: item["weight"] for item in DIMENSIONS}
DIMENSION_DEFINITIONS = {item["name"]: item["definition"] for item in DIMENSIONS}
DIMENSION_BOUNDARIES = {item["name"]: item["boundary"] for item in DIMENSIONS}

RESULT_LAYER_NAME = "历史文化活态传承感知评价"
RESULT_LAYER_NAME_EN = "Perceived living transmission of historical culture"
RESULT_LAYER_REQUIRED_FIELDS = (
    "composite_score",
    "core_strengths",
    "core_risks",
    "bottom_line_issues",
    "evidence_sufficiency",
    "evaluation_confidence",
)

BOTTOM_LINE_DIMENSIONS = (
    "在地文化特征",
    "生活文化延续",
    "文化实践体验",
    "保护活化感知",
)

SUBJECT_TAGS = (
    "本地居民",
    "商户",
    "游客",
    "传承人或文化实践者",
    "管理者",
    "专业人士",
    "其他公众",
)

TIME_TAGS = (
    "当前状态",
    "历史回忆",
    "改造前后比较",
    "长期变化",
    "工作日",
    "周末",
    "节假日",
    "节庆活动期间",
    "白天",
    "夜间",
)

SENTIMENT_TAGS = ("正向", "负向", "中性", "混合")


def assert_framework() -> None:
    if len(DIMENSIONS) != 7:
        raise RuntimeError("the canonical framework must contain exactly seven analysis dimensions")
    if len(set(DIMENSION_NAMES)) != 7 or len(set(DIMENSION_IDS)) != 7:
        raise RuntimeError("dimension names and IDs must be unique")
    if abs(sum(DIMENSION_WEIGHTS.values()) - 1.0) > 1e-9:
        raise RuntimeError("dimension weights must sum to 1")
    if DIMENSION_WEIGHTS != EXPECTED_DIMENSION_WEIGHTS:
        raise RuntimeError("dimension weights must match the canonical initial research weights")
    if RESULT_LAYER_NAME in DIMENSION_WEIGHTS:
        raise RuntimeError("the 7+1 result layer must not receive a parallel analysis weight")


assert_framework()
