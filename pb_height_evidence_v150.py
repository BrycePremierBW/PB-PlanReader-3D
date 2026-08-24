"""PlanReader v1.5.0 height evidence model, resolver and room association.

Replaces the v136 height solver with a structured HeightEvidence model and
a resolver that enforces a strict evidence precedence chain.  Does NOT create
a parallel engine — patches v136 functions so the existing production chain
(v135 registration → v136 evidence → v139 walls → v141 takeoff) flows through
the enhanced resolver.

Evidence precedence (strongest first):
  1. Explicit semantic label (CH/CLG/CEILING/FCL + value)
  2. Contextually paired RLs (FFL→FCL, Level 1→Level 2, etc.)
  3. Raw vertical dimension (1.8–12 m range)
  4. Project default (explicit Default/fallback — never Measured)

RL safety: two unrelated RL values on the same sheet do NOT become a wall/ceiling
height merely because their difference is close to 2.7–3.2 m.  RL-derived evidence
requires contextual pairing via semantic keywords (FFL, FCL, LEVEL, STOREY, etc.).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

VERSION = "1.5.0"
SETTING_KEY = "height_evidence_v150"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_HEIGHT_TYPES = (
    "floor_to_ceiling", "floor_to_floor", "ceiling_bulkhead",
    "parapet", "soffit", "facade", "generic",
)

# Valid height ranges per type (metres)
_TYPE_RANGES: Dict[str, Tuple[float, float]] = {
    "floor_to_ceiling": (1.8, 6.0),
    "floor_to_floor": (2.4, 10.0),
    "ceiling_bulkhead": (0.1, 2.0),
    "parapet": (0.3, 5.0),
    "soffit": (0.3, 5.0),
    "facade": (1.8, 60.0),
    "generic": (0.5, 60.0),
}

_PRIORITY_MAP = {
    "semantic_label": 0,   # explicit CH/CLG/FCL — strongest
    "rl_difference": 1,    # contextual RL pair
    "dimension_parse": 2,  # raw dimension
    "section": 3,          # section-derived
    "default": 99,         # fallback
}


@dataclass
class HeightEvidence:
    """Single unit of height evidence with provenance and confidence."""
    id: str = ""
    source_page_id: int = 0
    source_page_label: str = ""
    height_type: str = "generic"       # floor_to_ceiling, floor_to_floor, etc.
    raw_text: str = ""
    height_m: float = 2.7
    extraction_method: str = "default"  # semantic_label, rl_difference, dimension_parse, default
    confidence: float = 0.50
    confidence_reason: str = ""
    status: str = "Default/fallback"    # Measured, Provisional measured, Default/fallback, Review
    evidence: List[str] = field(default_factory=list)
    position: List[int] = field(default_factory=list)  # [start, end] in page text
    priority: int = 99  # lower = higher precedence; derived from extraction_method

    def __post_init__(self) -> None:
        if self.priority == 99 and self.extraction_method in _PRIORITY_MAP:
            self.priority = _PRIORITY_MAP[self.extraction_method]


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Semantic ceiling height labels
_CEIL_RE = re.compile(
    r"\b(?:CH|CLG|CEILING\s*HEIGHT|FCL|CEIL)\s*[:=]?\s*"
    r"(\d{1,5}(?:\.\d+)?)\s*(mm|m)?",
    re.IGNORECASE,
)

# Floor-to-floor / floor finish level
_FFL_RE = re.compile(
    r"\b(?:FFL|FINISHED\s*FLOOR\s*LEVEL)\s*[:=]?\s*"
    r"(-?\d{1,3}(?:\.\d{1,4})?)\s*(mm|m)?",
    re.IGNORECASE,
)

# RL / AHD (Reduced Level / Australian Height Datum)
_RL_RE = re.compile(
    r"\b(?:RL|AHD)\s*[:=]?\s*(-?\d{1,3}(?:\.\d{1,4})?)\b",
    re.IGNORECASE,
)

# Storey / level references
_LEVEL_RE = re.compile(
    r"\b(?:LEVEL|LVL|FLOOR|STOREY|STORY)\s*(\d{1,2})\b",
    re.IGNORECASE,
)

# Generic bare dimension: 3–5 digits, optional mm/m suffix
_DIM_RE = re.compile(
    r"(?<![:\d])(\d{3,5}(?:\.\d+)?)\s*(mm|m)?(?!\s*[:\d])",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Semantic RL pairing
# ---------------------------------------------------------------------------

# Contextual keywords that indicate a legitimate vertical reference
_RL_CONTEXT_KEYWORDS = (
    "ffl", "fcl", "ahd", "floor", "ceiling", "level", "storey",
    "story", "soffit", "parapet", "natural", "ground", "finished",
)

# Regex for detecting RL-context keywords (avoids false matches from
# "rl" substring inside words like "FCL")
_RL_KW_RE = re.compile(
    r"\b(?:ffl|fcl|ahd|floor|ceiling|level|storey|story|soffit|parapet|"
    r"natural|ground|finished)\b",
    re.IGNORECASE,
)


def _find_paired_rls(
    text: str,
    tolerance: float = 3.0,
    min_height: float = 1.8,
    max_height: float = 12.0,
) -> List[HeightEvidence]:
    """Find RL values that are contextually paired for vertical measurement.

    Only RLs near semantic height keywords (FFL, FCL, LEVEL, etc.) are
    considered.  Two unrelated RLs on the same sheet must NOT become a
    wall/ceiling height merely because abs(RL2 − RL1) ≈ 2.7–3.2 m.
    """
    paired_rls: List[Tuple[float, str, int]] = []
    for m in _RL_RE.finditer(text):
        rl_val = float(m.group(1))
        start = max(0, m.start() - 30)
        end = min(len(text), m.end() + 30)
        context = text[start:end]
        kw_match = _RL_KW_RE.search(context)
        if kw_match:
            paired_rls.append((rl_val, kw_match.group(0).lower(), m.start()))

    if len(paired_rls) < 2:
        return []

    results: List[HeightEvidence] = []
    seen: set[Tuple[float, float]] = set()
    for i, (a, kw_a, pos_a) in enumerate(paired_rls):
        for b, kw_b, pos_b in paired_rls[i + 1:]:
            key = (min(a, b), max(a, b))
            if key in seen:
                continue
            seen.add(key)
            diff = round(abs(b - a), 4)
            if min_height <= diff <= max_height:
                best_dist = abs(diff - tolerance)
                rls_on_page = sorted(set(round(v, 4) for v, _, _ in paired_rls))
                ev = HeightEvidence(
                    id="",  # assigned by caller
                    source_page_id=0,
                    source_page_label="",
                    height_type="floor_to_floor",
                    raw_text=f"RL {a} → RL {b} (context: {kw_a}/{kw_b})",
                    height_m=diff,
                    extraction_method="rl_difference",
                    confidence=0.95,
                    confidence_reason=f"Contextual RL pairing ({kw_a}/{kw_b})",
                    status="Measured",
                    evidence=[
                        f"RL {a} → RL {b} = {diff:.3f} m",
                        f"Semantic context: {kw_a}/{kw_b}",
                        f"Distance from 3.0 m anchor: {best_dist:.3f} m",
                    ],
                    position=[pos_a, pos_b],
                )
                results.append(ev)
    results.sort(key=lambda e: abs(e.height_m - tolerance))
    return results[:3]  # top 3 closest to 3.0 m


# ---------------------------------------------------------------------------
# Semantic height label extraction
# ---------------------------------------------------------------------------

def _extract_semantic_heights(text: str) -> List[HeightEvidence]:
    """Extract explicitly labelled heights: CH 2700, CLG 3000, FFL 0.000, etc."""
    results: List[HeightEvidence] = []

    for m in _CEIL_RE.finditer(text):
        raw = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "mm" or (not unit and raw >= 100):
            val = raw / 1000.0
        elif unit == "m":
            val = raw
        elif not unit and 1.8 <= raw <= 12.0:
            val = raw  # bare value in metres (e.g. FCL 2.7)
        else:
            continue
        rng = _TYPE_RANGES["floor_to_ceiling"]
        if rng[0] <= val <= rng[1]:
            results.append(HeightEvidence(
                id="", source_page_id=0, source_page_label="",
                height_type="floor_to_ceiling",
                raw_text=m.group(0).strip(),
                height_m=round(val, 4),
                extraction_method="semantic_label",
                confidence=0.95,
                confidence_reason="Explicit ceiling height label",
                status="Measured",
                evidence=[f"Ceiling height: {m.group(0).strip()} = {val:.3f} m"],
                position=[m.start(), m.end()],
            ))

    for m in _FFL_RE.finditer(text):
        raw = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "m":
            val = raw
        elif unit == "mm" or not unit:
            val = raw / 1000.0 if abs(raw) >= 10 else raw
        else:
            continue
        results.append(HeightEvidence(
            id="", source_page_id=0, source_page_label="",
            height_type="floor_to_floor",
            raw_text=m.group(0).strip(),
            height_m=round(val, 4),
            extraction_method="semantic_label",
            confidence=0.85,
            confidence_reason="Floor level reference (RL value, not a storey height directly)",
            status="Measured",
            evidence=[f"Floor level: {m.group(0).strip()} = {val:.3f} m"],
            position=[m.start(), m.end()],
        ))

    for m in _LEVEL_RE.finditer(text):
        level_num = int(m.group(1))
        if 1 <= level_num <= 99:
            start = max(0, m.start() - 10)
            end = min(len(text), m.end() + 30)
            context = text[start:end]
            dim_match = _DIM_RE.search(context)
            if dim_match:
                raw = float(dim_match.group(1))
                u = (dim_match.group(2) or "").lower()
                if u == "mm" or (not u and raw >= 100):
                    val = raw / 1000.0
                elif u == "m":
                    val = raw
                else:
                    continue
                if 1.8 <= val <= 12.0:
                    results.append(HeightEvidence(
                        id="", source_page_id=0, source_page_label="",
                        height_type="floor_to_floor",
                        raw_text=f"Level {level_num} = {val:.3f} m",
                        height_m=round(val, 4),
                        extraction_method="semantic_label",
                        confidence=0.90,
                        confidence_reason=f"Level {level_num} height annotation",
                        status="Measured",
                        evidence=[f"Level {level_num}: {val:.3f} m"],
                        position=[m.start(), m.end()],
                    ))

    return results


# ---------------------------------------------------------------------------
# Raw dimension extraction
# ---------------------------------------------------------------------------

def _extract_dimension_heights(
    text: str,
    height_type: str = "generic",
) -> List[HeightEvidence]:
    """Extract raw dimensions in the plausible height range (1.8–12 m)."""
    rng = _TYPE_RANGES.get(height_type, _TYPE_RANGES["generic"])
    min_h = max(1.8, rng[0])
    max_h = min(12.0, rng[1])
    results: List[HeightEvidence] = []
    for m in _DIM_RE.finditer(text):
        raw = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "mm" or (not unit and raw >= 100):
            val = raw / 1000.0
        elif unit == "m":
            val = raw
        else:
            continue
        if min_h <= val <= max_h:
            results.append(HeightEvidence(
                id="", source_page_id=0, source_page_label="",
                height_type=height_type,
                raw_text=m.group(0).strip(),
                height_m=round(val, 4),
                extraction_method="dimension_parse",
                confidence=0.70,
                confidence_reason="Raw dimension in plausible height range",
                status="Provisional measured",
                evidence=[f"Dimension: {m.group(0).strip()} = {val:.3f} m"],
                position=[m.start(), m.end()],
            ))
    return results


# ---------------------------------------------------------------------------
# Section height extraction
# ---------------------------------------------------------------------------

def _extract_section_heights(
    text: str,
    page_id: int = 0,
    page_label: str = "",
) -> List[HeightEvidence]:
    """Extract heights from section drawing text.

    Sections carry storey heights and vertical spans that elevations may not.
    Uses the same semantic/RL/dimension parsing but with section-specific
    confidence adjustments.
    """
    results: List[HeightEvidence] = []
    for ev in _extract_semantic_heights(text):
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        ev.confidence = max(0.80, ev.confidence - 0.05)
        results.append(ev)

    for ev in _find_paired_rls(text):
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        ev.confidence = max(0.80, ev.confidence - 0.05)
        results.append(ev)

    for ev in _extract_dimension_heights(text, "generic"):
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        ev.confidence = max(0.65, ev.confidence - 0.05)
        results.append(ev)

    return results


# ---------------------------------------------------------------------------
# All-evidence aggregator
# ---------------------------------------------------------------------------

def extract_all_height_evidence(
    text: str,
    page_id: int = 0,
    page_label: str = "",
    page_type: str = "",
) -> List[HeightEvidence]:
    """Extract all height evidence from a page's text."""
    ev_id_counter = 0
    def _next_id() -> str:
        nonlocal ev_id_counter
        ev_id_counter += 1
        return f"H{page_id:04d}_{ev_id_counter:02d}"

    results: List[HeightEvidence] = []

    for ev in _extract_semantic_heights(text):
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        results.append(ev)

    for ev in _find_paired_rls(text):
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        results.append(ev)

    for ev in _extract_dimension_heights(text):
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        results.append(ev)

    if "section" in str(page_type).lower():
        for ev in _extract_section_heights(text, page_id, page_label):
            if not any(e.raw_text == ev.raw_text and e.height_m == ev.height_m
                       for e in results):
                ev.id = _next_id()
                results.append(ev)

    results.sort(key=lambda e: (-e.confidence, e.priority))
    return results


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def resolve_height(
    evidence_list: Sequence[HeightEvidence],
    target_type: str = "floor_to_ceiling",
    allow_floor_to_floor: bool = True,
    wall_ref: str = "",
    side: str = "",
) -> Tuple[float, HeightEvidence]:
    """Resolve the best height from evidence, respecting type compatibility.

    Args:
        evidence_list: All available height evidence, sorted or unsorted.
        target_type: What height type is needed.
        allow_floor_to_floor: If False, reject floor-to-floor evidence.
        wall_ref: Optional wall reference for context logging.
        side: Optional building side for context logging.

    Returns:
        (height_m, best_evidence) — always returns a valid height.
    """
    if not evidence_list:
        return 2.7, HeightEvidence(
            id="", source_page_id=0, source_page_label="",
            height_type="generic", raw_text="project default",
            height_m=2.7, extraction_method="default",
            confidence=0.50, confidence_reason="No height evidence available",
            status="Default/fallback",
            evidence=["Project default 2.7 m — no measured evidence"],
        )

    compatible: List[HeightEvidence] = []
    for ev in evidence_list:
        if ev.height_type == "floor_to_floor" and not allow_floor_to_floor:
            continue
        if target_type in ("floor_to_ceiling", "ceiling_bulkhead"):
            if ev.height_type == "floor_to_floor":
                continue
        compatible.append(ev)

    if not compatible:
        default_ev = HeightEvidence(
            id="", source_page_id=0, source_page_label="",
            height_type="generic", raw_text="project default (no compatible evidence)",
            height_m=2.7, extraction_method="default",
            confidence=0.50,
            confidence_reason=f"No compatible evidence for {target_type}",
            status="Default/fallback",
            evidence=[f"Project default 2.7 m — no evidence compatible with {target_type}"],
        )
        return 2.7, default_ev

    compatible.sort(key=lambda e: (e.priority, -e.confidence))
    best = compatible[0]
    return round(best.height_m, 4), best


