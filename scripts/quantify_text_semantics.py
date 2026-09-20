#!/usr/bin/env python3
"""Quantify non-numeric evidence text with an auditable, vendor-neutral codebook."""

from __future__ import annotations

import argparse
import csv
import hashlib
import strict_json as json
import math
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from dimension_evidence import validate_machine_dimension_audit
from dimension_framework import DIMENSION_NAMES
from formal_scoring import (
    FORMAL_SCORING_FIELDS,
    annotate_rows,
    build_formal_scoring_chain,
    load_protocol as load_formal_protocol,
    trusted_human_review,
)
from artifact_provenance import (
    register_protected_artifact,
    verify_artifact_writer,
    verify_artifact_writer_any,
)
from runtime_guard import authorize_runtime_write, seal_scoring_input


DEFAULT_CODEBOOK = Path(__file__).resolve().parents[1] / "assets" / "semantic-quantification-codebook.json"
DEFAULT_PROTOCOL = Path(__file__).resolve().parents[1] / "assets" / "evaluation-protocol.json"

SEMANTIC_FIELDS = [
    "review_trust_proof",
    "native_rating_value",
    "native_rating_scale_min",
    "native_rating_scale_max",
    "native_rating_normalized",
    "semantic_method",
    "semantic_language",
    "semantic_unit_text",
    "semantic_clause_count",
    "lexicon_match_status",
    "lexicon_match_trace",
    "coding_parse_status",
    "coding_parse_candidates",
    "coding_parse_trace",
    "open_code_id",
    "aspect_assignment_basis",
    "aspect_confidence",
    "dimension_rule_hits",
    "polarity_basis",
    "negation_hits",
    "degree_hits",
    "contrast_hits",
    "hedge_hits",
    "figurative_risk",
    "semantic_score_raw",
    "semantic_score_final",
    "semantic_confidence",
    "evidence_reliability",
    "aggregation_weight",
    "semantic_review_status",
    "semantic_rule_trace",
    "coder_id",
    "adjudication_status",
    "reviewer_id",
    "review_origin",
    "review_record_id",
    "review_decision_time",
    "review_provenance_sha256",
    "review_provenance_valid",
]

SENTIMENTS = {"positive", "neutral", "negative", "mixed", "na"}
REVIEW_ACCEPTED = {"auto_eligible", "human_confirmed"}
SCORABLE_USER_UNITS = {"page_body", "user_post", "user_review", "comment", "reply"}
TRUE_VALUES = {"true", "1", "yes", "y", "是"}
FALSE_VALUES = {"false", "0", "no", "n", "否"}


def parse_bool(value: object) -> bool | None:
    text = str(value or "").strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def rounded(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value + 1e-12, digits)


def round_half_away_from_zero(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        rows = [
            {key: (value or "").strip() for key, value in row.items()}
            for row in reader
        ]
        return list(reader.fieldnames), rows


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def load_codebook(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("semantic codebook root must be an object")
    required = {
        "score_scale",
        "thresholds",
        "scope",
        "languages",
        "lexicon_schema",
        "positive_lexicon",
        "negative_lexicon",
        "polarity_concepts",
        "dimensions",
        "confidence_components",
        "release_protocol",
    }
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"semantic codebook is missing sections: {', '.join(missing)}")
    release = data.get("release_protocol")
    if not isinstance(release, dict):
        raise ValueError("semantic codebook must contain a locked release_protocol")
    for field in ("evaluation_protocol_version", "semantic_codebook_version", "released_at", "manifest_sha256"):
        if not str(release.get(field, "")).strip():
            raise ValueError(f"semantic codebook release_protocol requires {field}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(release.get("released_at", ""))):
        raise ValueError("semantic codebook release date must use YYYY-MM-DD")
    if not re.fullmatch(r"[0-9a-f]{64}", str(release.get("manifest_sha256", ""))):
        raise ValueError("semantic codebook release manifest checksum is invalid")
    if release.get("task_level_rule_mutation_allowed") is not False:
        raise ValueError("semantic codebook must prohibit task-level rule mutation")
    if str(release.get("task_rule_update_policy", "")) != "locked_at_skill_release":
        raise ValueError("semantic codebook must be locked at Skill release")
    dimensions = data.get("dimensions")
    if not isinstance(dimensions, list) or len(dimensions) != len(DIMENSION_NAMES):
        raise ValueError("semantic codebook must define exactly seven analysis dimensions")
    dimension_names = [str(item.get("name", "")) for item in dimensions if isinstance(item, dict)]
    if dimension_names != DIMENSION_NAMES:
        raise ValueError("semantic codebook dimension names and order must match the canonical seven-dimension framework")
    if any(not isinstance(item.get("cues"), list) or not item["cues"] for item in dimensions if isinstance(item, dict)):
        raise ValueError("every semantic codebook dimension must define at least one cue")
    dimension_ids = [str(item.get("dimension_id", "")) for item in dimensions if isinstance(item, dict)]
    if len(dimension_ids) != len(DIMENSION_NAMES) or any(not value for value in dimension_ids) or len(set(dimension_ids)) != len(DIMENSION_NAMES):
        raise ValueError("semantic codebook dimension IDs must be non-empty and unique")
    for item in dimensions:
        if not isinstance(item, dict):
            raise ValueError("every semantic codebook dimension must be an object")
        if not str(item.get("name_en", "")).strip():
            raise ValueError("every semantic codebook dimension must define an English name")
        for key in ("cues_by_language", "near_synonyms", "gloss"):
            multilingual = item.get(key)
            if not isinstance(multilingual, dict):
                raise ValueError(f"every semantic codebook dimension must define {key}")
            if any(not multilingual.get(language) for language in ("zh", "en")):
                raise ValueError(f"every semantic codebook dimension {key} must include zh and en")

    languages = data.get("languages")
    if not isinstance(languages, list) or not {"zh", "en"}.issubset({str(item) for item in languages}):
        raise ValueError("semantic codebook languages must include zh and en")
    schema = data.get("lexicon_schema")
    if not isinstance(schema, dict) or not isinstance(schema.get("surface_fields"), list):
        raise ValueError("semantic codebook must define a machine-readable lexicon schema")

    scale = data["score_scale"]
    thresholds = data["thresholds"]
    components = data["confidence_components"]
    if not isinstance(scale, dict) or not isinstance(thresholds, dict) or not isinstance(components, dict):
        raise ValueError("semantic codebook scale, thresholds, and confidence components must be objects")
    try:
        if float(scale["minimum"]) >= float(scale["maximum"]) or float(scale["normalization_alpha"]) <= 0:
            raise ValueError
        for key in (
            "dimension_auto_confidence",
            "semantic_auto_confidence",
            "minimum_evidence_reliability",
        ):
            value = float(thresholds[key])
            if not 0 <= value <= 1:
                raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("semantic codebook scale or confidence thresholds are invalid") from exc

    for component_name in ("semantic", "evidence_reliability"):
        weights = components.get(component_name)
        if not isinstance(weights, dict) or not weights:
            raise ValueError(f"semantic codebook confidence component {component_name!r} is missing")
        try:
            total = sum(float(value) for value in weights.values())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"semantic codebook confidence component {component_name!r} has non-numeric weights") from exc
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError(f"semantic codebook confidence component {component_name!r} weights must sum to 1")

    positive = data["positive_lexicon"]
    negative = data["negative_lexicon"]
    if not isinstance(positive, dict) or not isinstance(negative, dict) or not positive or not negative:
        raise ValueError("semantic codebook polarity lexicons must be non-empty objects")
    overlap = sorted(set(positive) & set(negative))
    if overlap:
        raise ValueError(f"semantic codebook polarity lexicons overlap: {', '.join(overlap)}")
    try:
        if any(float(value) <= 0 for value in [*positive.values(), *negative.values()]):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("semantic codebook polarity weights must be positive numbers") from exc

    concepts = data.get("polarity_concepts")
    if not isinstance(concepts, list) or not concepts:
        raise ValueError("semantic codebook polarity_concepts must be a non-empty list")
    concept_ids: set[str] = set()
    compiled_positive = {str(term): float(weight) for term, weight in positive.items()}
    compiled_negative = {str(term): float(weight) for term, weight in negative.items()}
    for index, concept in enumerate(concepts, start=1):
        if not isinstance(concept, dict):
            raise ValueError(f"polarity concept {index} must be an object")
        concept_id = str(concept.get("concept_id", "")).strip()
        polarity = str(concept.get("polarity", "")).strip()
        if not concept_id or concept_id in concept_ids:
            raise ValueError(f"polarity concept {index} requires a unique concept_id")
        if polarity not in {"positive", "negative"}:
            raise ValueError(f"polarity concept {concept_id} requires positive or negative polarity")
        try:
            weight = float(concept.get("weight", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"polarity concept {concept_id} has invalid weight") from exc
        if weight <= 0:
            raise ValueError(f"polarity concept {concept_id} weight must be positive")
        flattened: list[str] = []
        for key in ("terms", "near_synonyms"):
            multilingual = concept.get(key)
            if not isinstance(multilingual, dict):
                raise ValueError(f"polarity concept {concept_id} requires {key}")
            for language in ("zh", "en"):
                terms = multilingual.get(language)
                if not isinstance(terms, list) or not terms:
                    raise ValueError(f"polarity concept {concept_id} {key} must include {language}")
                flattened.extend(str(term).strip() for term in terms if str(term).strip())
        gloss = concept.get("gloss")
        if not isinstance(gloss, dict) or any(not str(gloss.get(language, "")).strip() for language in ("zh", "en")):
            raise ValueError(f"polarity concept {concept_id} gloss must include zh and en")
        target = compiled_positive if polarity == "positive" else compiled_negative
        other = compiled_negative if polarity == "positive" else compiled_positive
        for term in flattened:
            if term in other:
                raise ValueError(f"semantic codebook polarity lexicons overlap after concept expansion: {term}")
            target[term] = max(weight, float(target.get(term, 0)))
        concept_ids.add(concept_id)

    for dimension in dimensions:
        assert isinstance(dimension, dict)
        compiled_cues: list[str] = [str(item) for item in dimension.get("cues", [])]
        for key in ("cues_by_language", "near_synonyms"):
            multilingual = dimension.get(key)
            assert isinstance(multilingual, dict)
            for language in ("zh", "en"):
                values = multilingual.get(language, [])
                assert isinstance(values, list)
                compiled_cues.extend(str(value) for value in values if str(value))
        dimension["compiled_cues"] = sorted(set(compiled_cues), key=lambda value: (-len(value), value))
    data["positive_lexicon"] = compiled_positive
    data["negative_lexicon"] = compiled_negative
    return data


def term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    if re.search(r"[A-Za-z0-9]", term):
        if re.match(r"[A-Za-z0-9]", term):
            escaped = r"(?<![A-Za-z0-9])" + escaped
        if re.search(r"[A-Za-z0-9]$", term):
            escaped += r"(?![A-Za-z0-9])"
    return re.compile(escaped, flags=re.IGNORECASE)


def count_term(text: str, term: str) -> int:
    if not term:
        return 0
    return len(list(term_pattern(term).finditer(text)))


def all_term_hits(text: str, terms: Iterable[str]) -> list[str]:
    hits: list[str] = []
    for term in sorted({str(item) for item in terms if str(item)}, key=len, reverse=True):
        hits.extend([term] * count_term(text, term))
    return hits


def non_overlapping_term_hits(text: str, terms: Iterable[str]) -> list[str]:
    """Return longest, non-overlapping term occurrences in text order."""
    candidates: list[tuple[int, int, str]] = []
    for term in sorted({str(item) for item in terms if str(item)}, key=len, reverse=True):
        for match in term_pattern(term).finditer(text):
            candidates.append((match.start(), match.end(), term))
    candidates.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2]))
    occupied: set[int] = set()
    selected: list[tuple[int, str]] = []
    for start, end, term in candidates:
        positions = set(range(start, end))
        if positions & occupied:
            continue
        occupied.update(positions)
        selected.append((start, term))
    return [term for _, term in sorted(selected)]


