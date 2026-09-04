"""PlanReader v1.7.5 production integration for the approved P5 opening pipeline.

Safety goals:
- Legacy v134/v137/v139/v145 may report/show openings, but an old ``deduct=True``
  default is never sufficient authority to subtract wall area.
- Explicit estimator decisions remain supported.
- Automatic subtraction requires the complete approved B5 proof bundle.
- The existing native-vector analysis command runs real PDF geometry through
  B1->B5 and persists the evidence separately. Missing corroboration remains
  review/no-deduction; errors fail closed.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pb_opening_evidence_v170 import (
    DEDUCTION_AUTO_ELIGIBLE,
    DEDUCTION_DERIVED_ELIGIBLE,
    DEDUCTION_DEDUCTED,
    DIMENSION_BASIS_ROUGH_OPENING,
)
from pb_opening_deduction_v174 import run_opening_pipeline
from pb_plan_opening_detection_v171 import Segment, TextWord
from pb_canonical_building import parse_strict_bool

VERSION = "1.7.5"
SETTING_PREFIX = "opening_evidence_v175_page_"
MIN_DEDUCTION_CONFIDENCE = 0.70


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _assigned_wall(raw: Dict[str, Any]) -> str:
    value = str(raw.get("resolved_wall_ref") or raw.get("wall_ref") or "").strip()
    if value.lower() in {"", "unassigned", "unassigned wall", "unknown", "none"}:
        return ""
    return value


def is_authorised_deduction(raw: Dict[str, Any]) -> bool:
    """True only for an explicit manual decision or a proven B5 decision."""
    raw = dict(raw or {})
    if not parse_strict_bool(raw.get("deduct", False)):
        return False
    if not _assigned_wall(raw):
        return False
    if _num(raw.get("width_m")) <= 0 or _num(raw.get("height_m")) <= 0:
        return False

    confidence_label = str(raw.get("confidence") or "").strip().lower()
    if parse_strict_bool(raw.get("manual_override_confirmed", False)) or confidence_label == "manual estimator entry":
        return True

    if not parse_strict_bool(raw.get("reconciliation_complete", False)):
        return False
    if str(raw.get("deduction_status") or "") not in {
        DEDUCTION_AUTO_ELIGIBLE,
        DEDUCTION_DERIVED_ELIGIBLE,
    }:
        return False
    if str(raw.get("deduction_decision") or "") != DEDUCTION_DEDUCTED:
        return False
    if str(raw.get("dimension_basis") or "") != DIMENSION_BASIS_ROUGH_OPENING:
        return False
    minimum = min(
        _num(raw.get("geometry_confidence")),
        _num(raw.get("dimension_confidence")),
        _num(raw.get("association_confidence")),
    )
    return minimum >= MIN_DEDUCTION_CONFIDENCE


def _safe_legacy_normaliser(original_normalise):
    def safe_normalise(raw: Dict[str, Any]) -> Dict[str, Any]:
        source = dict(raw or {})
        item = dict(original_normalise(source))
        for key in (
            "manual_override_confirmed", "opening_instance_id", "page_id", "page_no",
            "position_along_wall_m", "reconciliation_complete", "deduction_status",
            "deduction_decision", "dimension_basis", "geometry_confidence",
            "dimension_confidence", "association_confidence",
        ):
            if key in source:
                item[key] = source[key]
        proof = dict(source)
        proof.update(item)
        item["deduct"] = is_authorised_deduction(proof)
        return item
    return safe_normalise


def _safe_legacy_save(original_save, safe_normalise):
    def safe_save(app: Any, workspace_id: int, openings: Iterable[Dict[str, Any]],
                  confirm_all: bool = False,
                  confirm_ids: Optional[Iterable[Any]] = None) -> None:
        confirm = set(str(x) for x in (confirm_ids or []))
        payload: List[Dict[str, Any]] = []
        for raw in openings or []:
            row = dict(raw or {})
            # `manual_override_confirmed` means "the estimator explicitly decided
            # this physical opening", NOT "the estimator chose deduct=True", so an
            # explicit EXCLUDE (deduct=False) is preserved and a later B5 result
            # cannot re-enable an opening the estimator deliberately excluded.
            # Confirmation is ACTION-scoped: only rows the estimator explicitly
            # acted on are promoted, and every other row keeps its prior state
            # (never cleared, never blanket-promoted).
            prior = parse_strict_bool(row.get("manual_override_confirmed", False))
            explicitly_confirmed = parse_strict_bool(confirm_all) or (str(row.get("id")) in confirm)
            row["manual_override_confirmed"] = prior or explicitly_confirmed
            payload.append(safe_normalise(row))
        original_save(app, int(workspace_id), payload)
    return safe_save


def _safe_deducted_area(openings: Iterable[Dict[str, Any]]) -> float:
    total = 0.0
    for raw in openings or []:
        row = dict(raw or {})
        if is_authorised_deduction(row):
            total += _num(row.get("width_m")) * _num(row.get("height_m")) * max(1, int(_num(row.get("quantity"), 1)))
    return round(total, 4)


def _safe_net_wall_area(gross_wall_m2: float, openings: Iterable[Dict[str, Any]]) -> float:
    return round(max(0.0, _num(gross_wall_m2) - _safe_deducted_area(openings)), 4)


def _safe_v145_detect(original_detect):
    def detect(candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows = original_detect(candidates)
        result: List[Dict[str, Any]] = []
        for raw in rows or []:
            row = dict(raw)
            row["deduct"] = False
            row["deduction_status"] = "review"
            row["deduction_decision"] = "not_deducted"
            row.setdefault("dimension_basis", "unknown")
            row["reconciliation_complete"] = False
            result.append(row)
        return result
    return detect


def _safe_v145_room_summary(original_summary):
    def room_summary(rooms: Sequence[Dict[str, Any]], openings: Sequence[Dict[str, Any]]):
        safe: List[Dict[str, Any]] = []
        for raw in openings or []:
            row = dict(raw or {})
            row["deduct"] = is_authorised_deduction(row)
            safe.append(row)
        return original_summary(rooms, safe)
    return room_summary


def _safe_v145_facade_net(original_facade):
    def facade(regions: Sequence[Dict[str, Any]], openings: Sequence[Dict[str, Any]]):
        safe: List[Dict[str, Any]] = []
        for raw in openings or []:
            row = dict(raw or {})
            row["deduct"] = is_authorised_deduction(row)
            safe.append(row)
        return original_facade(regions, safe)
    return facade


def install_legacy_safety_fence(app: Any) -> None:
    """Fence module globals and, critically, already-bound ``app`` aliases."""
    if getattr(app, "_pb_opening_legacy_safety_v175", False):
        return

    try:
        import pb_opening_deductions_v134 as legacy
        original_normalise = legacy.normalise_opening
        original_save = legacy._save
        safe_normalise = _safe_legacy_normaliser(original_normalise)
        safe_save = _safe_legacy_save(original_save, safe_normalise)
        legacy.normalise_opening = safe_normalise
        legacy._save = safe_save
        legacy.deducted_area_m2 = _safe_deducted_area
        legacy.net_wall_area_m2 = _safe_net_wall_area
        app.normalise_opening = safe_normalise
        app.deducted_opening_area_m2 = _safe_deducted_area
        app.net_wall_area_m2 = _safe_net_wall_area
    except Exception:
        pass

    try:
        import pb_accuracy_v13_engines_v145 as accuracy
        original_detect = accuracy.detect_openings
        original_room = accuracy.room_quantity_summary
        original_facade = accuracy.facade_net_area
        safe_detect = _safe_v145_detect(original_detect)
        safe_room = _safe_v145_room_summary(original_room)
        safe_facade = _safe_v145_facade_net(original_facade)
        accuracy.detect_openings = safe_detect
        accuracy.room_quantity_summary = safe_room
        accuracy.facade_net_area = safe_facade
        app.detect_openings_v145 = safe_detect
        app.room_quantity_summary_v145 = safe_room
        app.facade_net_area_v145 = safe_facade
    except Exception:
        pass

    if hasattr(app, "attach_openings_v137"):
        original_attach = app.attach_openings_v137
        def safe_attach(workspace_id: int, walls: List[Dict[str, Any]]):
            attached = original_attach(workspace_id, walls)
            result = []
            for raw in attached or []:
                row = dict(raw or {})
                row["deduct"] = is_authorised_deduction(row)
                result.append(row)
            # Merge completed B5 decisions so they are the authoritative net-area
            # deductions (deduped against any legacy record for the same opening;
            # estimator manual overrides are never overridden).
            try:
                b5_rows = _b5_authoritative_instances(app, int(workspace_id))
                if b5_rows:
                    result = merge_b5_authoritative(result, b5_rows)
            except Exception:
                pass
            return result
        app.attach_openings_v137 = safe_attach
        # Dedicated marker: the authoritative B5 consumer wrapper is installed.
        app._pb_opening_consumer_attach_v175 = True

    app._pb_opening_legacy_safety_v175 = True


def _drawing_index(raw: Dict[str, Any]) -> int:
    if raw.get("drawing_index") is not None:
        return int(_num(raw.get("drawing_index"), 0))
    text = str(raw.get("id") or "")
    if text.startswith("d") and "i" in text:
        return int(_num(text[1:].split("i", 1)[0], 0))
    return 0


def _segment_from_native(raw: Dict[str, Any]) -> Segment:
    return Segment(
        x1=_num(raw.get("x1")), y1=_num(raw.get("y1")),
        x2=_num(raw.get("x2")), y2=_num(raw.get("y2")),
        layer=str(raw.get("layer") or ""), drawing_index=_drawing_index(raw),
    )


def _word_from_native(raw: Dict[str, Any], page_no: int) -> TextWord:
    bbox = list(raw.get("bbox") or [raw.get("x0"), raw.get("y0"), raw.get("x1"), raw.get("y1")])
    while len(bbox) < 4:
        bbox.append(0)
    return TextWord(
        text=str(raw.get("text") or ""), x0=_num(bbox[0]), y0=_num(bbox[1]),
        x1=_num(bbox[2]), y1=_num(bbox[3]), page_no=int(page_no),
    )


def _looks_like_door_window_schedule(page_text: str) -> bool:
    """Conservative door/window schedule page detector.

    A page is treated as a door/window schedule only when its text explicitly
    references a door/window schedule heading.  Plan pages and generic material
    schedules are not parsed as opening schedules, so the bridge never invents
    schedule evidence (the approved B2 parser still decides marks/basis from the
    actual PDF words, and generic WIDTH/HEIGHT never becomes rough-opening).
    """
    text = (" " + str(page_text or "") + " ").lower()
    if "schedule" not in text:
        return False
    return any(tok in text for tok in ("door", "window", "doors", "windows", "door/window", "door / window"))


def _page_text(pdf_page: Any) -> str:
    try:
        words = pdf_page.get_text("words") or []
        return " ".join(str(w[4]) for w in words if len(w) >= 5)
    except Exception:
        return ""


def extract_schedule_entries(pdf: Any) -> List[Any]:
    """Parse real door/window schedule rows from a document's schedule pages.

    Uses the approved B2 parser (pb_opening_schedule_v171.parse_schedule_page)
    over the genuine PDF words.  dimension_basis is derived only from explicit
    headings (e.g. "Rough Opening"), never invented.  Returns ScheduleEntry list;
    empty when no door/window schedule page is present (B2 evidence missing).
    """
    try:
        from pb_opening_schedule_v171 import parse_schedule_page
    except Exception:
        return []
    entries: List[Any] = []
    try:
        count = pdf.page_count
    except Exception:
        return []
    for idx in range(count):
        try:
            page = pdf.load_page(idx)
        except Exception:
            continue
        if not _looks_like_door_window_schedule(_page_text(page)):
            continue
        try:
            entries.extend(parse_schedule_page(page, page_no=idx + 1))
        except Exception:
            continue
    return entries


# Same approved families and digit range as the B1 classifier
# (D##/ED##/ID## doors, W##/EW##/IW## windows).  True full-match: a mark like
# ``ED01XYZ`` or ``IW05junk`` is never accepted as a family, and bare ``I``/``II``
# are not opening families.
_DOOR_MARK_RE = re.compile(r"^(?:D|ED|ID)\d{1,3}$", re.IGNORECASE)
_WINDOW_MARK_RE = re.compile(r"^(?:W|EW|IW)\d{1,3}$", re.IGNORECASE)


def _opening_category(row: Dict[str, Any]) -> str:
    """Canonical opening type ("door"|"window"|"other") for a record.

    Preference order:
      1. a genuine ``opening_type`` field (the B5 dataclass carries
         "door"/"window"/"other");
      2. the legacy ``kind``/``label``/``unit_type`` (Door/Window) that v134
         preserves (it drops the original mark);
      3. an anchored full-match classifier over the approved mark families:
         doors ``D##``/``ED##``/``ID##``, windows ``W##``/``EW##``/``IW##``.

    The classifier uses the same families and digit range as B1 and full-matches
    the whole token, so prefixes like ``ED01``/``IW05`` are read as the full
    approved family (never misread from their first character) and strings with
    trailing junk are rejected.
    """
    opening_type = str(row.get("opening_type") or "").strip().lower()
    if opening_type in ("door", "window"):
        return opening_type
    for key in ("kind", "label", "unit_type"):
        value = str(row.get(key) or "").lower()
        if "window" in value and "door" not in value:
            return "window"
        if "door" in value and "window" not in value:
            return "door"
    mark = str(row.get("type_mark") or row.get("label") or "").strip()
    if mark:
        if _WINDOW_MARK_RE.fullmatch(mark):
            return "window"
        if _DOOR_MARK_RE.fullmatch(mark):
            return "door"
    return "other"


def _mark_token(row: Dict[str, Any]) -> str:
    """The specific mark token used as an opening-identity signal (e.g. D01)."""
    value = str(row.get("type_mark") or row.get("label") or "").strip()
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _b5_instance_key(row: Dict[str, Any]) -> Tuple:
    """Physical-instance identity for a B5 opening.

    Distinct physical openings must never be collapsed, so this prioritises
    instance-level evidence: a unique ``opening_instance_id``, a plan-geometry
    signature, then page/location/position.  It intentionally does NOT use
    (wall, category, width, height) alone, which would merge two equal-sized
    openings on the same wall.
    """
    instance_id = str(row.get("opening_instance_id") or "").strip()
    signature = str(row.get("plan_geometry_signature") or "").strip()
    page = int(_num(row.get("page_no"), 0))
    position = round(_num(row.get("position_along_wall_m"), -1.0), 4)
    width = round(_num(row.get("width_m")), 3)
    height = round(_num(row.get("height_m")), 3)
    return (instance_id, signature, page, position, width, height)


def _canonical_wall(row: Dict[str, Any]) -> str:
    return str(row.get("resolved_wall_ref") or row.get("wall_ref") or "").strip().upper()


def _same_opening(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Conservative same-physical-opening test between a B5 row and a legacy row.

    Returns True only with sufficient evidence:
      - identical non-empty ``opening_instance_id``, or
      - identical specific mark token + wall + category + width + height.
    This suppresses a B5 row against a manual legacy row only for the same
    physical opening and never merges two distinct openings (prefer under-
    deduction/review over a false merge).
    """
    ia = str(a.get("opening_instance_id") or "").strip()
    ib = str(b.get("opening_instance_id") or "").strip()
    if ia and ib and ia == ib:
        return True
    ma = _mark_token(a)
    mb = _mark_token(b)
    if not ma or not mb or ma != mb:
        return False
    return (
        _canonical_wall(a) == _canonical_wall(b)
        and _opening_category(a) == _opening_category(b)
        and round(_num(a.get("width_m")), 3) == round(_num(b.get("width_m")), 3)
        and round(_num(a.get("height_m")), 3) == round(_num(b.get("height_m")), 3)
    )