# ---------------------------------------------------------------------------
# Per-room height association
# ---------------------------------------------------------------------------

def resolve_room_heights(
    rooms: Sequence[Dict[str, Any]],
    all_evidence: Sequence[HeightEvidence],
    default_height: float = 2.7,
) -> Dict[str, Dict[str, Any]]:
    """Associate height evidence with rooms using Priority 2 room polygons.

    Each room gets the best compatible height evidence:
    - Semantic label inside the room polygon wins first
    - Paired RL evidence for the registered side
    - Raw dimension in the room area
    - Default/fallback if no evidence found
    """
    results: Dict[str, Dict[str, Any]] = {}

    for room in rooms:
        label = str(room.get("label") or "")
        room_type = _room_height_type(label)
        allow_f2f = room_type != "floor_to_ceiling"
        polygon = room.get("polygon") or []

        spatial = []
        generic = []
        for ev in all_evidence:
            if ev.position and len(ev.position) == 2:
                x, y = ev.position
                if polygon and _point_in_polygon((x, y), polygon):
                    spatial.append(ev)
                else:
                    generic.append(ev)
            else:
                generic.append(ev)

        candidates = []
        for ev in spatial:
            if ev.height_type == "floor_to_floor" and not allow_f2f:
                continue
            if room_type in ("floor_to_ceiling", "ceiling_bulkhead"):
                if ev.height_type == "floor_to_floor":
                    continue
            candidates.append(ev)

        if not candidates:
            for ev in generic:
                if ev.extraction_method == "default":
                    continue
                if ev.height_type == "floor_to_floor" and not allow_f2f:
                    continue
                if room_type in ("floor_to_ceiling", "ceiling_bulkhead"):
                    if ev.height_type == "floor_to_floor":
                        continue
                candidates.append(ev)

        if candidates:
            candidates.sort(key=lambda e: (e.priority, -e.confidence))
            best = candidates[0]
            h = round(best.height_m, 4)
            source = best.extraction_method
            status = best.status
            conf = best.confidence
            ev_id = best.id
        else:
            h = default_height
            source = "default"
            status = "Default/fallback"
            conf = 0.50
            ev_id = ""

        room_key = label or room.get("room_ref") or f"room_{len(results)}"
        results[room_key] = {
            "height_m": h,
            "height_type": room_type,
            "height_status": status,
            "height_confidence": "Verified" if conf >= 0.90 else
                                 "High" if conf >= 0.80 else
                                 "Derived" if conf >= 0.65 else "Review",
            "height_source": source,
            "height_evidence_id": ev_id,
        }

    return results