def non_overlapping_polarity_hits(
    text: str,
    positive: dict[str, object],
    negative: dict[str, object],
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for polarity, lexicon in (("positive", positive), ("negative", negative)):
        for raw_term, raw_weight in lexicon.items():
            term = str(raw_term)
            weight = float(raw_weight)
            for match in term_pattern(term).finditer(text):
                candidates.append(
                    {
                        "term": term,
                        "start": match.start(),
                        "end": match.end(),
                        "polarity": polarity,
                        "weight": weight,
                    }
                )
    candidates.sort(
        key=lambda item: (
            -(int(item["end"]) - int(item["start"])),
            int(item["start"]),
            -float(item["weight"]),
        )
    )
    occupied: set[int] = set()
    selected: list[dict[str, object]] = []
    for item in candidates:
        positions = set(range(int(item["start"]), int(item["end"])))
        if positions & occupied:
            continue
        occupied.update(positions)
        selected.append(item)
    return sorted(selected, key=lambda item: int(item["start"]))


def closest_modifier(prefix: str, degree_modifiers: dict[str, object]) -> tuple[float, str]:
    best_position = -1
    best_multiplier = 1.0
    best_term = ""
    for raw_multiplier, raw_terms in degree_modifiers.items():
        multiplier = float(raw_multiplier)
        if not isinstance(raw_terms, list):
            continue
        for raw_term in raw_terms:
            term = str(raw_term)
            matches = list(term_pattern(term).finditer(prefix))
            if not matches:
                continue
            position = matches[-1].start()
            if position > best_position or (position == best_position and len(term) > len(best_term)):
                best_position = position
                best_multiplier = multiplier
                best_term = term
    return best_multiplier, best_term


def bounded_prefix(text: str, start: int, window: int) -> str:
    """Return modifier scope without crossing a local punctuation boundary."""
    prefix = text[max(0, start - window) : start]
    return re.split(r"[，,、：:（）()\[\]【】]", prefix)[-1]


def sentence_clauses(text: str, contrast_markers: list[str]) -> list[dict[str, object]]:
    sentences = [part.strip() for part in re.split(r"[。！？!?；;\n]+", text) if part.strip()]
    clauses: list[dict[str, object]] = []
    markers = sorted({item for item in contrast_markers if item}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(item) for item in markers), flags=re.IGNORECASE) if markers else None
    for sentence in sentences:
        matches = list(pattern.finditer(sentence)) if pattern else []
        if not matches:
            clauses.append({"text": sentence, "contrast_weight": 1.0, "marker": ""})
            continue
        first = matches[0]
        before = sentence[: first.start()].strip("，,、 ")
        if before:
            clauses.append(
                {
                    "text": before,
                    "contrast_weight": "before",
                    "marker": "",
                }
            )
        for match_index, match in enumerate(matches):
            next_start = matches[match_index + 1].start() if match_index + 1 < len(matches) else len(sentence)
            after = sentence[match.end() : next_start].strip("，,、 ")
            if not after:
                continue
            clauses.append(
                {
                    "text": after,
                    "contrast_weight": "after",
                    "marker": match.group(0),
                }
            )
    return clauses


def aspect_assignment(text: str, existing: str, codebook: dict[str, object]) -> dict[str, object]:
    dimensions = codebook["dimensions"]
    assert isinstance(dimensions, list)
    hit_map: dict[str, dict[str, object]] = {}
    for raw_dimension in dimensions:
        if not isinstance(raw_dimension, dict):
            continue
        name = str(raw_dimension.get("name", ""))
        cues = raw_dimension.get("compiled_cues", raw_dimension.get("cues", []))
        if not name or not isinstance(cues, list):
            continue
        cue_hits = Counter(all_term_hits(text, [str(item) for item in cues]))
        hit_map[name] = {"count": sum(cue_hits.values()), "cues": dict(cue_hits)}

    ranked = sorted(
        hit_map,
        key=lambda name: (int(hit_map[name]["count"]), name),
        reverse=True,
    )
    thresholds = codebook.get("thresholds", {})
    minimum_hits = int(thresholds.get("minimum_dimension_hits", 1)) if isinstance(thresholds, dict) else 1
    top = ranked[0] if ranked and int(hit_map[ranked[0]]["count"]) >= minimum_hits else ""
    top_count = int(hit_map[top]["count"]) if top else 0
    second_count = int(hit_map[ranked[1]]["count"]) if len(ranked) > 1 else 0
    total_hits = sum(int(item["count"]) for item in hit_map.values())
    dimension_names = set(hit_map)

    if existing and existing not in dimension_names:
        basis = "invalid_declared_dimension"
        proposed = top
        confidence = 0.0
    elif existing and top and existing == top:
        basis = "declared+lexical_agreement"
        proposed = existing
        dominance = top_count / max(1, total_hits)
        margin = (top_count - second_count) / max(1, top_count)
        confidence = clamp(0.65 + 0.2 * dominance + 0.15 * margin)
    elif existing and top and existing != top:
        basis = "declared_lexical_conflict"
        proposed = existing
        confidence = 0.4
    elif existing:
        basis = "declared_only"
        proposed = existing
        confidence = 0.65
    elif top:
        basis = "lexical_candidate"
        proposed = top
        dominance = top_count / max(1, total_hits)
        margin = (top_count - second_count) / max(1, top_count)
        confidence = clamp(0.45 + 0.35 * dominance + 0.2 * margin)
    else:
        basis = "unresolved"
        proposed = ""
        confidence = 0.0

    compact_hits = {
        name: details
        for name, details in hit_map.items()
        if int(details["count"]) > 0
    }
    return {
        "primary_dimension": proposed,
        "basis": basis,
        "confidence": rounded(confidence),
        "hits": compact_hits,
    }


ENGLISH_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for", "from",
    "had", "has", "have", "he", "her", "here", "his", "i", "in", "is", "it", "its",
    "me", "my", "of", "on", "or", "our", "she", "so", "that", "the", "their", "them",
    "there", "they", "this", "to", "very", "was", "we", "were", "with", "you", "your",
}
CHINESE_STOP_TOKENS = {
    "一个", "一些", "这里", "那里", "这个", "那个", "可以", "感觉", "觉得", "比较",
    "非常", "以及", "还是", "就是", "因为", "所以", "但是", "不过", "体验", "地方",
}


def detect_language(text: str) -> str:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk and latin:
        total = cjk + latin
        return "mixed" if min(cjk, latin) / total >= 0.12 else "zh" if cjk > latin else "en"
    if cjk:
        return "zh"
    if latin:
        return "en"
    return "unknown"


def normalize_english_token(token: str) -> str:
    value = token.casefold().strip("'-")
    if len(value) > 5 and value.endswith("ing"):
        value = value[:-3]
    elif len(value) > 4 and value.endswith("ied"):
        value = value[:-3] + "y"
    elif len(value) > 4 and value.endswith("ed"):
        value = value[:-2]
    elif len(value) > 4 and value.endswith("es"):
        value = value[:-2]
    elif len(value) > 3 and value.endswith("s"):
        value = value[:-1]
    return value