def _is_authorised_b5_automatic(row: Dict[str, Any]) -> bool:
    """Full automatic-B5 proof gate, independent of any persisted ``deduct`` flag.

    Unlike ``is_authorised_deduction`` (which also honours an explicit manual
    estimator override), a persisted automatic P5 row must prove the complete
    B5 bundle itself so a stale/corrupted/incomplete payload can never reach
    the wall/net-area consumer as a deduction:
      - assigned wall;
      - positive width/height;
      - ``reconciliation_complete`` True;
      - eligible deduction status (auto/derived);
      - ``deduction_decision == "deducted"``;
      - ``dimension_basis == "rough_opening"``;
      - min confidence at the deduction floor.
    """
    raw = dict(row or {})
    if not _assigned_wall(raw):
        return False
    if _num(raw.get("width_m")) <= 0 or _num(raw.get("height_m")) <= 0:
        return False
    if not parse_strict_bool(raw.get("reconciliation_complete")):
        return False
    if str(raw.get("deduction_status") or "") not in {
        DEDUCTION_AUTO_ELIGIBLE,
        DEDUCTION_DERIVED_ELIGIBLE,
    }:
        return False
    if str(raw.get("deduction_decision") or "") != DEDUCTION_DEDUCTED:
        return False
    if str(raw.get("dimension_basis") or "") != DIMENSION_BASIS_ROUGH_OPENING:
        return False
    minimum = min(
        _num(raw.get("geometry_confidence")),
        _num(raw.get("dimension_confidence")),
        _num(raw.get("association_confidence")),
    )
    return minimum >= MIN_DEDUCTION_CONFIDENCE


