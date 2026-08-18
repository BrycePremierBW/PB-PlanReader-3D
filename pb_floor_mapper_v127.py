"""PB PlanReader v1.2.7 floor-area/calibration migration.

Adds the drag-to-calibrate + drag/resize floor-area workflow that previously
lived in the JobHub-side PlanReader prototype. The production PlanReader keeps
its existing data model: generated measurements are stored as
``row_role='floor_area'`` reference rows so they can drive floor-m² pricing but
never become painted area, litres or labour by themselves.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List

try:
    from PIL import Image
except Exception:  # pragma: no cover - production requirements include Pillow
    Image = None

try:
    from planreader_floor_mapper import floor_mapper_editor
except Exception:  # pragma: no cover - UI degrades cleanly if component unavailable
    floor_mapper_editor = None

SOURCE_PREFIX = "PB floor mapper v1.2.7"
SETTING_PREFIX = "floor_mapper_v127_page_"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def calibration_px_per_m(calibration: Dict[str, Any] | None, width_px: float, height_px: float) -> float:
    """Convert percentage calibration coordinates + known metres into px/metre."""
    if not calibration:
        return 0.0
    metres = _num(calibration.get("len_m"))
    if metres <= 0 or width_px <= 0 or height_px <= 0:
        return 0.0
    dx = (_num(calibration.get("x2")) - _num(calibration.get("x1"))) / 100.0 * width_px
    dy = (_num(calibration.get("y2")) - _num(calibration.get("y1"))) / 100.0 * height_px
    pixels = math.hypot(dx, dy)
    return pixels / metres if pixels > 1 else 0.0


def measured_box_area_m2(box: Dict[str, Any], width_px: float, height_px: float, px_per_m: float) -> float:
    """Return a floor box area, preferring an explicit manual override."""
    manual = _num(box.get("manual_m2"))
    if manual > 0:
        return round(manual, 2)
    if px_per_m <= 0 or width_px <= 0 or height_px <= 0:
        return 0.0
    width_m = (_num(box.get("w")) / 100.0 * width_px) / px_per_m
    height_m = (_num(box.get("h")) / 100.0 * height_px) / px_per_m
    return round(max(0.0, width_m * height_m), 2)


def build_floor_area_rows(
    boxes: Iterable[Dict[str, Any]],
    width_px: float,
    height_px: float,
    px_per_m: float,
    page_label: str,
    page_id: int,
) -> List[Dict[str, Any]]:
    """Build unpriced floor-area reference rows from mapper boxes."""
    rows: List[Dict[str, Any]] = []
    for index, box in enumerate(boxes or [], start=1):
        area = measured_box_area_m2(box, width_px, height_px, px_per_m)
        label = str(box.get("label") or "").strip() or f"{page_label or 'Floor plan'} · area {index}"
        box_id = str(box.get("id") or f"area-{index}")
        rows.append(
            {
                "section": "Internal",
                "element": "Floor area",
                "location": label,
                "substrate": "",
                "finish_system": "",
                "quantity": area,
                "unit": "m²",
                "quantity_status": "Measured" if area > 0 else "To measure",
                "source_page": page_label,
                "source_reference": f"{SOURCE_PREFIX} · page:{int(page_id)} · box:{box_id}",
                "inclusion_status": "INCLUSION",
                "coats": 0,
                "coverage_m2_per_litre": 0,
                "productivity_m2_per_hour": 0,
                "rate_per_unit": 0,
                "confidence": "Measured" if area > 0 and px_per_m > 0 else ("Derived" if area > 0 else "To review"),
                "notes": "Internal floor-area pricing basis only; not a painted surface quantity.",
                "row_role": "floor_area",
            }
        )
    return rows


def _state_key(page_id: int) -> str:
    return f"{SETTING_PREFIX}{int(page_id)}"


def _load_saved_state(app: Any, workspace_id: int, page_id: int) -> Dict[str, Any]:
    raw = app.workspace_setting(workspace_id, _state_key(page_id), "{}")
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _image_size(path: Path) -> tuple[float, float]:
    if Image is None or not path.exists():
        return 0.0, 0.0
    try:
        with Image.open(path) as img:
            return float(img.width), float(img.height)
    except Exception:
        return 0.0, 0.0


def _synthetic_calibration(existing_px_per_m: float, width_px: float) -> Dict[str, float] | None:
    """Represent an existing page scale as a visible 50%-width reference line."""
    if existing_px_per_m <= 0 or width_px <= 0:
        return None
    pixels = width_px * 0.5
    return {"x1": 10.0, "y1": 8.0, "x2": 60.0, "y2": 8.0, "len_m": round(pixels / existing_px_per_m, 4)}


def _replace_generated_rows(app: Any, workspace_id: int, page_id: int, rows: List[Dict[str, Any]]) -> None:
    prefix = f"{SOURCE_PREFIX} · page:{int(page_id)} ·"
    app.lexecute(
        "DELETE FROM takeoff_rows WHERE workspace_id=? AND source_reference LIKE ?",
        (workspace_id, prefix + "%"),
    )
    sql = """INSERT INTO takeoff_rows(
        workspace_id,section,element,location,substrate,finish_system,quantity,unit,
        quantity_status,source_page,source_reference,inclusion_status,coats,
        coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,
        notes,row_role,created_at,updated_at
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    for row in rows:
        app.lexecute(
            sql,
            (
                workspace_id,
                row["section"], row["element"], row["location"], row["substrate"],
                row["finish_system"], row["quantity"], row["unit"], row["quantity_status"],
                row["source_page"], row["source_reference"], row["inclusion_status"],
                row["coats"], row["coverage_m2_per_litre"], row["productivity_m2_per_hour"],
                row["rate_per_unit"], row["confidence"], row["notes"], row["row_role"],
                app.now_stamp(), app.now_stamp(),
            ),
        )


def floor_mapper_panel(app: Any, workspace: Dict[str, Any]) -> None:
    """Render and persist the fast floor-m² workflow above the legacy mapper."""
    workspace_id = int(workspace["id"])
    app.st.markdown("### Fast floor m² mapper")
    app.st.caption(
        "Drag a calibration line across a known dimension, enter its real length, then add/drag/resize floor-area boxes. "
        "Saved areas become floor-area reference rows only — no paint litres or labour are invented."
    )
    pages = app.ldf(
        "SELECT id,page_label,page_type,image_path,px_per_m,width_px,height_px FROM pages "
        "WHERE workspace_id=? AND selected=1 ORDER BY id",
        (workspace_id,),
    )
    if pages.empty:
        app.st.info("Process and select drawing pages first.")
        return
    labels = [f"#{int(r.id)} · {r.page_label} · {r.page_type}" for r in pages.itertuples()]
    chosen = app.st.selectbox("Floor drawing page", labels, key=f"v127_floor_page_{workspace_id}")
    page = pages.iloc[labels.index(chosen)].to_dict()
    image_path = Path(str(page.get("image_path") or ""))
    if not image_path.exists():
        app.st.error("Rendered drawing image is missing for this page.")
        return

    width_px = _num(page.get("width_px"))
    height_px = _num(page.get("height_px"))
    if width_px <= 0 or height_px <= 0:
        width_px, height_px = _image_size(image_path)

    saved = _load_saved_state(app, workspace_id, int(page["id"]))
    session_key = f"v127_floor_state_{workspace_id}_{int(page['id'])}"
    if session_key not in app.st.session_state:
        calibration = saved.get("calibration")
        if not calibration:
            calibration = _synthetic_calibration(_num(page.get("px_per_m")), width_px)
        app.st.session_state[session_key] = {
            "boxes": list(saved.get("boxes") or []),
            "calibration": calibration,
        }

    current = app.st.session_state[session_key]
    if floor_mapper_editor is None:
        app.st.error("The fast floor mapper component is unavailable. The existing Plan Mapper remains available below.")
        return

    result = floor_mapper_editor(
        image_path.read_bytes(),
        boxes=current.get("boxes") or [],
        calibration=current.get("calibration"),
        revision=int(app.st.session_state.get(f"v127_floor_rev_{page['id']}", 0)),
        key=f"v127_floor_widget_{page['id']}",
        height=860,
    )
    if isinstance(result, dict):
        app.st.session_state[session_key] = {
            "boxes": list(result.get("boxes") or []),
            "calibration": result.get("calibration"),
        }
        current = app.st.session_state[session_key]

    px_per_m = calibration_px_per_m(current.get("calibration"), width_px, height_px)
    rows = build_floor_area_rows(
        current.get("boxes") or [], width_px, height_px, px_per_m,
        str(page.get("page_label") or ""), int(page["id"]),
    )
    c1, c2, c3 = app.st.columns(3)
    c1.metric("Calibrated scale", f"{px_per_m:,.2f} px/m" if px_per_m > 0 else "Not set")
    c2.metric("Floor areas", str(len(rows)))
    c3.metric("Floor m²", f"{sum(_num(r['quantity']) for r in rows):,.2f}")
    if rows:
        app.st.dataframe(
            app.pd.DataFrame([{"Area": r["location"], "Floor m²": r["quantity"], "Status": r["quantity_status"]} for r in rows]),
            use_container_width=True,
            hide_index=True,
        )

    if app.st.button("Save calibration & floor areas to take-off", type="primary", key=f"v127_floor_save_{page['id']}"):
        if px_per_m <= 0 and any(_num(r["quantity"]) <= 0 for r in rows):
            app.st.error("Calibrate the drawing scale first, or enter a manual m² override for each area.")
            return
        app.set_workspace_setting(workspace_id, _state_key(int(page["id"])), json.dumps(current, separators=(",", ":")))
        if px_per_m > 0:
            app.lexecute("UPDATE pages SET px_per_m=? WHERE id=?", (px_per_m, int(page["id"])))
        _replace_generated_rows(app, workspace_id, int(page["id"]), rows)
        app.st.success(f"Saved {len(rows)} floor-area row(s) totalling {sum(_num(r['quantity']) for r in rows):,.2f} m².")
        app.st.rerun()


def apply(app: Any) -> None:
    """Install v1.2.7 without replacing the production PlanReader architecture."""
    if getattr(app, "_pb_floor_mapper_v127_applied", False):
        return
    app._pb_floor_mapper_v127_applied = True
    app.calibration_px_per_m = calibration_px_per_m
    app.measured_box_area_m2 = measured_box_area_m2
    app.build_floor_area_rows = build_floor_area_rows

    base_mapper = app.plan_mapper_page

    def _v127_plan_mapper_page(workspace):
        app.hero(workspace)
        with app.st.expander("Fast floor m² mapper (v1.2.7)", expanded=True):
            floor_mapper_panel(app, workspace)
        # The base mapper renders its own hero. Suppress only that duplicate while
        # preserving the rest of the production page unchanged.
        original_hero = app.hero
        app.hero = lambda *_args, **_kwargs: None
        try:
            return base_mapper(workspace)
        finally:
            app.hero = original_hero

    app.plan_mapper_page = _v127_plan_mapper_page
