"""v1.2.21 documented unit-area block matching.

Prevents a unit from inheriting the previous unit's m² when several unit records
are close together in extracted PDF text.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

import pb_auto_geometry_v1219 as auto
import pb_unit_floor_area_v1221 as unit


def extract_unit_area_candidates(text: Any) -> List[Dict[str, Any]]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines() if line.strip()]
    labelled = [(idx, unit._match_unit_label(line, allow_short=True)) for idx, line in enumerate(lines)]
    labelled = [(idx, label) for idx, label in labelled if label]
    out: List[Dict[str, Any]] = []
    used: set[str] = set()

    for pos, (idx, label) in enumerate(labelled):
        previous_idx = labelled[pos - 1][0] if pos > 0 else -1
        next_idx = labelled[pos + 1][0] if pos + 1 < len(labelled) else len(lines)

        # Prefer the label line and the lines that belong to this unit until the
        # next unit starts. This is how most schedules/plan annotations read.
        forward_end = min(next_idx, idx + 4)
        candidates = list(range(idx, forward_end))
        # If an unusual schedule puts area immediately before the unit label,
        # search backwards second, but never cross the previous unit label.
        backward_start = max(previous_idx + 1, idx - 3)
        candidates.extend(range(idx - 1, backward_start - 1, -1))

        area = 0.0
        source = lines[idx]
        for line_idx in candidates:
            if not (0 <= line_idx < len(lines)):
                continue
            match = auto._AREA_RE.search(lines[line_idx])
            if not match:
                continue
            value = auto._num(match.group(1))
            if 8.0 <= value <= 1000.0:
                area = value
                source = lines[line_idx]
                break

        key = str(label).lower()
        if area > 0 and key not in used:
            out.append({"label": label, "area_m2": round(area, 2), "confidence": "Documented", "source": source})
            used.add(key)
    return out


def apply(app: Any) -> None:
    if getattr(app, "_pb_unit_floor_area_textfix_v1221_applied", False):
        return
    app._pb_unit_floor_area_textfix_v1221_applied = True
    auto.extract_unit_area_candidates = extract_unit_area_candidates
    app.extract_documented_unit_areas = extract_unit_area_candidates
