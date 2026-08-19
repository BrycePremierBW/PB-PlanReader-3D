"""v1.2.23 accuracy-precedence guards for Autopilot."""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Tuple

import pb_autopilot_v1223 as autopilot


def cross_page_calibration(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    """Use same-scale sibling sheets as donors without overwriting trusted scale.

    Eligible target:
      * no scale, or
      * v1.2.19 provisional printed-scale fallback.

    Ineligible target:
      * manual/existing calibration,
      * local dimension-line calibration,
      * previously cross-verified calibration.

    Donors must themselves be stronger than a provisional printed-scale fallback.
    """
    pages = autopilot._selected_pages(app, int(workspace_id))
    meta: Dict[int, Tuple[str, Tuple[int, int]]] = {
        int(page["id"]): (autopilot._printed_scale(page), autopilot._image_size(page)) for page in pages
    }
    donors: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for page in pages:
        scale, size = meta[int(page["id"])]
        scale_text = str(page.get("scale_text") or "")
        if (
            scale and autopilot._num(page.get("px_per_m")) > 0 and size != (0, 0)
            and not scale_text.lower().startswith("auto provisional printed scale")
        ):
            donors.setdefault((int(page.get("document_id") or 0), scale), []).append(page)

    updates: List[Dict[str, Any]] = []
    conn = app.local_connect()
    try:
        for page in pages:
            current = autopilot._num(page.get("px_per_m"))
            current_label = str(page.get("scale_text") or "")
            provisional_printed = current_label.lower().startswith("auto provisional printed scale")
            if current > 0 and not provisional_printed:
                continue
            scale, size = meta[int(page["id"])]
            if not scale or size == (0, 0):
                continue
            candidates = []
            donor_ids = []
            for donor in donors.get((int(page.get("document_id") or 0), scale), []):
                if int(donor["id"]) == int(page["id"]):
                    continue
                dsize = meta[int(donor["id"])][1]
                if not dsize[0] or not dsize[1]:
                    continue
                if abs(dsize[0] - size[0]) / max(size[0], 1) > 0.025:
                    continue
                if abs(dsize[1] - size[1]) / max(size[1], 1) > 0.025:
                    continue
                value = autopilot._num(donor.get("px_per_m"))
                if value > 0:
                    candidates.append(value)
                    donor_ids.append(int(donor["id"]))
            if not candidates:
                continue
            median = float(statistics.median(candidates))
            spread = (max(candidates) - min(candidates)) / median if len(candidates) > 1 and median else 0.0
            if len(candidates) > 1 and spread > 0.05:
                continue
            confidence = "Cross-verified" if len(candidates) >= 2 else "Provisional cross-page"
            label = f"Auto cross-page {scale} · {confidence} · donors {','.join(map(str, donor_ids))}"
            conn.execute("UPDATE pages SET px_per_m=?,scale_text=? WHERE id=?", (median, label, int(page["id"])))
            updates.append({
                "page_id": int(page["id"]), "px_per_m": median, "method": "Cross-page scale",
                "confidence": confidence, "scale": scale, "donor_page_ids": donor_ids,
                "replaced_provisional_printed_scale": bool(provisional_printed),
            })
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
    return updates


def build_autopilot_model(app: Any, workspace_id: int, report: Dict[str, Any], state: Dict[str, Any], refs):
    """Protect every measured/verified estimator mass, including NULL source refs."""
    measured = app.lquery(
        """SELECT id,label,source_reference FROM model_masses WHERE workspace_id=?
           AND LOWER(COALESCE(confidence,'')) IN ('measured','verified')
           AND COALESCE(source_reference,'') NOT LIKE ?
           AND COALESCE(source_reference,'') NOT LIKE ?""",
        (int(workspace_id), autopilot.MODEL_SOURCE_PREFIX + "%", autopilot.auto.MODEL_SOURCE_PREFIX + "%"),
    )
    if measured:
        levels = autopilot.detect_levels(autopilot._selected_pages(app, int(workspace_id)))
        return {
            "created": 0,
            "reason": "Measured/verified estimator model preserved",
            "protected_mass_ids": [int(row["id"]) for row in measured],
            "levels": levels,
        }
    return _base_model(app, int(workspace_id), report, state, refs)


_base_model = autopilot.build_autopilot_model


def apply(app: Any) -> None:
    if getattr(app, "_pb_autopilot_accuracy_guard_v1223_applied", False):
        return
    app._pb_autopilot_accuracy_guard_v1223_applied = True
    autopilot.cross_page_calibration = cross_page_calibration
    autopilot.build_autopilot_model = build_autopilot_model
    app.autopilot_cross_page_calibration = lambda workspace_id: cross_page_calibration(app, int(workspace_id))