PAGES_INDEX_KEY = "opening_evidence_v175_pages"


def _verify_b5_page(app: Any, workspace_id: int, page_id: int, payload: Dict[str, Any]) -> bool:
    """Fail-closed check that persisted page-scoped P5 evidence is still live.

    Returns True only when the source page can be verified to still exist in the
    current workspace's ``pages`` table AND the payload's own page/workspace
    identity agrees.  Orphaned / deleted / re-homed / mismatched evidence is
    rejected so stale persisted B5 deductions never keep reducing wall area.
    """
    query = getattr(app, "lquery", None)
    if not callable(query):
        return False  # cannot verify current page existence -> fail closed
    try:
        rows = query("SELECT id, workspace_id FROM pages WHERE id=?", (int(page_id),))
    except Exception:
        return False
    if not rows:
        return False  # page no longer exists in the live table
    live = dict(rows[0])
    if int(_num(live.get("workspace_id"), -1)) != int(workspace_id):
        return False  # page has been moved to another workspace
    pl = dict(payload or {})
    if "page_id" in pl and _num(pl.get("page_id")) != int(page_id):
        return False
    if "workspace_id" in pl and _num(pl.get("workspace_id")) != int(workspace_id):
        return False
    return True


def _row_identity_agrees(row: Dict[str, Any], workspace_id: int, page_id: int) -> bool:
    """Where a row carries its own page/workspace identity, require it to agree."""
    if "page_id" in row and _num(row.get("page_id")) != int(page_id):
        return False
    if "workspace_id" in row and _num(row.get("workspace_id")) != int(workspace_id):
        return False
    return True


