"""PlanReader v1.2.20 memory and media stability hardening.

Fixes blank/unrendered Drawing Register previews and bounds the OpenCV working
resolution used by v1.2.19 automatic geometry. Geometry coordinates are scaled
back to the original rendered drawing, so calibration and m² remain based on the
original page pixel scale.
"""
from __future__ import annotations

import gc
import io
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

import pb_auto_geometry_v1219 as auto_v1219

VERSION = "1.2.20"
REGISTER_PAGE_SIZE = 30
PREVIEW_LONG_EDGE_PX = 1000
CV_LONG_EDGE_PX = 1100


def regular_file(value: Any) -> Optional[Path]:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def thumbnail_bytes(value: Any, max_long_edge: int = PREVIEW_LONG_EDGE_PX) -> bytes:
    """Return a small deterministic JPEG preview, or b'' for blank/missing paths."""
    path = regular_file(value)
    if path is None:
        return b""
    limit = max(320, min(int(max_long_edge or PREVIEW_LONG_EDGE_PX), 1400))
    with Image.open(path) as image:
        image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
        if image.mode != "RGB":
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=78, optimize=False)
        return buffer.getvalue()


def _release_memory(app: Any) -> None:
    """Return large native/Python caches after image/PDF analysis."""
    try:
        cv2 = getattr(auto_v1219, "cv2", None)
        if cv2 is not None:
            cv2.setNumThreads(1)
            try:
                cv2.ocl.setUseOpenCL(False)
            except Exception:
                pass
    except Exception:
        pass
    try:
        fitz = getattr(app, "fitz", None)
        tools = getattr(fitz, "TOOLS", None)
        shrink = getattr(tools, "store_shrink", None)
        if callable(shrink):
            shrink(100)
    except Exception:
        pass
    gc.collect()


def _bounded_gray(image_path: Path, max_long_edge: int = CV_LONG_EDGE_PX):
    cv2 = getattr(auto_v1219, "cv2", None)
    if cv2 is None:
        return None
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None or image.size == 0:
        return None
    original_h, original_w = image.shape[:2]
    long_edge = max(original_w, original_h)
    limit = max(640, min(int(max_long_edge or CV_LONG_EDGE_PX), 1400))
    if long_edge <= limit:
        return image, 1.0, 1.0, original_w, original_h
    ratio = limit / float(long_edge)
    work_w = max(1, int(round(original_w * ratio)))
    work_h = max(1, int(round(original_h * ratio)))
    work = cv2.resize(image, (work_w, work_h), interpolation=cv2.INTER_AREA)
    del image
    return work, original_w / float(work_w), original_h / float(work_h), original_w, original_h


def bounded_drawing_component(image_path: Path, *, elevation: bool = False) -> Optional[Dict[str, Any]]:
    """Memory-bounded equivalent of v1.2.19 drawing-cluster detection."""
    cv2 = getattr(auto_v1219, "cv2", None)
    np = getattr(auto_v1219, "np", None)
    if cv2 is None or np is None:
        return None
    loaded = _bounded_gray(image_path)
    if loaded is None:
        return None
    image, sx, sy, original_w, original_h = loaded
    try:
        height, width = image.shape[:2]
        y_end = max(1, int(height * (0.82 if elevation else 0.86)))
        x_start, x_end = int(width * 0.03), int(width * 0.97)
        roi = image[:y_end, x_start:x_end]
        _, ink = cv2.threshold(roi, 205, 255, cv2.THRESH_BINARY_INV)
        kernel = np.ones((3, 3), np.uint8)
        connected = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel, iterations=1)
        count, labels, stats, _ = cv2.connectedComponentsWithStats((connected > 0).astype(np.uint8), 8)
        best = None
        roi_area = float(max(1, roi.shape[0] * roi.shape[1]))
        for label_id in range(1, count):
            x, y, w, h, pixels = [int(v) for v in stats[label_id]]
            bbox_area = float(w * h)
            frac = bbox_area / roi_area
            if w < roi.shape[1] * 0.12 or h < roi.shape[0] * 0.10 or not (0.015 <= frac <= 0.78):
                continue
            density = pixels / max(bbox_area, 1.0)
            if not (0.006 <= density <= 0.45):
                continue
            score = bbox_area * (0.45 + min(density, 0.12) * 5.0)
            if best is None or score > best["score"]:
                best = {"label": label_id, "score": score, "bbox": (x + x_start, y, w, h), "density": density}
        if best is None:
            return None
        component_mask = np.zeros_like(connected)
        component_mask[labels == best["label"]] = 255
        contours, _ = cv2.findContours(component_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        contour = max(contours, key=cv2.contourArea)
        contour[:, 0, 0] += x_start
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, max(2.0, perimeter * 0.006), True)
        points = [[float(p[0][0]) * sx, float(p[0][1]) * sy] for p in approx]
        x, y, w, h = best["bbox"]
        return {
            "bbox": [float(x) * sx, float(y) * sy, float(w) * sx, float(h) * sy],
            "polygon": points,
            "pixel_area": float(abs(cv2.contourArea(contour))) * sx * sy,
            "density": float(best["density"]),
            "image_width": original_w,
            "image_height": original_h,
        }
    finally:
        del image
        gc.collect()


