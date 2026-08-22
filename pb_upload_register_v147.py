"""PlanReader v1.4.8 fast Upload + Drawing Register page.

Keeps the original PlanReader sidebar/navigation, but treats upload/documents and the
Drawing Register as two lightweight views of the same page. Only the active view is
rendered on a Streamlit rerun, avoiding the previous double-render performance cost.
"""
from __future__ import annotations

from typing import Any

VERSION = "1.4.8"


def apply(app: Any) -> None:
    if getattr(app, "_pb_upload_register_v147_applied", False):
        return
    app._pb_upload_register_v147_applied = True

    if not hasattr(app, "_pb_original_project_documents_page_v147"):
        app._pb_original_project_documents_page_v147 = app.project_documents_page

    original = app._pb_original_project_documents_page_v147

    def _combined_project_documents_page(workspace, bridge, user):
        wid = int(workspace["id"])
        state_key = f"pb_documents_view_{wid}"
        if state_key not in app.st.session_state:
            app.st.session_state[state_key] = "upload"

        app.st.markdown("### Project documents")
        app.st.caption(
            "Upload/manage the drawing set and review the Drawing Register from the same page. "
            "Only the view you are using is loaded, so normal clicks stay fast."
        )

        c1, c2 = app.st.columns(2)
        upload_active = app.st.session_state[state_key] == "upload"
        register_active = app.st.session_state[state_key] == "register"

        if c1.button(
            "Upload & documents" + (" ✓" if upload_active else ""),
            type="primary" if upload_active else "secondary",
            use_container_width=True,
            key=f"pb_docs_upload_{wid}",
        ):
            if not upload_active:
                app.st.session_state[state_key] = "upload"
                app.st.rerun()

        if c2.button(
            "Drawing register" + (" ✓" if register_active else ""),
            type="primary" if register_active else "secondary",
            use_container_width=True,
            key=f"pb_docs_register_{wid}",
        ):
            if not register_active:
                app.st.session_state[state_key] = "register"
                app.st.rerun()

        app.st.divider()

        # Critical performance rule: render exactly one heavy view per rerun.
        if app.st.session_state[state_key] == "register":
            app.st.markdown("#### Drawing register")
            app.st.caption(
                "Check sheet names/types and correct anything PlanReader classified incorrectly "
                "before measuring."
            )
            return app.drawing_register_page(workspace)

        return original(workspace, bridge, user)

    app.project_documents_page = _combined_project_documents_page
    app.upload_register_version = VERSION