def _b5_authoritative_instances(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    """Collect completed B5 (``deduct``) instances from persisted P5 payloads.

    These carry the full proof bundle with numeric confidences.  They are kept
    pristine in the page-scoped evidence setting, not rewritten through the
    legacy register, because ``attach_openings_v137`` stamps a visual-only
    ``geometry_confidence="Review"`` that would otherwise strip B5 authority.

    Each page-scoped payload is only trusted after the source page is verified
    to still exist in the current workspace (fail closed otherwise) and its own
    page/workspace identity agrees.
    """
    out: List[Dict[str, Any]] = []
    try:
        index = json.loads(
            str(app.workspace_setting(int(workspace_id), PAGES_INDEX_KEY, "[]") or "[]")
        )
    except Exception:
        index = []
    if not isinstance(index, list):
        index = []
    for page_id in index:
        try:
            payload = json.loads(str(
                app.workspace_setting(
                    int(workspace_id), f"{SETTING_PREFIX}{int(page_id)}", "{}"
                ) or "{}"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        # Reject stale/orphaned evidence: the source page must still exist in the
        # live pages table, owned by the current workspace, with agreeing identity.
        if not _verify_b5_page(app, int(workspace_id), int(page_id), payload):
            continue
        for row in (payload.get("instances") or []):
            if not _row_identity_agrees(row, int(workspace_id), int(page_id)):
                continue
            # Re-authorise: never trust a persisted ``deduct`` flag alone.  The
            # complete automatic-B5 proof must still hold at consumption time.
            if _is_authorised_b5_automatic(row):
                out.append(dict(row))
    return out


def _record_p5_page(app: Any, workspace_id: int, page_id: int) -> None:
    """Track which pages have a persisted P5 payload for this workspace."""
    try:
        index = json.loads(str(
            app.workspace_setting(int(workspace_id), PAGES_INDEX_KEY, "[]") or "[]"))
        if not isinstance(index, list):
            index = []
        if int(page_id) not in [int(x) for x in index]:
            index.append(int(page_id))
        app.set_workspace_setting(
            int(workspace_id), PAGES_INDEX_KEY,
            json.dumps(index, separators=(",", ":")),
        )
    except Exception:
        pass


def merge_b5_authoritative(
    attached: Sequence[Dict[str, Any]], b5_rows: Iterable[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Merge re-authorised B5 decisions into the net-area consumer's list.

    Reconciliation rules (distinct physical openings preserved, estimator
    intent honoured, no double-deduction):
      - Every B5 row is re-verified with ``_is_authorised_b5_automatic``; a
        persisted ``deduct`` flag alone is never trusted at the consumer.
      - B5-to-B5 uniqueness uses physical-instance identity
        (``opening_instance_id`` / ``plan_geometry_signature`` / position), so
        two distinct equal-sized openings on the same wall each deduct once.
      - Legacy records are NOT aggressively removed: an unproven legacy record
        already carries ``deduct=False``, so it cannot double-deduct.
      - A B5 row is only suppressed against a legacy ``manual_override_confirmed``
        record when ``_same_opening`` shows strong evidence they are the same
        physical opening.
      - B5 rows carry their own numeric confidences (unlike ``attach_openings``
        which stamps a visual-only ``geometry_confidence="Review"``), so they
        remain deducible.
    """
    b5_list = [dict(r) for r in (b5_rows or []) if _is_authorised_b5_automatic(r)]
    result: List[Dict[str, Any]] = [dict(r) for r in (attached or [])]
    if not b5_list:
        return result
    # Preserve explicit estimator decisions: only suppress a B5 row against a
    # legacy manual record that is the same physical opening (strong evidence).
    manual_rows = [r for r in result if parse_strict_bool(r.get("manual_override_confirmed"))]
    added = set()
    for row in b5_list:
        if any(_same_opening(row, m) for m in manual_rows):
            continue  # estimator explicitly decided this opening
        inst_key = _b5_instance_key(row)
        if inst_key in added:
            continue  # same physical instance already merged
        added.add(inst_key)
        rec = dict(row)
        rec.setdefault("resolved_wall_ref", rec.get("wall_ref") or "")
        rec.setdefault("area_m2", round(
            _num(rec.get("width_m")) * _num(rec.get("height_m"))
            * max(1, int(_num(rec.get("quantity"), 1))), 4))
        rec["deduct"] = True
        result.append(rec)
    return result


def run_p5_native_payload(
    native: Dict[str, Any], *, page_no: int, page_id: int = 0,
    workspace_id: int = 0, scale_info: Dict[str, Any] | None = None,
    schedule_entries: Optional[Sequence[Any]] = None,
    elevation_openings: Optional[Sequence[Any]] = None,
    elevation_diagnostics: Optional[Sequence[Dict[str, Any]]] = None,
    elevation_provenance: Optional[Sequence[Dict[str, Any]]] = None,
    facade_registration: Any = None,
) -> Dict[str, Any]:
    """Run the native payload through B1->B5 with optional elevation evidence.

    Phase 2B controlled seam: ``elevation_openings`` may carry a
    provenance-complete, dimensional list of ``ElevationOpening`` objects
    produced by the fail-closed v1.7.8 production bridge.  They are correlated
    against the B1/B2 instances with the PRODUCTION strict B3 stage
    (``correlate_elevation_to_plan_production`` in the v1.7.8 bridge), which
    requires a GENUINELY VALIDATED instance-specific identity anchor (exact
    opening mark, or validated position backed by a registered facade
    ``wall_ref``) — generic "same elevation side + compatible width" and
    arbitrary caller-attached identifiers/positions are never sufficient.

    ``elevation_provenance`` is index-aligned with ``elevation_openings``: it
    carries the SAME resolved provenance (original source filename, page,
    drawing ref/title, coordinate space, calibration source+state, elevation
    side, level) as the matching accepted opening, so reviewers can trace each
    persisted opening back to its source.  It is persisted additively as
    ``elevation_provenance`` (never overwriting existing fields).

    ``facade_registration`` (optional, shape of
    ``pb_elevation_registration_v135.footprint_facades``) supplies the
    drawing-derived facade coregistration used to validate GENUINE position
    anchors: a validated-position anchor is only accepted when the elevation
    opening's ``wall_ref`` is registered on its side AND the plan station lies
    within that registered segment's extent.  When omitted, no position anchor
    is genuine (positions fail closed), so only exact-mark anchors can
    correlate — corroboration never relies on arbitrary caller-attached data.

    Because the strict B3 stage is owned by the production seam (and must not
    reuse v1.7.2's permissive side+width identity), elevation openings are NOT
    threaded into ``run_opening_pipeline``'s shared weak B3 stage; instead the
    pipeline runs B1->B2 (and B4/B5) and production applies the strict
    elevation correlation afterwards.  This ordering is SAFE for all safety
    guarantees: elevation is CORROBORATION ONLY — it never creates instances,
    never sets ``deduct=True``, and always keeps ``dimension_basis="unknown"``
    — so running it after B4/B5 cannot change any deduction outcome.

    Safety guarantees preserved:
      - Default is ``None``/empty -> identical to previous behaviour (no B3
        stage, no elevation influence).
      - Elevation evidence is CORROBORATION ONLY: B3 never creates instances
        and never sets ``deduct=True``; generic elevation observations keep
        ``dimension_basis="unknown"`` so the B5 rough-opening gate still
        rejects them.
      - Outcomes are persisted additively in ``elevation_openings``,
        ``elevation_diagnostics`` and ``elevation_provenance`` (never
        overwriting existing fields), so a reviewer can see WHY every candidate
        was accepted/rejected, how it correlated, and trace it to its source.
    """
    segments = [_segment_from_native(row) for row in native.get("segments") or []]
    words = [_word_from_native(row, page_no) for row in native.get("words") or []]
    # Elevation is NOT passed to the shared weak pipeline B3 stage (v1.7.2
    # treats side+width as sufficient identity).  Production correlation uses
    # the strict gated stage below instead.
    pipeline = run_opening_pipeline(
        segments=segments, words=words,
        schedule_entries=schedule_entries, elevation_openings=None,
        scale_info=scale_info or {}, page_no=int(page_no),
    )
    pipeline_instances = list(pipeline.get("instances") or [])

    elevation_openings_payload = [asdict(open) for open in (elevation_openings or [])]
    elevation_diagnostics_payload = list(elevation_diagnostics or [])
    # index-aligned provenance for each persisted opening (R1): same resolution
    # as the matching accepted diagnostic, so persisting can never disagree.
    elevation_provenance_payload = [dict(p) for p in (elevation_provenance or [])]

    # --- Production strict B3 correlation (C2) ------------------------------
    matched_openings = 0
    unmatched_openings = len(elevation_openings or [])
    if (elevation_openings or []) and pipeline_instances:
        try:
            from pb_elevation_production_bridge_v178 import (
                correlate_elevation_to_plan_production,
            )
            correlated, unmatched = correlate_elevation_to_plan_production(
                list(elevation_openings), pipeline_instances,
                facades=facade_registration,
            )
            pipeline_instances = list(correlated)
            matched_openings = len(elevation_openings) - len(unmatched)
            unmatched_openings = len(unmatched)
        except Exception:
            # If the strict seam is unavailable, elevation never influences the
            # result (fail closed to the no-elevation path).
            matched_openings = 0
            unmatched_openings = len(elevation_openings or [])
    if (elevation_openings or []):
        elevation_diagnostics_payload.append({
            "kind": "b3_correlation",
            "note": (
                f"B3 strict production correlation: {matched_openings} correlated, "
                f"{unmatched_openings} unmatched/review"
            ),
            "source": "pb_elevation_production_bridge_v178.correlate_elevation_to_plan_production",
            "identity_rule": (
                "requires GENUINELY validated anchor (exact mark / validated "
                "position backed by registered wall_ref); side+width alone or "
                "attached identity signals never identify"
            ),
            "matched_count": matched_openings,
            "unmatched_count": unmatched_openings,
        })

    for inst in pipeline_instances:
        inst.workspace_id = int(workspace_id)
        inst.page_id = int(page_id) if page_id else None
    instances = [asdict(inst) for inst in pipeline_instances]
    conflicts = [asdict(conflict) for conflict in pipeline.get("conflicts") or []]
    return {
        "version": VERSION, "workspace_id": int(workspace_id), "page_id": int(page_id),
        "page_no": int(page_no), "instances": instances, "conflicts": conflicts,
        "deducted_area_m2": float(pipeline.get("deducted_area_m2") or 0.0),
        "pipeline_notes": list(pipeline.get("pipeline_notes") or []),
        "candidate_count": len(instances),
        "deducted_count": sum(1 for row in instances if parse_strict_bool(row.get("deduct"))),
        "review_count": sum(1 for row in instances if str(row.get("deduction_status")) == "review"),
        "elevation_openings": elevation_openings_payload,
        "elevation_diagnostics": elevation_diagnostics_payload,
        "elevation_provenance": elevation_provenance_payload,
        "status": "ok", "error": "",
    }


def analyse_stored_page_openings(
    app: Any, page_id: int, vector_result: Dict[str, Any], *,
    elevation_openings: Optional[Sequence[Any]] = None,
    elevation_diagnostics: Optional[Sequence[Dict[str, Any]]] = None,
    elevation_provenance: Optional[Sequence[Dict[str, Any]]] = None,
    facade_registration: Any = None,
) -> Dict[str, Any]:
    """Analyse a stored page via the P5 native-vector path.

    ``elevation_openings`` / ``elevation_diagnostics`` are the Phase 2B
    controlled-elevation seam: a provenance-complete list of ElevationOpening
    objects (plus diagnostics) is threaded into ``run_p5_native_payload`` and
    from there into ``run_opening_pipeline(elevation_openings=...)``.  Defaults
    keep pages without elevation evidence a no-op.  ``elevation_provenance`` is
    the index-aligned resolved provenance for each opening.
    ``facade_registration`` is the footprint-derived facade coregistration used
    to validate genuine position anchors (see ``run_p5_native_payload``).
    """
    rows = app.lquery(
        "SELECT p.*,d.path FROM pages p JOIN documents d ON d.id=p.document_id WHERE p.id=?",
        (int(page_id),),
    )
    if not rows:
        raise ValueError("Page not found for P5 opening analysis")
    row = dict(rows[0])
    path = Path(str(row.get("path") or ""))
    if getattr(app, "fitz", None) is None or not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("P5 opening analysis requires the original PDF file")
    page_no = max(1, int(_num(row.get("page_no"), 1)))
    render_zoom = max(0.01, _num(row.get("render_zoom"), 1.0))
    px_per_m = _num((vector_result.get("scale") or {}).get("px_per_m"), _num(row.get("px_per_m"), 0.0))
    pdf = app.fitz.open(path)
    try:
        schedule_entries = extract_schedule_entries(pdf)
        native = app.extract_native_page_v130(pdf.load_page(page_no - 1))
    finally:
        pdf.close()
    return run_p5_native_payload(
        native, page_no=page_no, page_id=int(page_id),
        workspace_id=int(row.get("workspace_id") or 0),
        scale_info={"px_per_m": px_per_m, "render_zoom": render_zoom},
        schedule_entries=schedule_entries,
        elevation_openings=elevation_openings,
        elevation_diagnostics=elevation_diagnostics,
        elevation_provenance=elevation_provenance,
        facade_registration=facade_registration,
    )


def _persist_p5_result(app: Any, workspace_id: int, page_id: int, payload: Dict[str, Any]) -> None:
    app.set_workspace_setting(
        int(workspace_id), f"{SETTING_PREFIX}{int(page_id)}",
        json.dumps(payload, separators=(",", ":")),
    )


def install_native_vector_bridge(app: Any) -> None:
    if getattr(app, "_pb_opening_native_bridge_v175", False):
        return
    if not hasattr(app, "analyse_stored_page_v130"):
        raise RuntimeError("P5 production bridge requires analyse_stored_page_v130")
    base_analyse = app.analyse_stored_page_v130

    def analyse_with_openings(page_id: int):
        result = dict(base_analyse(int(page_id)) or {})
        rows = app.lquery("SELECT workspace_id FROM pages WHERE id=?", (int(page_id),))
        workspace_id = int(dict(rows[0]).get("workspace_id") or 0) if rows else 0
        try:
            payload = analyse_stored_page_openings(app, int(page_id), result)
        except Exception as exc:
            payload = {
                "version": VERSION, "workspace_id": workspace_id, "page_id": int(page_id),
                "instances": [], "conflicts": [], "deducted_area_m2": 0.0,
                "candidate_count": 0, "deducted_count": 0, "review_count": 0,
                "pipeline_notes": ["P5 opening analysis failed closed"],
                "status": "error", "error": str(exc),
            }
        if workspace_id:
            _persist_p5_result(app, workspace_id, int(page_id), payload)
            # Completed B5 decisions are consumed by the net-area path at attach
            # time (see safe_attach / merge_b5_authoritative); track the page so
            # the consumer can find this workspace's B5-authoritative instances.
            _record_p5_page(app, workspace_id, int(page_id))
        result["p5_openings"] = {
            "version": VERSION, "status": payload.get("status"),
            "candidate_count": int(payload.get("candidate_count") or 0),
            "deducted_count": int(payload.get("deducted_count") or 0),
            "review_count": int(payload.get("review_count") or 0),
            "deducted_area_m2": float(payload.get("deducted_area_m2") or 0.0),
            "error": str(payload.get("error") or ""),
        }
        return result

    app.analyse_stored_page_v130 = analyse_with_openings
    app.run_p5_opening_native_payload_v175 = run_p5_native_payload
    app.is_authorised_opening_deduction_v175 = is_authorised_deduction
    app.merge_p5_authoritative_v175 = merge_b5_authoritative
    app.extract_p5_schedule_entries_v175 = extract_schedule_entries
    # Phase 2B: expose the fail-closed elevation-evidence production bridge
    # (additive; existing guard-required bindings are untouched).
    try:
        from pb_elevation_production_bridge_v178 import (
            produce_elevation_openings,
            raster_openings_from_candidates,
            vector_openings_from_candidates,
        )
        app.extract_p5_elevation_openings_v178 = produce_elevation_openings
        app.raster_to_p5_elevation_openings_v178 = raster_openings_from_candidates
        app.vector_to_p5_elevation_openings_v178 = vector_openings_from_candidates
    except Exception:
        # Elevation evidence is an optional corroboration source; the core
        # production integration must still install without it.
        pass
    app._pb_opening_native_bridge_v175 = True


def apply(app: Any) -> None:
    if getattr(app, "_pb_opening_production_v175_applied", False):
        return
    install_legacy_safety_fence(app)
    install_native_vector_bridge(app)
    app._pb_opening_production_v175_applied = True
