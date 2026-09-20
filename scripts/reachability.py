"""Observed reachability is diagnostic, never an alternative stopping rule."""
import csv
import math
from pathlib import Path
from source_identity import page_entity_id_for_source


def _rows(path):
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def diagnostics(root, *, target_units, remaining_page_budget=None):
    root = Path(root)
    sources = _rows(root / "canonical/active-source-ledger.csv")
    evidence = _rows(root / "derived/formal-evidence.csv")
    pages = len({page_entity_id_for_source(s) for s in sources if page_entity_id_for_source(s)})
    users = {s["source_id"] for s in sources if s.get("is_user_source") == "true"}
    user_pages = {page_entity_id_for_source(s) for s in sources
                  if s.get('source_id') in users and page_entity_id_for_source(s)}
    direct = [e for e in evidence if e.get("source_id") in users and e.get("is_direct_place_evidence") == "true"]
    eligible = sum(e.get("formal_scoring_eligible") == "true" for e in direct)
    scored = sum(e.get("included_in_platform_score") == "true" for e in direct)
    rate = eligible / pages if pages else None
    scored_rate = scored / pages if pages else None
    required = math.ceil(max(0, target_units - scored) / scored_rate) if scored_rate else None
    history = _rows(root / "derived/refreshed-search.csv")
    rounds = {}
    for row in history:
        try:
            index = int(row["iteration_round"])
            units = int(row.get("cumulative_scored_evidence_units") or 0)
        except (KeyError, ValueError):
            continue
        rounds[index] = max(rounds.get(index, 0), units)
    previous = 0
    marginal = []
    for index, units in sorted(rounds.items()):
        marginal.append({"iteration_round": index, "new_scored_units": units - previous})
        previous = units
    exceeds = required is not None and remaining_page_budget is not None and required > remaining_page_budget
    return {"total_pages": pages, "user_source_pages": len(user_pages),
        "direct_user_evidence_units": len(direct), "eligible_scoring_units": eligible,
        "scored_evidence_units": scored, "scored_conversion_rate": scored_rate,
        "eligible_conversion_rate": rate, "estimated_additional_pages_required": required,
        "target_units": target_units, "remaining_page_budget": remaining_page_budget,
        "marginal_yield": marginal,
        "machine_bound_exhaustion_forecast": "estimated_budget_shortfall" if exceeds else
            "zero_observed_yield" if pages and not scored else "insufficient_budget_data" if remaining_page_budget is None else "within_estimated_budget",
        "termination_authorized": False, "scoring_authorized": False,
        "note": "Observed-rate estimate only. Existing independent exhaustion audit remains mandatory."}