def semantic_tokens(text: str, language: str) -> set[str]:
    tokens: set[str] = set()
    if language in {"en", "mixed", "unknown"}:
        for raw in re.findall(r"[A-Za-z][A-Za-z'-]+", text):
            token = normalize_english_token(raw)
            if len(token) >= 2 and token not in ENGLISH_STOPWORDS:
                tokens.add(f"en:{token}")
    if language in {"zh", "mixed", "unknown"}:
        for segment in re.findall(r"[\u3400-\u9fff]+", text):
            if len(segment) <= 8 and segment not in CHINESE_STOP_TOKENS:
                tokens.add(f"zh:{segment}")
            for size in (2, 3):
                for index in range(max(0, len(segment) - size + 1)):
                    token = segment[index : index + size]
                    if token not in CHINESE_STOP_TOKENS:
                        tokens.add(f"zh:{token}")
    return tokens


def multilingual_values(container: object, languages: list[str]) -> list[str]:
    if not isinstance(container, dict):
        return []
    values: list[str] = []
    for language in languages:
        raw = container.get(language)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if str(item).strip())
        elif isinstance(raw, str) and raw.strip():
            values.append(raw)
    return values


def similarity_score(query_tokens: set[str], profile_tokens: set[str]) -> tuple[float, list[str]]:
    if not query_tokens or not profile_tokens:
        return 0.0, []
    overlap = query_tokens & profile_tokens
    if not overlap:
        return 0.0, []
    overlap_coefficient = len(overlap) / min(len(query_tokens), len(profile_tokens))
    jaccard = len(overlap) / len(query_tokens | profile_tokens)
    score = 0.7 * overlap_coefficient + 0.3 * jaccard
    return clamp(score), sorted(overlap)[:12]


def coding_parse(
    text: str,
    language: str,
    lexicon_status: str,
    codebook: dict[str, object],
) -> dict[str, object]:
    if lexicon_status == "full_match":
        return {
            "status": "not_needed",
            "candidates": [],
            "trace": {"trigger": "none", "language": language},
            "open_code_id": "",
        }
    languages = ["zh", "en"] if language in {"mixed", "unknown"} else [language]
    query_tokens = semantic_tokens(text, language)
    thresholds = codebook.get("thresholds", {})
    assert isinstance(thresholds, dict)
    minimum = float(thresholds.get("coding_parse_candidate_similarity", 0.12))
    strong = float(thresholds.get("coding_parse_strong_similarity", 0.28))
    maximum = int(thresholds.get("coding_parse_max_candidates_per_type", 3))
    candidates: list[dict[str, object]] = []

    need_dimension = lexicon_status in {"polarity_only", "zero_hit"}
    need_polarity = lexicon_status in {"dimension_only", "zero_hit"}
    if need_dimension:
        ranked_dimensions: list[dict[str, object]] = []
        dimensions = codebook.get("dimensions", [])
        assert isinstance(dimensions, list)
        for raw_dimension in dimensions:
            if not isinstance(raw_dimension, dict):
                continue
            profile_values = [
                str(raw_dimension.get("name", "")),
                str(raw_dimension.get("name_en", "")),
                *multilingual_values(raw_dimension.get("cues_by_language"), languages),
                *multilingual_values(raw_dimension.get("near_synonyms"), languages),
                *multilingual_values(raw_dimension.get("gloss"), languages),
            ]
            profile_tokens = semantic_tokens(" ".join(profile_values), "mixed" if len(languages) > 1 else languages[0])
            score, matched = similarity_score(query_tokens, profile_tokens)
            if score >= minimum:
                ranked_dimensions.append(
                    {
                        "candidate_type": "dimension",
                        "concept_id": str(raw_dimension.get("dimension_id", "")),
                        "label_zh": str(raw_dimension.get("name", "")),
                        "label_en": str(raw_dimension.get("name_en", "")),
                        "similarity": rounded(score),
                        "strength": "strong" if score >= strong else "weak",
                        "matched_profile_tokens": matched,
                        "basis": "bilingual_synonym_gloss_profile",
                    }
                )
        candidates.extend(
            sorted(ranked_dimensions, key=lambda item: (-float(item["similarity"]), str(item["concept_id"])))[:maximum]
        )

    if need_polarity:
        ranked_polarity: list[dict[str, object]] = []
        concepts = codebook.get("polarity_concepts", [])
        assert isinstance(concepts, list)
        for raw_concept in concepts:
            if not isinstance(raw_concept, dict):
                continue
            profile_values = [
                *multilingual_values(raw_concept.get("terms"), languages),
                *multilingual_values(raw_concept.get("near_synonyms"), languages),
                *multilingual_values(raw_concept.get("gloss"), languages),
            ]
            profile_tokens = semantic_tokens(" ".join(profile_values), "mixed" if len(languages) > 1 else languages[0])
            score, matched = similarity_score(query_tokens, profile_tokens)
            if score >= minimum:
                ranked_polarity.append(
                    {
                        "candidate_type": "polarity",
                        "concept_id": str(raw_concept.get("concept_id", "")),
                        "polarity": str(raw_concept.get("polarity", "")),
                        "weight": float(raw_concept.get("weight", 0)),
                        "similarity": rounded(score),
                        "strength": "strong" if score >= strong else "weak",
                        "matched_profile_tokens": matched,
                        "basis": "bilingual_synonym_gloss_profile",
                    }
                )
        candidates.extend(
            sorted(ranked_polarity, key=lambda item: (-float(item["similarity"]), str(item["concept_id"])))[:maximum]
        )

    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    open_code_id = "OPEN-" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    status = "candidate_generated" if candidates else "started_unresolved"
    return {
        "status": status,
        "candidates": candidates,
        "open_code_id": open_code_id,
        "trace": {
            "trigger": lexicon_status,
            "language": language,
            "query_tokens": sorted(query_tokens)[:60],
            "candidate_similarity_threshold": minimum,
            "strong_similarity_threshold": strong,
            "candidate_count": len(candidates),
            "scoring_policy": "review_required_no_automatic_score",
            "next_action": "schema_constrained_assisted_or_human_coding",
        },
    }


def sentiment_analysis(text: str, codebook: dict[str, object]) -> dict[str, object]:
    scale = codebook["score_scale"]
    scope = codebook["scope"]
    assert isinstance(scale, dict) and isinstance(scope, dict)
    positive = codebook["positive_lexicon"]
    negative = codebook["negative_lexicon"]
    degree_modifiers = codebook.get("degree_modifiers", {})
    negators = [str(item) for item in codebook.get("negators", [])]
    hedges = [str(item) for item in codebook.get("hedges", [])]
    contrast_markers = [str(item) for item in codebook.get("contrast_markers", [])]
    neutral_cues = [str(item) for item in codebook.get("neutral_cues", [])]
    figurative_cues = [str(item) for item in codebook.get("sarcasm_or_figurative_cues", [])]
    assert isinstance(positive, dict) and isinstance(negative, dict)
    assert isinstance(degree_modifiers, dict)

    clauses = sentence_clauses(text, contrast_markers)
    trace: list[dict[str, object]] = []
    total = 0.0
    positive_contribution = 0.0
    negative_contribution = 0.0
    negation_hits: list[str] = []
    degree_hits: list[str] = []
    hedge_hits: list[str] = []
    contrast_hits: list[str] = []

    for clause in clauses:
        clause_text = str(clause["text"])
        contrast_label = str(clause["contrast_weight"])
        if contrast_label == "before":
            contrast_weight = float(scope["contrast_before_weight"])
        elif contrast_label == "after":
            contrast_weight = float(scope["contrast_after_weight"])
        else:
            contrast_weight = 1.0
        marker = str(clause.get("marker", ""))
        if marker:
            contrast_hits.append(marker)
        punctuation_count = min(3, len(re.findall(r"[!！]", clause_text)))
        punctuation_weight = min(
            float(scope["punctuation_amplification_cap"]),
            1.0 + punctuation_count * float(scope["punctuation_amplification_per_mark"]),
        )
        polarity_hits = non_overlapping_polarity_hits(clause_text, positive, negative)
        for hit in polarity_hits:
            start = int(hit["start"])
            sign = 1.0 if hit["polarity"] == "positive" else -1.0
            negation_window = int(scope["negation_window_characters"])
            degree_window = int(scope["degree_window_characters"])
            hedge_window = int(scope["hedge_window_characters"])
            negation_prefix = bounded_prefix(clause_text, start, negation_window)
            degree_prefix = bounded_prefix(clause_text, start, degree_window)
            hedge_prefix = bounded_prefix(clause_text, start, hedge_window)
            local_negators = non_overlapping_term_hits(negation_prefix, negators)
            negation_multiplier = -1.0 if len(local_negators) % 2 else 1.0
            degree_multiplier, degree_term = closest_modifier(degree_prefix, degree_modifiers)
            local_hedges = all_term_hits(hedge_prefix, hedges)
            hedge_weight = float(scope["hedge_weight"]) if local_hedges else 1.0
            contribution = (
                sign
                * float(hit["weight"])
                * negation_multiplier
                * degree_multiplier
                * contrast_weight
                * hedge_weight
                * punctuation_weight
            )
            total += contribution
            if contribution >= 0:
                positive_contribution += abs(contribution)
            else:
                negative_contribution += abs(contribution)
            negation_hits.extend(local_negators)
            if degree_term:
                degree_hits.append(degree_term)
            hedge_hits.extend(local_hedges)
            trace.append(
                {
                    "clause": clause_text,
                    "cue": hit["term"],
                    "lexical_polarity": hit["polarity"],
                    "base_weight": hit["weight"],
                    "negation_multiplier": negation_multiplier,
                    "degree_multiplier": degree_multiplier,
                    "contrast_multiplier": contrast_weight,
                    "hedge_multiplier": hedge_weight,
                    "punctuation_multiplier": rounded(punctuation_weight),
                    "contribution": rounded(contribution),
                }
            )

    alpha = float(scale["normalization_alpha"])
    normalized = 0.0 if not total else 5.0 * total / math.sqrt(total * total + alpha)
    normalized = clamp(normalized, float(scale["minimum"]), float(scale["maximum"]))
    neutral_band = float(scale["neutral_band"])
    mixed_minimum = float(codebook["thresholds"]["mixed_minimum_absolute_contribution"])
    mixed_share = float(codebook["thresholds"]["mixed_secondary_share"])
    both_polarities = positive_contribution >= mixed_minimum and negative_contribution >= mixed_minimum
    secondary_share = min(positive_contribution, negative_contribution) / max(
        positive_contribution,
        negative_contribution,
        1e-12,
    )
    neutral_hits = all_term_hits(text, neutral_cues)
    if both_polarities and secondary_share >= mixed_share:
        sentiment = "mixed"
        basis = "positive_and_negative_cues"
    elif trace and normalized >= neutral_band:
        sentiment = "positive"
        basis = "rule_adjusted_positive"
    elif trace and normalized <= -neutral_band:
        sentiment = "negative"
        basis = "rule_adjusted_negative"
    elif trace or neutral_hits:
        sentiment = "neutral"
        basis = "neutral_band_or_explicit_neutral"
    else:
        sentiment = "na"
        basis = "no_directional_cue"

    figurative_hits = all_term_hits(text, figurative_cues)
    return {
        "sentiment": sentiment,
        "basis": basis,
        "raw_total": rounded(total),
        "score_continuous": rounded(normalized),
        "score_final": round_half_away_from_zero(normalized),
        "positive_contribution": rounded(positive_contribution),
        "negative_contribution": rounded(negative_contribution),
        "negation_hits": negation_hits,
        "degree_hits": degree_hits,
        "hedge_hits": hedge_hits,
        "contrast_hits": contrast_hits,
        "figurative_hits": figurative_hits,
        "neutral_hits": neutral_hits,
        "clause_count": len(clauses),
        "trace": trace,
    }