def bounded_unit_boundary_candidates(app: Any, page: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Detect unit contours on a bounded working image and return original-pixel geometry."""
    cv2 = getattr(auto_v1219, "cv2", None)
    np = getattr(auto_v1219, "np", None)
    if cv2 is None or np is None or auto_v1219._num(page.get("px_per_m")) <= 0:
        return []
    image_path = auto_v1219._regular_image(page.get("image_path"))
    if image_path is None:
        return []
    lines = auto_v1219._pdf_word_lines(app, page)
    labels: List[Dict[str, Any]] = []
    for line in lines:
        match = auto_v1219._UNIT_LABEL_RE.search(line["text"])
        if match:
            labels.append({"label": f"Unit {match.group(1)}", "center": line["center"]})
    if not labels:
        return []
    loaded = _bounded_gray(image_path)
    if loaded is None:
        return []
    image, sx, sy, original_w, original_h = loaded
    try:
        height, width = image.shape[:2]
        _, ink = cv2.threshold(image, 205, 255, cv2.THRESH_BINARY_INV)
        ink[int(height * 0.88):, :] = 0
        kernel = np.ones((3, 3), np.uint8)
        ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel, iterations=1)
        contours, _ = cv2.findContours(ink, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        page_area = float(width * height)
        eligible = []
        for idx, contour in enumerate(contours):
            area = abs(float(cv2.contourArea(contour)))
            if not (page_area * 0.008 <= area <= page_area * 0.45):
                continue
            x, y, w, h = cv2.boundingRect(contour)
            if w < width * 0.06 or h < height * 0.06 or y > height * 0.86:
                continue
            eligible.append((idx, contour, area, (x, y, w, h)))
        chosen = []
        for label in labels:
            cx, cy = label["center"]
            work_point = (float(cx) / sx, float(cy) / sy)
            containing = [item for item in eligible if cv2.pointPolygonTest(item[1], work_point, False) >= 0]
            if not containing:
                continue
            item = max(containing, key=lambda candidate: candidate[2])
            chosen.append((label, item[0], item[1], item[2], item[3]))
        contour_use: Dict[int, int] = {}
        for _label, contour_id, *_rest in chosen:
            contour_use[contour_id] = contour_use.get(contour_id, 0) + 1
        pxpm = auto_v1219._num(page.get("px_per_m"))
        results: List[Dict[str, Any]] = []
        for label, contour_id, contour, area_work, bbox in chosen:
            if contour_use.get(contour_id, 0) != 1:
                continue
            area_m2 = area_work * sx * sy / (pxpm * pxpm)
            if not (8.0 <= area_m2 <= 1000.0):
                continue
            perimeter = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, max(2.0, perimeter * 0.005), True)
            x, y, w, h = bbox
            results.append({
                "label": label["label"],
                "area_m2": round(area_m2, 2),
                "confidence": "Derived",
                "source": "Closed drawing boundary around unit label",
                "bbox": [float(x) * sx, float(y) * sy, float(w) * sx, float(h) * sy],
                "polygon": [[float(p[0][0]) * sx, float(p[0][1]) * sy] for p in approx],
                "image_width": original_w,
                "image_height": original_h,
            })
        return results
    finally:
        del image
        gc.collect()


def drawing_register_page(app: Any, workspace: Dict[str, Any]) -> None:
    """Memory-safe drawing register with paginated metadata and one small preview."""
    workspace_id = int(workspace["id"])
    app.hero(workspace)
    pages = app.ldf(
        """SELECT p.id,p.page_label,p.page_type,p.scale_text,p.px_per_m,p.page_no,
                  p.selected,d.file_name,p.image_path
           FROM pages p JOIN documents d ON d.id=p.document_id
           WHERE p.workspace_id=? ORDER BY d.id,p.page_no""",
        (workspace_id,),
    )
    if pages.empty:
        app.st.info("Process documents first.")
        return

    selected_count = int(pages["selected"].fillna(0).astype(bool).sum())
    c1, c2, c3 = app.st.columns(3)
    c1.metric("Drawing sheets", len(pages))
    c2.metric("Take-off selected", selected_count)
    c3.metric("Discarded / reference", len(pages) - selected_count)

    mode = app.st.radio(
        "Show sheets",
        ["Selected take-off sheets", "Discarded / reference sheets", "All sheets"],
        horizontal=True,
        key=f"drawing_register_filter_{workspace_id}",
    )
    if mode.startswith("Selected"):
        filtered = pages[pages["selected"].fillna(0).astype(bool)].copy()
    elif mode.startswith("Discarded"):
        filtered = pages[~pages["selected"].fillna(0).astype(bool)].copy()
    else:
        filtered = pages.copy()

    if filtered.empty:
        app.st.info("No sheets match this filter.")
        return

    total_pages = max(1, math.ceil(len(filtered) / REGISTER_PAGE_SIZE))
    page_number = int(app.st.number_input(
        "Register page",
        min_value=1,
        max_value=total_pages,
        value=1,
        step=1,
        key=f"drawing_register_page_{workspace_id}_{mode}",
    ))
    start = (page_number - 1) * REGISTER_PAGE_SIZE
    visible = filtered.iloc[start:start + REGISTER_PAGE_SIZE].copy()
    app.st.caption(f"Showing {start + 1}-{start + len(visible)} of {len(filtered)} matching sheets. Only this register page is rendered in the editor.")

    editable = visible[["id", "page_label", "page_type", "scale_text", "selected"]].copy()
    edited = app.st.data_editor(
        editable,
        use_container_width=True,
        hide_index=True,
        column_config={
            "id": app.st.column_config.NumberColumn(disabled=True),
            "page_type": app.st.column_config.SelectboxColumn(options=app.PAGE_TYPES),
            "selected": app.st.column_config.CheckboxColumn(),
        },
        num_rows="fixed",
        key=f"drawing_register_editor_{workspace_id}_{mode}_{page_number}",
    )
    if app.st.button("Save drawing register changes", type="primary", key=f"drawing_register_save_{workspace_id}_{mode}_{page_number}"):
        conn = app.local_connect()
        try:
            conn.executemany(
                "UPDATE pages SET page_label=?,page_type=?,scale_text=?,selected=? WHERE id=?",
                [
                    (
                        row.get("page_label", ""), row.get("page_type", "Other"), row.get("scale_text", ""),
                        1 if row.get("selected") else 0, int(row["id"]),
                    )
                    for row in edited.to_dict("records")
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        app.seed_drawing_register(workspace_id)
        app.st.success("Drawing register changes saved.")
        app.st.rerun()

    app.st.subheader("Drawing preview")
    preview_labels = [f"#{int(r.id)} · {r.page_label} · {r.page_type}" for r in visible.itertuples()]
    chosen = app.st.selectbox(
        "Preview page",
        preview_labels,
        key=f"drawing_register_preview_select_{workspace_id}_{mode}_{page_number}",
    )
    row = visible.iloc[preview_labels.index(chosen)]
    image_path = regular_file(row.get("image_path"))
    if image_path is None:
        state = "selected but not rendered yet" if bool(row.get("selected")) else "indexed as a discarded/reference sheet"
        app.st.info(
            f"This sheet is {state}, so there is no page image to load. Its drawing-register metadata is still retained. "
            "Select it and process the selected pages if you need a visual preview."
        )
        return

    payload = thumbnail_bytes(image_path)
    if not payload:
        app.st.warning("The rendered page file could not be opened for preview. Reprocess this sheet if needed.")
        return
    app.st.image(
        payload,
        caption=f"{row['file_name']} · page {row['page_no']} · memory-safe preview",
        use_container_width=True,
    )
    app.st.caption("Preview is downsampled for the Drawing Register only. Plan Mapper and take-off calculations continue to use the original rendered page and its saved calibration.")
    del payload
    _release_memory(app)


def apply(app: Any) -> None:
    if getattr(app, "_pb_memory_stability_v1220_applied", False):
        return
    app._pb_memory_stability_v1220_applied = True

    # Keep OpenCV from creating extra worker pools on a small Render service.
    _release_memory(app)

    # v1.2.19 resolves these helpers from module globals at run time. Replacing
    # them therefore reduces RAM for automatic processing without changing its
    # calibrated geometry contract.
    auto_v1219._drawing_component = bounded_drawing_component
    auto_v1219._unit_boundary_candidates = bounded_unit_boundary_candidates

    base_analyse = auto_v1219.analyse_workspace

    def _memory_bounded_analyse(app_obj: Any, workspace_id: int):
        _release_memory(app_obj)
        try:
            return base_analyse(app_obj, int(workspace_id))
        finally:
            _release_memory(app_obj)

    auto_v1219.analyse_workspace = _memory_bounded_analyse

    app.drawing_register_page = lambda workspace: drawing_register_page(app, workspace)
    app.planreader_regular_file = regular_file
    app.planreader_thumbnail_bytes = thumbnail_bytes
    app.release_planreader_memory = lambda: _release_memory(app)
