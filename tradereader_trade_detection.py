from __future__ import annotations

"""Conservative deterministic trade detection for processed drawing sets.

Detection suggests which estimators to run. It never creates quantities.
"""

from collections import defaultdict
import re
from typing import Any, Dict, Iterable, List

from tradereader_profiles import TRADE_OPTIONS

TRADE_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "Electrical": (
        "electrical", "lighting plan", "luminaire", "gpo", "general power outlet",
        "switchboard", "distribution board", "single line diagram", "emergency lighting",
        "exit sign", "cable tray", "conduit",
    ),
    "Plumbing": (
        "hydraulic", "plumbing", "sanitary", "cold water", "hot water", "stormwater",
        "sewer", "floor waste", "fixture schedule", "backflow", "drainage",
    ),
    "HVAC / Mechanical": (
        "mechanical", "hvac", "air conditioning", "duct", "diffuser", "grille", "ahu",
        "fcu", "fan coil", "condenser", "ventilation", "exhaust fan", "bms",
    ),
    "Carpentry / Joinery": (
        "joinery", "carpentry", "door schedule", "hardware schedule", "skirting",
        "architrave", "cabinet", "timber framing", "wall framing", "roof framing", "battens",
    ),
    "Plastering / Linings": (
        "plasterboard", "gyprock", "wall type schedule", "reflected ceiling plan", "rcp",
        "cornice", "bulkhead", "fibre cement", "lining", "level 5 finish",
        "acoustic wall", "fire rated wall",
    ),
    "Tiling": (
        "tiling", "tile finish", "tile schedule", "wall tile", "floor tile", "splashback",
        "waterproofing", "tile skirting", "movement joint",
    ),
    "Flooring": (
        "floor finish", "flooring", "carpet", "vinyl", "resilient flooring", "laminate",
        "timber flooring", "floor finish schedule", "coving",
    ),
    "Roofing": (
        "roof plan", "roofing", "roof sheet", "roof sheeting", "ridge flashing",
        "barge flashing", "gutter", "rainhead", "sarking", "roof safety", "roof anchor",
    ),
    "Concreting": (
        "concrete", "slab", "footing", "pile cap", "formwork", "reinforcement", "reo",
        "mesh", "concrete beam", "concrete column", "thickening", "setdown",
    ),
    "Landscaping": (
        "landscape", "landscaping", "planting plan", "plant schedule", "turf", "mulch",
        "paving", "irrigation", "retaining wall", "garden bed", "landscape plan",
    ),
}

WEAK_ONLY = {"slab", "concrete", "gutter", "door schedule", "paving"}


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def detect_trades(pages: Iterable[Dict[str, Any]], min_score: int = 4) -> List[Dict[str, Any]]:
    scores: Dict[str, int] = defaultdict(int)
    evidence: Dict[str, List[str]] = defaultdict(list)
    matched_pages: Dict[str, set[str]] = defaultdict(set)
    for page in pages:
        meta = _norm(" ".join(str(page.get(k) or "") for k in ("file_name", "page_label", "page_type")))
        body = _norm(page.get("extracted_text"))
        page_ref = str(page.get("page_label") or page.get("page_no") or page.get("id") or "page")
        for trade, keywords in TRADE_KEYWORDS.items():
            page_score = 0
            strong = 0
            hits: List[str] = []
            for keyword in keywords:
                in_meta = keyword in meta
                in_body = keyword in body
                if not (in_meta or in_body):
                    continue
                page_score += 4 if in_meta else 1
                strong += 0 if keyword in WEAK_ONLY else 1
                hits.append(keyword)
            if page_score and (strong or page_score >= 4):
                scores[trade] += min(page_score, 12)
                matched_pages[trade].add(page_ref)
                for hit in hits:
                    if hit not in evidence[trade] and len(evidence[trade]) < 8:
                        evidence[trade].append(hit)
    known = set(TRADE_OPTIONS)
    result = [
        {
            "trade": trade,
            "score": int(score),
            "matched_pages": len(matched_pages[trade]),
            "evidence": evidence[trade],
        }
        for trade, score in scores.items()
        if trade in known and score >= int(min_score)
    ]
    result.sort(key=lambda row: (-row["score"], row["trade"]))
    return result
