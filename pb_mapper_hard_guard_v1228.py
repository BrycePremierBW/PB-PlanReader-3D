"""PlanReader v1.2.28 hard guard for Plan Mapper image reads.

The legacy mapper constructs ``Path(str(image_path or ''))``.  ``Path('')`` is the
current working directory, so ``exists()`` is true and ``read_bytes()`` raises
IsADirectoryError.  v1.2.25 preflight protected selected pages but the legacy page
picker still listed every workspace page, including deselected/unrendered rows.

This outer wrapper exposes only estimator-selected pages whose image_path is a real
regular file.  Missing selected renders are retried once before the mapper opens.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pb_memory_stability_v1220 as memory

VERSION = "1.2.28"


def _selected_rows(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    return [dict(row) for row in app.lquery(
        "SELECT * FROM pages WHERE workspace_id=? AND COALESCE(selected,0)=1 ORDER BY id",
        (int(workspace_id),),
    )]


def _render_missing(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    rows = _selected_rows(app, workspace_id)
    by_doc: Dict[int, List[int]] = {}
    for row in rows:
        if memory.regular_file(row.get("image_path")) is None:
            by_doc.setdefault(int(row.get("document_id") or 0), []).append(int(row.get("page_no") or 0))
    for document_id, page_nos in by_doc.items():
        if document_id <= 0:
            continue
        try:
            app.process_document(document_id, force=False, page_ids=sorted({p for p in page_nos if p > 0}))
        except Exception:
            pass
    return _selected_rows(app, workspace_id)


def apply(app: Any) -> None:
    if getattr(app, "_pb_mapper_hard_guard_v1228_applied", False):
        return
    app._pb_mapper_hard_guard_v1228_applied = True
    base_mapper = app.plan_mapper_page

    def _guarded_mapper(workspace: Dict[str, Any]):
        workspace_id = int(workspace["id"])
        rows = _render_missing(app, workspace_id)
        valid_ids = {
            int(row["id"]) for row in rows
            if memory.regular_file(row.get("image_path")) is not None
        }
        missing = [row for row in rows if int(row["id"]) not in valid_ids]
        if not valid_ids:
            app.hero(workspace)
            app.st.error("Plan Mapper has no selected drawing sheet with a rendered image. The blank-path crash has been blocked.")
            if missing:
                app.st.dataframe(app.pd.DataFrame([
                    {"Page": row.get("page_label"), "PDF page": row.get("page_no"), "Page ID": row.get("id")}
                    for row in missing
                ]), hide_index=True, use_container_width=True)
            app.st.info("Process the selected sheets from Job & Documents, or choose the required sheets in Drawing Register.")
            return None

        # The legacy mapper asks for SELECT * FROM pages ... and then uses its own
        # selectbox.  Temporarily narrow only that exact query to selected + valid
        # regular image files. This prevents a deselected/blank row from being
        # chosen after the v1.2.25 preflight has already completed.
        base_ldf = app.ldf
        def _safe_ldf(sql, params=()):
            text = " ".join(str(sql or "").lower().split())
            frame = base_ldf(sql, params)
            if text.startswith("select * from pages where workspace_id=? order by id") and not frame.empty:
                frame = frame[frame["id"].astype(int).isin(valid_ids)].copy()
            return frame

        app.ldf = _safe_ldf
        try:
            return base_mapper(workspace)
        finally:
            app.ldf = base_ldf

    app.plan_mapper_page = _guarded_mapper
    app.mapper_valid_page_ids_v1228 = lambda workspace_id: {
        int(row["id"]) for row in _selected_rows(app, int(workspace_id))
        if memory.regular_file(row.get("image_path")) is not None
    }
