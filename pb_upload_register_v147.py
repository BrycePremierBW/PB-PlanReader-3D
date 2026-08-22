"""PlanReader v1.4.7 combined Upload + Drawing Register page.

Keeps the original PlanReader sidebar/navigation, but removes the unnecessary jump
between uploading a drawing set and reviewing/classifying the drawing register.
"""
from __future__ import annotations

from typing import Any

VERSION = "1.4.7"


def apply(app: Any) -> None:
    if getattr(app, "_pb_upload_register_v147_applied", False):
        return
    app._pb_upload_register_v147_applied = True

    if not hasattr(app, "_pb_original_project_documents_page_v147"):
        app._pb_original_project_documents_page_v147 = app.project_documents_page

    original = app._pb_original_project_documents_page_v147

    def _combined_project_documents_page(workspace, bridge, user):
        # First keep every existing upload/document-management function exactly as-is.
        result = original(workspace, bridge, user)

        # Then put the drawing register directly underneath it so uploaded pages can
        # be checked, classified and corrected without navigating to another screen.
        app.st.divider()
        app.st.markdown("### Drawing register")
        app.st.caption(
            "Review the uploaded drawing set here. Check sheet names/types and correct "
            "anything PlanReader has classified incorrectly before measuring."
        )
        app.drawing_register_page(workspace)
        return result

    app.project_documents_page = _combined_project_documents_page
    app.upload_register_version = VERSION