def _room_height_type(label: str) -> str:
    """Determine the expected height type for a room based on its label."""
    lbl = str(label).upper()
    if any(kw in lbl for kw in ("BED", "LIVING", "LOUNGE", "DINING", "KITCHEN",
                                 "FAMILY", "RUMPUS", "MEDIA", "STUDY", "OFFICE")):
        return "floor_to_ceiling"
    if any(kw in lbl for kw in ("WC", "TOILET", "BATH", "ENSUITE", "SHOWER",
                                 "POWDER", "LAUNDRY")):
        return "floor_to_ceiling"
    if any(kw in lbl for kw in ("CORRIDOR", "HALL", "PASSAGE", "ENTRY",
                                 "FOYER", "VESTIBULE")):
        return "floor_to_ceiling"
    if any(kw in lbl for kw in ("GARAGE", "CARPORT")):
        return "floor_to_floor"
    return "generic"


def _point_in_polygon(point: Tuple[float, float], polygon: Sequence) -> bool:
    """Ray-casting point-in-polygon test."""
    x, y = float(point[0]), float(point[1])
    pts = [(float(p[0]), float(p[1])) for p in polygon]
    n = len(pts)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def store_height_evidence(
    app: Any,
    workspace_id: int,
    records: List[HeightEvidence],
) -> None:
    """Persist height evidence to workspace settings."""
    payload = {
        "version": VERSION,
        "records": [asdict(r) for r in records],
    }
    app.set_workspace_setting(
        int(workspace_id), SETTING_KEY,
        json.dumps(payload, separators=(",", ":")),
    )