def text_completeness(text: str, minimum_characters: int) -> float:
    length = len(re.sub(r"\s+", "", text))
    if length >= 30:
        return 1.0
    if length >= 15:
        return 0.8
    if length >= minimum_characters:
        return 0.6
    if length:
        return 0.3
    return 0.0


def evidence_reliability(
    row: dict[str, str],
    source: dict[str, str],
    completeness: float,
    codebook: dict[str, object],
) -> float:
    components = codebook["confidence_components"]
    assert isinstance(components, dict)
    weights = components["evidence_reliability"]
    assert isinstance(weights, dict)
    direct = 1.0 if parse_bool(row.get("is_direct_place_evidence")) is True else 0.0
    explicit = parse_bool(row.get("explicit_visit"))
    explicit_value = 1.0 if explicit is True else 0.0
    relevance_value = {
        "direct": 1.0,
        "indirect": 0.2,
        "high": 1.0,
        "medium": 0.6,
        "low": 0.2,
        "高": 1.0,
        "中": 0.6,
        "低": 0.2,
    }.get(row.get("place_relevance", "").strip(), 0.0)
    time_value = 1.0 if parse_bool(row.get("time_complete")) is True else 0.0
    promotion = parse_bool(source.get("suspected_promotion"))
    non_promotion = 1.0 if promotion is False else 0.0
    value = (
        float(weights["direct_place_relevance"]) * direct
        + float(weights["explicit_experience"]) * explicit_value
        + float(weights["place_relevance"]) * relevance_value
        + float(weights["text_completeness"]) * completeness
        + float(weights["time_completeness"]) * time_value
        + float(weights["non_promotional_source"]) * non_promotion
    )
    return clamp(value)


def normalize_native_rating(row: dict[str, str]) -> float | None:
    raw_value = row.get("native_rating_value", "")
    if all(row.get(key) in (None, "") for key in
           ("native_rating_value", "native_rating_scale_min", "native_rating_scale_max")):
        return None
    from input_safety import native_numbers, finite_number
    numbers = native_numbers(row)
    value, minimum, maximum = (numbers[key] for key in
        ("native_rating_value", "native_rating_scale_min", "native_rating_scale_max"))
    return finite_number(-5.0 + 10.0 * (value - minimum) / (maximum - minimum),
                         field="native_rating_normalized")


