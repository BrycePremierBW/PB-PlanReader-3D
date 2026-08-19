"""v1.2.23 upload-batch guard.

The main Autopilot index wrapper can render immediately after each indexed file.
During Streamlit multi-file uploads that means the uploader's raw bytes and PDF
rendering can overlap in memory. This outer guard deliberately suppresses that
per-document render and leaves the workspace `pending`; the Project & Documents
wrapper then renders all selected missing pages on the immediate rerun, after the
uploader batch has been released.
"""
from __future__ import annotations

from typing import Any

import pb_autopilot_v1223 as autopilot


def apply(app: Any) -> None:
    if getattr(app, "_pb_autopilot_upload_batch_v1223_applied", False):
        return
    app._pb_autopilot_upload_batch_v1223_applied = True

    base_index = app.index_document_pages

    def _batch_index(document_id: int, *args, **kwargs):
        doc_id = int(document_id)
        token = autopilot._processing_document.set(doc_id)
        try:
            result = base_index(doc_id, *args, **kwargs)
        finally:
            autopilot._processing_document.reset(token)

        docs = app.lquery("SELECT workspace_id FROM documents WHERE id=?", (doc_id,))
        if docs:
            workspace_id = int(docs[0]["workspace_id"])
            state = autopilot._state_get(app, workspace_id)
            state.update({
                "version": autopilot.VERSION,
                "pending": True,
                "completed": False,
                "last_indexed_document": doc_id,
                "upload_batch_deferred": True,
                "updated_at": app.now_stamp(),
            })
            autopilot._state_set(app, workspace_id, state)
        return result

    app.index_document_pages = _batch_index