def get_height_evidence(app: Any, workspace_id: int) -> List[HeightEvidence]:
    """Retrieve stored height evidence."""
    try:
        raw = app.workspace_setting(int(workspace_id), SETTING_KEY, "{}")
        parsed = json.loads(str(raw or "{}"))
        return [HeightEvidence(**r) for r in parsed.get("records", [])]
    except Exception:
        return []


def get_default_height(app: Any, workspace_id: int) -> float:
    """Get the project default wall height."""
    try:
        return max(0.5, float(
            app.workspace_setting(int(workspace_id), "default_wall_height_m", 2.7)
        ))
    except Exception:
        return 2.7


# ---------------------------------------------------------------------------
# Startup wiring — patches v136 to use enhanced resolver
# ---------------------------------------------------------------------------

def apply(app: Any) -> None:
    if getattr(app, "_pb_height_evidence_v150_applied", False):
        return
    app._pb_height_evidence_v150_applied = True

    # Import v136 to patch its functions
    try:
        import pb_elevation_profile_v136 as v136
    except ImportError:
        return

    _original_solve = v136.solve_height_from_text

    def _enhanced_solve(text: Any, default_height: float = 2.7) -> Dict[str, Any]:
        ev_list = extract_all_height_evidence(str(text or ""))
        h, best = resolve_height(ev_list, target_type="generic")
        result = _original_solve(text, default_height)
        if best.extraction_method != "default":
            result["height_m"] = round(h, 4)
            result["height_evidence"] = asdict(best)
        return result

    v136.solve_height_from_text = _enhanced_solve

    _original_build = v136.build_profiles

    def _enhanced_buildprofiles(app_obj: Any, workspace_id: int) -> Dict[str, Any]:
        default = get_default_height(app_obj, workspace_id)
        pages = {
            int(r["id"]): dict(r)
            for r in app_obj.lquery(
                "SELECT id,extracted_text,page_label,page_type FROM pages "
                "WHERE workspace_id=?",
                (int(workspace_id),),
            )
        }
        all_evidence: List[HeightEvidence] = []
        for pid, page in pages.items():
            text = str(page.get("extracted_text") or "")
            ev_list = extract_all_height_evidence(
                text, page_id=pid,
                page_label=str(page.get("page_label") or ""),
                page_type=str(page.get("page_type") or ""),
            )
            all_evidence.extend(ev_list)

        store_height_evidence(app_obj, workspace_id, all_evidence)

        reg = (app_obj.register_elevations_v135(int(workspace_id))
               if hasattr(app_obj, "register_elevations_v135")
               else {"elevations": []})

        profiles = []
        for item in reg.get("elevations") or []:
            pid = int(item.get("page_id") or 0)
            side = str(item.get("orientation") or "")
            page = pages.get(pid, {})
            ev_list = extract_all_height_evidence(
                str(page.get("extracted_text") or ""),
                page_id=pid,
                page_label=str(page.get("page_label") or ""),
            )
            side_ev = [e for e in ev_list if e.source_page_id == pid]
            h, best = resolve_height(
                side_ev, target_type="generic",
                allow_floor_to_floor=True, side=side,
            )
            profiles.append({
                "side": side,
                "page_id": item.get("page_id"),
                "page_label": item.get("page_label"),
                "height_m": round(h, 4),
                "status": best.status,
                "confidence": "Verified" if best.confidence >= 0.90 else
                              "High" if best.confidence >= 0.80 else
                              "Derived" if best.confidence >= 0.65 else "Review",
                "rls": [],
                "dimensions": [],
                "height_evidence_id": best.id,
            })

        payload = {"version": VERSION, "profiles": profiles}
        app_obj.set_workspace_setting(
            int(workspace_id), "elevation_profiles_v136",
            json.dumps(payload, separators=(",", ":")),
        )
        return payload

    v136.build_profiles = _enhanced_buildprofiles

    # Wire public API onto app
    app.extract_height_evidence_v150 = extract_all_height_evidence
    app.resolve_height_v150 = resolve_height
    app.resolve_room_heights_v150 = resolve_room_heights
    app.get_height_evidence_v150 = lambda wid: get_height_evidence(app, int(wid))
    app.get_default_height_v150 = lambda wid: get_default_height(app, int(wid))
    app.store_height_evidence_v150 = lambda wid, records: store_height_evidence(
        app, int(wid), records,
    )
    app.extract_section_heights_v150 = _extract_section_heights