def validate_locked_protocol(
    codebook: dict[str, object],
    codebook_path: Path,
    protocol_path: Path,
    rows: list[dict[str, str]],
) -> dict[str, object]:
    task_run_ids = {row.get("task_run_id", "").strip() for row in rows if row.get("task_run_id", "").strip()}
    if len(task_run_ids) != 1:
        raise ValueError("semantic quantification requires exactly one non-empty task_run_id")
    task_run_id = next(iter(task_run_ids))
    if "runtime_refresh" in codebook:
        raise ValueError("task-bound runtime refresh data is forbidden; use the released locked protocol")
    release = codebook.get("release_protocol")
    if not isinstance(release, dict):
        raise ValueError("semantic quantification requires a released locked codebook")
    protocol = load_formal_protocol(protocol_path, codebook_path)
    actual_sha = hashlib.sha256(codebook_path.read_bytes()).hexdigest()
    for field, minimum in (("query_count", 3), ("source_count", 3), ("source_domain_count", 2), ("decision_count", 1)):
        try:
            value = int(release.get(field, 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"released semantic codebook calibration field {field} is invalid") from exc
        if value < minimum:
            raise ValueError(f"released semantic codebook calibration field {field} must be at least {minimum}")
    return {
        "task_run_id": task_run_id,
        "evaluation_protocol_version": release.get("evaluation_protocol_version"),
        "semantic_codebook_version": release.get("semantic_codebook_version"),
        "released_at": release.get("released_at"),
        "semantic_codebook_sha256": actual_sha,
        "calibration_mode": release.get("mode"),
        "task_level_online_rule_refresh": False,
        "task_level_rule_mutation": False,
    }


def semantic_confidence(
    text: str,
    aspect: dict[str, object],
    sentiment: dict[str, object],
    completeness: float,
    codebook: dict[str, object],
) -> float:
    components = codebook["confidence_components"]
    assert isinstance(components, dict)
    weights = components["semantic"]
    assert isinstance(weights, dict)
    trace = sentiment["trace"]
    assert isinstance(trace, list)
    polarity_coverage = 1.0 if len(trace) >= 2 else 0.75 if len(trace) == 1 else 0.45 if sentiment["neutral_hits"] else 0.15
    aspect_clarity = float(aspect["confidence"] or 0.0)
    rule_clarity = 1.0
    if sentiment["sentiment"] == "mixed":
        rule_clarity -= 0.15
    if sentiment["figurative_hits"]:
        rule_clarity -= 0.35
    if aspect["basis"] in {"declared_lexical_conflict", "invalid_declared_dimension", "unresolved"}:
        rule_clarity -= 0.25
    if len(sentiment["contrast_hits"]) > 1:
        rule_clarity -= 0.1
    if not text:
        rule_clarity = 0.0
    value = (
        float(weights["polarity_coverage"]) * polarity_coverage
        + float(weights["aspect_clarity"]) * aspect_clarity
        + float(weights["text_completeness"]) * completeness
        + float(weights["rule_clarity"]) * clamp(rule_clarity)
    )
    return clamp(value)


def analyze_row(
    row: dict[str, str],
    source: dict[str, str],
    codebook: dict[str, object],
    apply_auto_codes: bool = False,
    trusted_review_input: bool = True,
) -> tuple[dict[str, str], list[str]]:
    updated = dict(row)
    for field in SEMANTIC_FIELDS:
        updated.setdefault(field, "")
    existing_method = updated.get("semantic_method", "").strip()
    existing_review = updated.get("semantic_review_status", "").strip()
    existing_adjudication = updated.get("adjudication_status", "").strip().lower()
    human_confirmation_claimed = (
        existing_method in {"manual_code", "assisted_semantic"}
        and (
            existing_review == "human_confirmed"
            or existing_adjudication in {"confirmed", "human_confirmed", "已复核"}
        )
    )
    human_confirmed_coding = (
        human_confirmation_claimed
        and trusted_review_input
        and trusted_human_review(updated)
    )
    original_text = row.get("original_summary_text", "").strip()
    text = (original_text or row.get("excerpt_or_summary") or "").strip()
    thresholds = codebook["thresholds"]
    assert isinstance(thresholds, dict)
    completeness = text_completeness(text, int(thresholds["minimum_text_characters"]))
    native_normalized = normalize_native_rating(updated)
    if native_normalized is not None:
        updated["native_rating_normalized"] = str(rounded(native_normalized))

    aspect = aspect_assignment(text, row.get("primary_dimension", ""), codebook)
    sentiment = sentiment_analysis(text, codebook)
    language = detect_language(text)
    dimension_hit_count = sum(
        int(details.get("count", 0))
        for details in aspect.get("hits", {}).values()
        if isinstance(details, dict)
    ) if isinstance(aspect.get("hits"), dict) else 0
    polarity_hit_count = len(sentiment.get("trace", [])) + len(sentiment.get("neutral_hits", []))
    if dimension_hit_count and polarity_hit_count:
        lexicon_status = "full_match"
    elif dimension_hit_count:
        lexicon_status = "dimension_only"
    elif polarity_hit_count:
        lexicon_status = "polarity_only"
    else:
        lexicon_status = "zero_hit"
    parse_result = coding_parse(text, language, lexicon_status, codebook)
    lexicon_trace = {
        "language": language,
        "status": lexicon_status,
        "dimension_hit_count": dimension_hit_count,
        "polarity_hit_count": polarity_hit_count,
        "matched_dimensions": sorted(aspect.get("hits", {})) if isinstance(aspect.get("hits"), dict) else [],
        "polarity_cues": [str(item.get("cue", "")) for item in sentiment.get("trace", []) if isinstance(item, dict)],
        "neutral_cues": [str(item) for item in sentiment.get("neutral_hits", [])],
    }
    confidence = semantic_confidence(text, aspect, sentiment, completeness, codebook)
    # A complete, capture-verified native scale is the numeric observation;
    # it must not be penalised for correctly omitting fabricated body text.
    reliability_completeness = 1.0 if native_normalized is not None else completeness
    reliability = evidence_reliability(row, source, reliability_completeness, codebook)
    aggregation_weight = confidence * reliability
    reasons: list[str] = []
    if human_confirmation_claimed and not human_confirmed_coding:
        reasons.append("review_provenance_invalid")

    if human_confirmed_coding:
        declared_dimension = updated.get("primary_dimension", "").strip()
        declared_sentiment = updated.get("sentiment", "").strip()
        declared_coder = updated.get("coder_id", "").strip()
        try:
            declared_stance = int(updated.get("stance_strength", ""))
        except ValueError as exc:
            raise ValueError("human-confirmed stance_strength must be an integer from 1 to 5") from exc
        if declared_dimension not in DIMENSION_NAMES:
            raise ValueError("human-confirmed primary_dimension is invalid")
        if declared_sentiment not in {"positive", "neutral", "negative"}:
            raise ValueError("human-confirmed sentiment is invalid")
        if declared_stance not in {1, 2, 3, 4, 5} or not declared_coder:
            raise ValueError("human-confirmed coding lacks stance or coder provenance")
        aspect = {
            **aspect,
            "primary_dimension": declared_dimension,
            "basis": "human_confirmed_review",
            "confidence": 1.0,
        }
        review_status = "human_confirmed"
        method = existing_method
        existing_tags = [
            part.strip()
            for part in re.split(r"[;；|]", updated.get("dimension_tags", ""))
            if part.strip()
        ]
        if declared_dimension not in existing_tags:
            existing_tags.append(declared_dimension)
        updated["dimension_tags"] = ";".join(existing_tags)
    elif native_normalized is not None:
        if updated.get("primary_dimension", "").strip() not in DIMENSION_NAMES:
            raise ValueError("native numeric evidence requires one canonical primary_dimension")
        review_status = "not_applicable"
        method = "source_native_numeric"
        lexicon_status = "not_applicable"
        parse_result = {"status": "not_applicable", "candidates": [], "trace": {}, "open_code_id": ""}
    elif not text:
        review_status = "not_applicable"
        method = "not_applicable"
        lexicon_status = "not_applicable"
        parse_result = {"status": "not_applicable", "candidates": [], "trace": {}, "open_code_id": ""}
        reasons.append("missing_visible_text")
    else:
        method = "rule_codebook" if lexicon_status == "full_match" else "coding_parse_fallback"
        if not original_text:
            reasons.append("missing_original_summary_text")
        if parse_bool(row.get("is_valid")) is not True:
            reasons.append("evidence_not_valid")
        if parse_bool(row.get("is_direct_place_evidence")) is not True:
            reasons.append("not_direct_place_evidence")
        if row.get("unit_type", "") not in SCORABLE_USER_UNITS:
            reasons.append("non_user_semantic_unit")
        if source:
            if source.get("access_status", "") not in {"full", "partial"}:
                reasons.append("source_not_readable")
            if parse_bool(source.get("is_user_source")) is not True:
                reasons.append("source_not_user_content")
            promotion_status = parse_bool(source.get("suspected_promotion"))
            if promotion_status is True:
                reasons.append("source_promotional")
            elif promotion_status is None:
                reasons.append("promotion_status_unknown")
        if sentiment["sentiment"] == "na":
            reasons.append("no_directional_cue")
        if not aspect["primary_dimension"]:
            reasons.append("dimension_unresolved")
        if aspect["basis"] in {"declared_lexical_conflict", "invalid_declared_dimension"}:
            reasons.append("dimension_conflict")
        if sentiment["figurative_hits"]:
            reasons.append("figurative_or_sarcasm_risk")
        if len(sentiment["contrast_hits"]) > 1:
            reasons.append("multiple_contrast_requires_review")
        if confidence < float(thresholds["semantic_auto_confidence"]):
            reasons.append("semantic_confidence_below_threshold")
        if reliability < float(thresholds["minimum_evidence_reliability"]):
            reasons.append("evidence_reliability_below_threshold")
        if float(aspect["confidence"] or 0.0) < float(thresholds["dimension_auto_confidence"]):
            reasons.append("aspect_confidence_below_threshold")
        if lexicon_status == "zero_hit":
            reasons.append("lexicon_zero_hit_coding_parse_started")
        elif lexicon_status != "full_match":
            reasons.append("lexicon_partial_hit_coding_parse_started")
        if parse_result["status"] == "started_unresolved":
            reasons.append("coding_parse_unresolved")
        elif parse_result["status"] == "candidate_generated":
            reasons.append("coding_parse_candidate_requires_confirmation")
        review_status = "auto_eligible" if not reasons else "review_required"

    lexicon_trace["status"] = lexicon_status
    # Keep the upstream research-selection claim unchanged.  A fallback row is
    # excluded by formal_scoring_eligible and review status; overwriting
    # used_for_scoring here would prevent a later schema-constrained human
    # confirmation from returning through the released derivation chain.

    if method == "rule_codebook" and trusted_review_input and trusted_human_review(updated):
        review_status = "human_confirmed"

    if method == "source_native_numeric":
        semantic_clause_count = ""
        polarity_basis = "source_native_numeric"
        negation_hits = ""
        degree_hits = ""
        contrast_hits = ""
        hedge_hits = ""
        figurative_risk = "false"
        semantic_score_raw = ""
        semantic_score_final = ""
        semantic_confidence_value = ""
        aggregation_weight_value = str(rounded(reliability))
        semantic_rule_trace = "[]"
    elif method == "not_applicable":
        semantic_clause_count = ""
        polarity_basis = "not_applicable"
        negation_hits = ""
        degree_hits = ""
        contrast_hits = ""
        hedge_hits = ""
        figurative_risk = "false"
        semantic_score_raw = ""
        semantic_score_final = ""
        semantic_confidence_value = ""
        aggregation_weight_value = ""
        semantic_rule_trace = "[]"
    elif method in {"manual_code", "assisted_semantic"}:
        semantic_clause_count = str(sentiment["clause_count"])
        polarity_basis = "human_confirmed_review"
        negation_hits = "|".join(str(item) for item in sentiment["negation_hits"])
        degree_hits = "|".join(str(item) for item in sentiment["degree_hits"])
        contrast_hits = "|".join(str(item) for item in sentiment["contrast_hits"])
        hedge_hits = "|".join(str(item) for item in sentiment["hedge_hits"])
        figurative_risk = "true" if sentiment["figurative_hits"] else "false"
        manual_sentiment = updated["sentiment"]
        manual_stance = int(updated["stance_strength"])
        manual_score = (
            0 if manual_sentiment == "neutral"
            else manual_stance if manual_sentiment == "positive"
            else -manual_stance
        )
        updated["sentiment_score"] = str(manual_score)
        semantic_score_raw = str(manual_score)
        semantic_score_final = str(manual_score)
        semantic_confidence_value = str(rounded(confidence))
        aggregation_weight_value = str(rounded(aggregation_weight))
        semantic_rule_trace = json.dumps(
            [
                *sentiment["trace"],
                {
                    "stage": "human_review_coding",
                    "coder_id": updated.get("coder_id", ""),
                    "adjudication_status": updated.get("adjudication_status", ""),
                    "primary_dimension": updated.get("primary_dimension", ""),
                    "sentiment": manual_sentiment,
                    "stance_strength": manual_stance,
                    "score_final": manual_score,
                    "score_mapping": "released_signed_stance_scale",
                },
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    elif method == "coding_parse_fallback":
        semantic_clause_count = str(sentiment["clause_count"])
        polarity_basis = "coding_parse_candidate" if any(
            isinstance(item, dict) and item.get("candidate_type") == "polarity"
            for item in parse_result["candidates"]
        ) else str(sentiment["basis"])
        negation_hits = "|".join(str(item) for item in sentiment["negation_hits"])
        degree_hits = "|".join(str(item) for item in sentiment["degree_hits"])
        contrast_hits = "|".join(str(item) for item in sentiment["contrast_hits"])
        hedge_hits = "|".join(str(item) for item in sentiment["hedge_hits"])
        figurative_risk = "true" if sentiment["figurative_hits"] else "false"
        semantic_score_raw = ""
        semantic_score_final = ""
        semantic_confidence_value = str(rounded(min(confidence, 0.71)))
        aggregation_weight_value = ""
        semantic_rule_trace = json.dumps(
            [
                *sentiment["trace"],
                {
                    "stage": "coding_parse",
                    "status": parse_result["status"],
                    "candidates": parse_result["candidates"],
                    "scoring_policy": "review_required_no_automatic_score",
                },
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    else:
        semantic_clause_count = str(sentiment["clause_count"])
        polarity_basis = str(sentiment["basis"])
        negation_hits = "|".join(str(item) for item in sentiment["negation_hits"])
        degree_hits = "|".join(str(item) for item in sentiment["degree_hits"])
        contrast_hits = "|".join(str(item) for item in sentiment["contrast_hits"])
        hedge_hits = "|".join(str(item) for item in sentiment["hedge_hits"])
        figurative_risk = "true" if sentiment["figurative_hits"] else "false"
        semantic_score_raw = str(sentiment["score_continuous"])
        semantic_score_final = str(sentiment["score_final"])
        semantic_confidence_value = str(rounded(confidence))
        aggregation_weight_value = str(rounded(aggregation_weight))
        semantic_rule_trace = json.dumps(sentiment["trace"], ensure_ascii=False, separators=(",", ":"))

    updated.update(
        {
            "semantic_method": method,
            "semantic_language": language,
            "semantic_unit_text": text,
            "semantic_clause_count": semantic_clause_count,
            "lexicon_match_status": lexicon_status,
            "lexicon_match_trace": json.dumps(lexicon_trace, ensure_ascii=False, separators=(",", ":")),
            "coding_parse_status": str(parse_result["status"]),
            "coding_parse_candidates": json.dumps(parse_result["candidates"], ensure_ascii=False, separators=(",", ":")),
            "coding_parse_trace": json.dumps(parse_result["trace"], ensure_ascii=False, separators=(",", ":")),
            "open_code_id": str(parse_result["open_code_id"]),
            "aspect_assignment_basis": (
                "coding_parse_candidate"
                if method == "coding_parse_fallback"
                and any(
                    isinstance(item, dict) and item.get("candidate_type") == "dimension"
                    for item in parse_result["candidates"]
                )
                else str(aspect["basis"])
            ),
            "aspect_confidence": str(aspect["confidence"]),
            "dimension_rule_hits": json.dumps(aspect["hits"], ensure_ascii=False, separators=(",", ":")),
            "polarity_basis": polarity_basis,
            "negation_hits": negation_hits,
            "degree_hits": degree_hits,
            "contrast_hits": contrast_hits,
            "hedge_hits": hedge_hits,
            "figurative_risk": figurative_risk,
            "semantic_score_raw": semantic_score_raw,
            "semantic_score_final": semantic_score_final,
            "semantic_confidence": semantic_confidence_value,
            "evidence_reliability": str(rounded(reliability)),
            "aggregation_weight": aggregation_weight_value,
            "semantic_review_status": review_status,
            "semantic_rule_trace": semantic_rule_trace,
        }
    )

    if apply_auto_codes and review_status in REVIEW_ACCEPTED and method == "rule_codebook":
        proposed_dimension = str(aspect["primary_dimension"])
        if not updated.get("primary_dimension"):
            updated["primary_dimension"] = proposed_dimension
        existing_tags = [
            part.strip()
            for part in re.split(r"[;；|]", updated.get("dimension_tags", ""))
            if part.strip()
        ]
        if proposed_dimension and proposed_dimension not in existing_tags:
            existing_tags.append(proposed_dimension)
        updated["dimension_tags"] = ";".join(existing_tags)
        updated["sentiment"] = str(sentiment["sentiment"])
        updated["sentiment_score"] = str(sentiment["score_final"])
        updated["stance_strength"] = str(max(1, abs(int(sentiment["score_final"]))))
        if not updated.get("normalized_theme"):
            hit_details = aspect["hits"].get(proposed_dimension, {}) if isinstance(aspect["hits"], dict) else {}
            cues = hit_details.get("cues", {}) if isinstance(hit_details, dict) else {}
            updated["normalized_theme"] = max(cues, key=cues.get) if cues else proposed_dimension
    elif apply_auto_codes and method == "source_native_numeric" and native_normalized is not None:
        native_final = round_half_away_from_zero(native_normalized)
        if not updated.get("sentiment_score"):
            updated["sentiment_score"] = str(native_final)
        if not updated.get("sentiment") or updated.get("sentiment") == "na":
            updated["sentiment"] = (
                "positive" if native_final > 0 else "negative" if native_final < 0 else "neutral"
            )
        if not updated.get("stance_strength"):
            updated["stance_strength"] = str(max(1, abs(native_final)))

    return updated, reasons


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    usable = [(left, right) for left, right in pairs if left and right]
    if not usable:
        return None
    observed = sum(left == right for left, right in usable) / len(usable)
    left_counts = Counter(left for left, _ in usable)
    right_counts = Counter(right for _, right in usable)
    labels = set(left_counts) | set(right_counts)
    expected = sum(
        left_counts[label] / len(usable) * right_counts[label] / len(usable)
        for label in labels
    )
    if math.isclose(expected, 1.0):
        return 1.0 if math.isclose(observed, 1.0) else None
    return (observed - expected) / (1.0 - expected)


def krippendorff_alpha(
    units: dict[str, list[str]],
    metric: str = "nominal",
    ordinal_order: list[str] | None = None,
) -> float | None:
    coincidence: defaultdict[tuple[str, str], float] = defaultdict(float)
    for values in units.values():
        usable = [value for value in values if value]
        count = len(usable)
        if count < 2:
            continue
        for first_index, first in enumerate(usable):
            for second_index, second in enumerate(usable):
                if first_index != second_index:
                    coincidence[(first, second)] += 1.0 / (count - 1)
    total = sum(coincidence.values())
    if total <= 1:
        return None
    marginals: defaultdict[str, float] = defaultdict(float)
    for (first, _), value in coincidence.items():
        marginals[first] += value

    if metric == "ordinal":
        order = ordinal_order or sorted(marginals)
        ranks = {label: index for index, label in enumerate(order)}
        denominator = max(1, len(order) - 1)

        def distance(left: str, right: str) -> float:
            if left not in ranks or right not in ranks:
                return 1.0
            return ((ranks[left] - ranks[right]) / denominator) ** 2
    else:
        def distance(left: str, right: str) -> float:
            return 0.0 if left == right else 1.0

    observed_disagreement = sum(
        value * distance(first, second)
        for (first, second), value in coincidence.items()
    ) / total
    expected_disagreement = sum(
        first_count * second_count * distance(first, second)
        for first, first_count in marginals.items()
        for second, second_count in marginals.items()
    ) / (total * (total - 1.0))
    if math.isclose(expected_disagreement, 0.0):
        return 1.0 if math.isclose(observed_disagreement, 0.0) else None
    return 1.0 - observed_disagreement / expected_disagreement


def agreement_audit(path: Path | None) -> dict[str, object]:
    if path is None:
        return {"status": "not_provided"}
    _, rows = read_csv(path)
    required = {"evidence_id", "coder_id", "primary_dimension", "sentiment", "stance_strength"}
    fields = set(rows[0]) if rows else set()
    missing = sorted(required - fields)
    if missing:
        raise ValueError(f"agreement ledger is missing fields: {', '.join(missing)}")
    by_unit: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    coders: set[str] = set()
    for row in rows:
        if row["evidence_id"] and row["coder_id"]:
            by_unit[row["evidence_id"]].append(row)
            coders.add(row["coder_id"])
    result: dict[str, object] = {
        "status": "calculated",
        "coder_count": len(coders),
        "unit_count": len(by_unit),
    }
    for field in ("primary_dimension", "sentiment"):
        units = {
            unit: [item.get(field, "") for item in items]
            for unit, items in by_unit.items()
        }
        result[f"krippendorff_alpha_{field}"] = rounded(krippendorff_alpha(units))
    stance_units = {
        unit: [item.get("stance_strength", "") for item in items]
        for unit, items in by_unit.items()
    }
    result["krippendorff_alpha_stance_ordinal"] = rounded(
        krippendorff_alpha(stance_units, metric="ordinal", ordinal_order=["1", "2", "3", "4", "5"])
    )
    if len(coders) == 2:
        left, right = sorted(coders)
        for field in ("primary_dimension", "sentiment"):
            pairs: list[tuple[str, str]] = []
            for items in by_unit.values():
                values = {item["coder_id"]: item.get(field, "") for item in items}
                if left in values and right in values:
                    pairs.append((values[left], values[right]))
            result[f"cohen_kappa_{field}"] = rounded(cohen_kappa(pairs))
    return result


def mention_level(count: int, denominator: int) -> str:
    ratio = count / max(1, denominator)
    if ratio >= 0.50:
        return "A"
    if ratio >= 0.30:
        return "B"
    if ratio >= 0.15:
        return "C"
    if ratio >= 0.05:
        return "D"
    return "E" if count else "U"


def build_platform_scores(
    place_name: str,
    rows: list[dict[str, str]],
    codebook: dict[str, object],
    *,
    sources: list[dict[str, str]],
    search_rows: list[dict[str, str]],
    dimension_audit: dict[str, object],
    task_run_id: str,
    release_protocol_version: str,
    protocol: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build formal platform input only after a ledger-bound final audit.

    There is deliberately no permissive default for the audit, task, search
    ledger, or released protocol.  Diagnostic callers must use the lower-level
    formal-scoring chain directly and may not label that result as formal.
    """
    protocol = protocol or load_formal_protocol()
    if release_protocol_version != protocol.get("evaluation_protocol_version"):
        raise ValueError("release protocol version does not match the locked evaluation protocol")
    if codebook.get("thresholds") != protocol.get("thresholds"):
        raise ValueError("semantic codebook thresholds drifted from the locked evaluation protocol")
    validated_audit = validate_machine_dimension_audit(
        dimension_audit,
        task_run_id=task_run_id,
        evidence=rows,
        sources=sources,
        search_rows=search_rows,
    )
    chain = build_formal_scoring_chain(rows, sources, protocol=protocol)
    platforms = chain["platforms"]
    assert isinstance(platforms, list)
    for platform in platforms:
        assert isinstance(platform, dict)
        denominator = int(platform["eligible_samples"])
        dimensions = platform["dimensions"]
        assert isinstance(dimensions, dict)
        for entry in dimensions.values():
            assert isinstance(entry, dict)
            eligible_units = int(entry["eligible_evidence_units"])
            entry["mention_level"] = mention_level(eligible_units, denominator)
            # Compatibility alias: it always means actual scored units.
            entry["evidence_units"] = int(entry["scored_evidence_units"])
    result: dict[str, object] = {
        "task_run_id": task_run_id,
        "place_name": place_name,
        "formal_scoring_schema_version": chain["schema_version"],
        "evaluation_protocol_version": chain["evaluation_protocol_version"],
        "semantic_codebook_version": chain["semantic_codebook_version"],
        "minimum_dimension_units": chain["minimum_dimension_units"],
        "dimension_assignment_field": "primary_dimension",
        "topic_coverage_field": "dimension_tags",
        "dimension_summary": chain["dimensions"],
        "platforms": platforms,
        "dimension_evidence_audit": validated_audit,
    }
    return seal_scoring_input(result)


def quantify(
    evidence_path: Path,
    output_path: Path,
    codebook_path: Path = DEFAULT_CODEBOOK,
    protocol_path: Path = DEFAULT_PROTOCOL,
    sources_path: Path | None = None,
    audit_output: Path | None = None,
    platform_scores_output: Path | None = None,
    dimension_audit_path: Path | None = None,
    search_log_path: Path | None = None,
    agreement_path: Path | None = None,
    coding_queue_output: Path | None = None,
    place_name: str = "",
    apply_auto_codes: bool = False,
    trusted_review_input: bool = True,
    execution_schema_context: dict[str, object] | None = None,
    finalize_unreviewed: bool = False,
) -> dict[str, object]:
    fields, rows = read_csv(evidence_path)
    codebook = load_codebook(codebook_path)
    sources: list[dict[str, str]] = []
    if sources_path:
        _, sources = read_csv(sources_path)
    # A context-only collection has a real source identity but no perception
    # units. Validate the same locked protocol, without inventing an evidence row.
    locked_protocol = validate_locked_protocol(codebook, codebook_path, protocol_path, rows or sources)
    source_lookup = {row.get("source_id", ""): row for row in sources}
    from execution_amendments import exclude_retracted_evidence
    rows = exclude_retracted_evidence(rows, sources, execution_schema_context)
    output_rows: list[dict[str, str]] = []
    review_reasons: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    sentiments: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    lexicon_statuses: Counter[str] = Counter()
    coding_statuses: Counter[str] = Counter()
    coding_queue: list[dict[str, object]] = []
    for row in rows:
        source = source_lookup.get(row.get("source_id", ""), {})
        updated, reasons = analyze_row(
            row,
            source,
            codebook,
            apply_auto_codes=apply_auto_codes,
            trusted_review_input=trusted_review_input,
        )
        if finalize_unreviewed and updated.get('semantic_review_status') == 'review_required':
            # Formal projection only: preserve the semantic candidate and its
            # unresolved judgement for a later genuinely trusted review.
            # No review identity, confidence, score or qualification is granted.
            updated['used_for_scoring'] = 'false'
        output_rows.append(updated)
        review_reasons.update(reasons)
        statuses[updated.get("semantic_review_status", "")] += 1
        sentiments[updated.get("sentiment", "na")] += 1
        methods[updated.get("semantic_method", "")] += 1
        lexicon_statuses[updated.get("lexicon_match_status", "")] += 1
        coding_statuses[updated.get("coding_parse_status", "")] += 1
        if updated.get("semantic_method") == "coding_parse_fallback":
            try:
                candidates = json.loads(updated.get("coding_parse_candidates", "[]") or "[]")
                trace = json.loads(updated.get("coding_parse_trace", "{}") or "{}")
            except json.JSONDecodeError:
                candidates, trace = [], {}
            coding_queue.append(
                {
                    "task_run_id": updated.get("task_run_id", ""),
                    "evidence_id": updated.get("evidence_id", ""),
                    "source_id": updated.get("source_id", ""),
                    "semantic_language": updated.get("semantic_language", ""),
                    "semantic_unit_text": updated.get("semantic_unit_text", ""),
                    "lexicon_match_status": updated.get("lexicon_match_status", ""),
                    "coding_parse_status": updated.get("coding_parse_status", ""),
                    "open_code_id": updated.get("open_code_id", ""),
                    "candidates": candidates,
                    "trace": trace,
                    "required_next_action": "schema_constrained_assisted_or_human_coding",
                    "automatic_scoring_allowed": False,
                }
            )
    formal_protocol = load_formal_protocol(protocol_path, codebook_path)
    formal_chain = build_formal_scoring_chain(
        output_rows,
        sources,
        protocol=formal_protocol,
        require_source_contract=bool(sources),
    )
    output_rows = annotate_rows(output_rows, formal_chain)
    output_fields = fields + [field for field in [*SEMANTIC_FIELDS, *FORMAL_SCORING_FIELDS] if field not in fields]
    write_csv(output_path, output_fields, output_rows)

    agreement = agreement_audit(agreement_path)
    summary: dict[str, object] = {
        "status": "created",
        "method_id": codebook.get("method_id"),
        "input_rows": len(rows),
        "output_rows": len(output_rows),
        "semantic_methods": dict(methods),
        "lexicon_match_statuses": dict(lexicon_statuses),
        "coding_parse_statuses": dict(coding_statuses),
        "coding_parse_queue_rows": len(coding_queue),
        "review_statuses": dict(statuses),
        "sentiment_counts": dict(sentiments),
        "review_reasons": dict(review_reasons),
        "agreement": agreement,
        "locked_evaluation_protocol": locked_protocol,
        "formal_scoring": {
            "schema_version": formal_chain["schema_version"],
            "eligible_evidence_units": len(formal_chain["eligible_rows"]),
            "scored_evidence_units": len(formal_chain["scored_rows"]),
            "dimension_summary": formal_chain["dimensions"],
        },
        "output": str(output_path.resolve()),
    }
    if coding_queue_output:
        coding_queue_output.parent.mkdir(parents=True, exist_ok=True)
        coding_queue_output.write_text(
            "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in coding_queue),
            encoding="utf-8",
        )
        summary["coding_queue_output"] = str(coding_queue_output.resolve())
    if platform_scores_output:
        if dimension_audit_path is None:
            raise ValueError(
                "platform score input requires --dimension-audit after all seven dimensions reach the preferred target, pass the stronger medium-completion audit, or have supported low-confidence exhaustion"
            )
        dimension_payload = json.loads(dimension_audit_path.read_text(encoding="utf-8-sig"))
        if not isinstance(dimension_payload, dict):
            raise ValueError("dimension audit root must be a JSON object")
        if search_log_path is None:
            raise ValueError("platform score input requires --search-log for independent audit recomputation")
        _, search_rows = read_csv(search_log_path)
        run_ids = {
            row.get("task_run_id", "").strip()
            for row in output_rows
            if row.get("task_run_id", "").strip()
        }
        if len(run_ids) != 1:
            raise ValueError("formal evidence must contain exactly one task_run_id")
        task_run_id = next(iter(run_ids))
        dimension_audit = validate_machine_dimension_audit(
            dimension_payload,
            task_run_id=task_run_id,
            evidence=output_rows,
            sources=sources,
            search_rows=search_rows,
            execution_schema_context=execution_schema_context,
        )
        audit_dimensions = dimension_audit["dimensions"]
        for dimension in DIMENSION_NAMES:
            audit_item = audit_dimensions[dimension]
            chain_item = formal_chain["dimensions"][dimension]
            audit_scored = int(audit_item.get("scored_evidence_units", audit_item.get("evidence_units", 0)))
            if audit_scored != int(chain_item["scored_evidence_units"]):
                raise ValueError(
                    f"dimension audit and formal scoring chain disagree on scored_evidence_units: {dimension}"
                )
            if audit_item.get("status") in {"sufficient", "sufficient_at_medium_after_audit"} and chain_item["scoring_confidence"] not in {"中", "中高", "高"}:
                raise ValueError(
                    f"dimension audit marks scoring sufficient but formal scored evidence is insufficient: {dimension}"
                )
        platform_data = build_platform_scores(
            place_name,
            output_rows,
            codebook,
            sources=sources,
            search_rows=search_rows,
            dimension_audit=dimension_audit,
            task_run_id=task_run_id,
            release_protocol_version=str(formal_protocol["evaluation_protocol_version"]),
            protocol=formal_protocol,
        )
        platform_scores_output.parent.mkdir(parents=True, exist_ok=True)
        platform_scores_output.write_text(
            json.dumps(platform_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary["platform_scores_output"] = str(platform_scores_output.resolve())
        summary["platform_count"] = len(platform_data["platforms"])
    if audit_output:
        audit_output.parent.mkdir(parents=True, exist_ok=True)
        audit_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["audit_output"] = str(audit_output.resolve())
    return summary


def build_platform_scores_from_frozen_formal(
    *,
    formal_evidence_path: Path,
    sources_path: Path,
    search_log_path: Path,
    dimension_audit_path: Path,
    platform_scores_output: Path,
    codebook_path: Path,
    protocol_path: Path,
    place_name: str,
    execution_schema_context: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build platform input without rewriting the audited formal evidence."""
    _, rows = read_csv(formal_evidence_path)
    _, sources = read_csv(sources_path)
    _, search_rows = read_csv(search_log_path)
    codebook = load_codebook(codebook_path)
    validate_locked_protocol(codebook, codebook_path, protocol_path, rows)
    protocol = load_formal_protocol(protocol_path, codebook_path)
    dimension_payload = json.loads(dimension_audit_path.read_text(encoding="utf-8-sig"))
    if not isinstance(dimension_payload, dict):
        raise ValueError("dimension audit root must be a JSON object")
    run_ids = {
        row.get("task_run_id", "").strip()
        for row in rows
        if row.get("task_run_id", "").strip()
    }
    if len(run_ids) != 1:
        raise ValueError("formal evidence must contain exactly one task_run_id")
    task_run_id = next(iter(run_ids))
    dimension_audit = validate_machine_dimension_audit(
        dimension_payload,
        task_run_id=task_run_id,
        evidence=rows,
        sources=sources,
        search_rows=search_rows,
        execution_schema_context=execution_schema_context,
    )
    chain = build_formal_scoring_chain(
        rows,
        sources,
        protocol=protocol,
        require_source_contract=True,
    )
    for dimension in DIMENSION_NAMES:
        audit_item = dimension_audit["dimensions"][dimension]
        chain_item = chain["dimensions"][dimension]
        if int(audit_item.get("scored_evidence_units", 0)) != int(
            chain_item["scored_evidence_units"]
        ):
            raise ValueError(
                "dimension audit and formal scoring chain disagree on "
                f"scored_evidence_units: {dimension}"
            )
    payload = build_platform_scores(
        place_name,
        rows,
        codebook,
        sources=sources,
        search_rows=search_rows,
        dimension_audit=dimension_audit,
        task_run_id=task_run_id,
        release_protocol_version=str(protocol["evaluation_protocol_version"]),
        protocol=protocol,
    )
    platform_scores_output.parent.mkdir(parents=True, exist_ok=True)
    temporary = platform_scores_output.with_name(platform_scores_output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, platform_scores_output)
    return {
        "status": "created_from_frozen_formal_evidence",
        "task_run_id": task_run_id,
        "formal_evidence_rewritten": False,
        "formal_evidence": str(formal_evidence_path.resolve()),
        "platform_scores_output": str(platform_scores_output.resolve()),
        "platform_count": len(payload["platforms"]),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Evidence-ledger CSV")
    parser.add_argument("--output", required=True, type=Path, help="Enriched evidence-ledger CSV")
    parser.add_argument(
        "--codebook",
        required=True,
        type=Path,
        help="Released semantic codebook locked with the evaluation protocol",
    )
    parser.add_argument(
        "--protocol",
        type=Path,
        default=DEFAULT_PROTOCOL,
        help="Released evaluation-protocol JSON containing the codebook SHA-256",
    )
    parser.add_argument("--sources", type=Path, help="Optional source-ledger CSV for promotion status")
    parser.add_argument("--audit-output", type=Path, help="Optional semantic audit JSON")
    parser.add_argument("--platform-scores", type=Path, help="Optional platform score-input JSON")
    parser.add_argument(
        "--dimension-audit",
        type=Path,
        help="Final evidence-audit JSON required when writing platform score input",
    )
    parser.add_argument(
        "--search-log",
        type=Path,
        help="Search ledger required for machine recomputation of the final dimension audit",
    )
    parser.add_argument("--agreement-input", type=Path, help="Optional double-coding CSV")
    parser.add_argument("--coding-queue", type=Path, help="Optional JSONL queue for lexical gaps requiring coding")
    parser.add_argument("--place", default="", help="Current research-object label for the score-input JSON")
    parser.add_argument(
        "--apply-auto-codes",
        action="store_true",
        help="Populate blank dimension and sentiment fields only for rows that pass automatic thresholds",
    )
    parser.add_argument(
        "--output-role",
        choices=["semantic_evidence", "formal_evidence"],
        default="semantic_evidence",
        help="Protected writer role for the enriched evidence output",
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--writer-ledger", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        phase = "SCORE" if args.platform_scores else "SEMANTIC_QUANTIFICATION"
        input_roles = (
            {"formal_evidence"}
            if phase == "SCORE"
            else {"raw_evidence", "semantic_evidence", "reviewed_evidence"}
        )
        input_writer = verify_artifact_writer_any(
            state_path=args.state,
            writer_ledger_path=args.writer_ledger,
            output_roles=input_roles,
            output_path=args.input,
        )
        if args.sources is not None:
            verify_artifact_writer(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                output_role="source_ledger",
                output_path=args.sources,
            )
        if args.platform_scores is not None:
            if args.dimension_audit is None or args.search_log is None:
                raise ValueError("平台评分输入必须绑定正式证据审计与检索日志")
            if args.sources is None:
                raise ValueError("平台评分输入必须绑定正式来源台账")
            if args.output.resolve() != args.input.resolve():
                raise ValueError("SCORE阶段不得重写或复制已冻结的正式证据")
            verify_artifact_writer(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                output_role="evidence_audit",
                output_path=args.dimension_audit,
            )
            verify_artifact_writer(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                output_role="search_log",
                output_path=args.search_log,
            )
        else:
            authorize_runtime_write(
                state_path=args.state,
                writer_script_id="quantify_text_semantics.py",
                output_role=args.output_role,
                expected_phase=phase,
                output_path=args.output,
            )
        optional_outputs = [
            (args.audit_output, "semantic_audit"),
            (args.coding_queue, "coding_queue"),
            (args.platform_scores, "platform_scores"),
        ]
        for path, role in optional_outputs:
            if path is not None:
                authorize_runtime_write(
                    state_path=args.state,
                    writer_script_id="quantify_text_semantics.py",
                    output_role=role,
                    expected_phase=phase,
                    output_path=path,
                )
        trusted_review_input = input_writer.get("output_role") == "reviewed_evidence"
        if not trusted_review_input and input_writer.get("output_role") == "semantic_evidence":
            inputs = input_writer.get("inputs")
            evidence_input = inputs.get("evidence") if isinstance(inputs, dict) else None
            upstream_path = (
                Path(str(evidence_input.get("path", "")))
                if isinstance(evidence_input, dict)
                else None
            )
            if upstream_path is not None and upstream_path.is_file():
                try:
                    verify_artifact_writer(
                        state_path=args.state,
                        writer_ledger_path=args.writer_ledger,
                        output_role="reviewed_evidence",
                        output_path=upstream_path,
                    )
                except (OSError, ValueError):
                    pass
                else:
                    trusted_review_input = True
        if phase == "SCORE":
            from execution_facts import context_from_state
            from retrieval_controls import load_retrieval_config
            from runtime_guard import load_runtime_state
            execution_context = context_from_state(load_runtime_state(args.state),
                config=load_retrieval_config(), state_path=args.state)
            if args.audit_output is not None or args.coding_queue is not None:
                raise ValueError("SCORE阶段只允许从冻结正式证据生成平台评分输入")
            result = build_platform_scores_from_frozen_formal(
                formal_evidence_path=args.input,
                sources_path=args.sources,
                search_log_path=args.search_log,
                dimension_audit_path=args.dimension_audit,
                platform_scores_output=args.platform_scores,
                codebook_path=args.codebook,
                protocol_path=args.protocol,
                place_name=args.place.strip(),
                execution_schema_context=execution_context,
            )
        else:
            result = quantify(
                evidence_path=args.input,
                output_path=args.output,
                codebook_path=args.codebook,
                protocol_path=args.protocol,
                sources_path=args.sources,
                audit_output=args.audit_output,
                platform_scores_output=None,
                dimension_audit_path=None,
                search_log_path=None,
                agreement_path=args.agreement_input,
                coding_queue_output=args.coding_queue,
                place_name=args.place.strip(),
                apply_auto_codes=args.apply_auto_codes,
                trusted_review_input=trusted_review_input,
                finalize_unreviewed=args.output_role == 'formal_evidence',
                execution_schema_context=__import__('execution_facts').context_from_state(
                    __import__('runtime_guard').load_runtime_state(args.state),
                    config=__import__('retrieval_controls').load_retrieval_config(), state_path=args.state),
            )
        inputs = {"evidence": args.input, "codebook": args.codebook, "protocol": args.protocol}
        if args.sources:
            inputs["sources"] = args.sources
        if args.dimension_audit:
            inputs["dimension_audit"] = args.dimension_audit
        if args.search_log:
            inputs["search_log"] = args.search_log
        if args.agreement_input:
            inputs["agreement_input"] = args.agreement_input
        if phase != "SCORE":
            register_protected_artifact(
                state_path=args.state,
                writer_ledger_path=args.writer_ledger,
                writer_script_id="quantify_text_semantics.py",
                output_role=args.output_role,
                output_path=args.output,
                input_paths=inputs,
                expected_phase=phase,
            )
        for output_path, role in optional_outputs:
            if output_path is not None and output_path.is_file():
                register_protected_artifact(
                    state_path=args.state,
                    writer_ledger_path=args.writer_ledger,
                    writer_script_id="quantify_text_semantics.py",
                    output_role=role,
                    output_path=output_path,
                    input_paths=inputs,
                    expected_phase=phase,
                )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "invalid", "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
