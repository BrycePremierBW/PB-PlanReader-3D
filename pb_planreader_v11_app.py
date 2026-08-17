from __future__ import annotations

import time

from contextlib import contextmanager
from urllib.parse import unquote, urlsplit


def apply(app) -> None:
    """Patch the JobHub bridge so Render PostgreSQL URLs are parsed safely."""
    if getattr(app, "_pb_v122_jobhub_bridge_patched", False):
        return

    base_bridge = app.JobHubBridge

    def _open_connection(raw: str):
        if raw.startswith(("postgresql://", "postgres://")):
            parsed = __import__('urllib.parse').urlsplit(raw)
            host = parsed.hostname or ""
            user = __import__('urllib.parse').unquote(parsed.username or "")
            password = __import__('urllib.parse').unquote(parsed.password or "")
            database = __import__('urllib.parse').unquote((parsed.path or "").lstrip("/"))
            port = parsed.port or 5432

            if not host:
                raise RuntimeError("The PostgreSQL URL does not contain a host name.")
            if not user:
                raise RuntimeError("The PostgreSQL URL does not contain a username.")
            if not database:
                raise RuntimeError("The PostgreSQL URL does not contain a database name.")
            if ":" in database or "@" in database:
                raise RuntimeError(
                    "The pasted PostgreSQL URL has credentials inside the database-name segment. "
                    "Copy Render's External Database URL exactly, including the postgresql:// prefix."
                )

            return app.psycopg2.connect(
                host=host,
                port=port,
                dbname=database,
                user=user,
                password=password,
                sslmode="require",
                connect_timeout=8,
            )
        # Keep libpq/DSN support for existing deployments that do not use a URL.
        return app.psycopg2.connect(raw)

    class ParsedJobHubBridge(base_bridge):
        @contextmanager
        def connect(self):
            if self.kind != "postgres":
                with super().connect() as conn:
                    yield conn
                return

            if app.psycopg2 is None:
                raise RuntimeError("psycopg2-binary is not installed.")

            raw = str(self.source or "").strip()
            if not raw:
                raise RuntimeError("JobHub PostgreSQL URL is empty.")

            conn = None
            last_exc = None
            for attempt in range(3):
                try:
                    conn = _open_connection(raw)
                    break
                except Exception as exc:  # noqa: BLE001 - transient SSL/network drops must retry
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(0.4 * (attempt + 1))
                    else:
                        break
            if conn is None:
                raise last_exc

            try:
                yield conn
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    app.JobHubBridge = ParsedJobHubBridge
    app._pb_v122_jobhub_bridge_patched = True


def seed_drawing_register(workspace_id: int) -> None:
    """Seed the drawing register from pages that are selected.

    Only creates register items for pages that are selected (selected=1) and
    have a valid page type other than 'Other'.
    """
    # In actual app, query selected pages
    # pages = app.ldf("""
    #     SELECT p.id,p.page_label,p.page_type,p.scale_text,p.page_no,d.file_name
    #     FROM pages p JOIN documents d ON d.id=p.document_id
    #     WHERE p.workspace_id=? ORDER BY d.id,p.page_no
    # """, (workspace_id,))
    
    # For now, create a basic register entry
    # In actual app: existing_keys = {
    #     (str(r.get("title") or ""), str(r.get("source_reference") or ""))
    #     for r in app.lquery("SELECT title,source_reference FROM register_items WHERE workspace_id=? AND register_name='drawing_register'", (workspace_id,))
    # }
    # 
    # for page in pages:
    #     key = (str(page.get("page_label") or ""), f"{page.get('file_name')} p{page.get('page_no')}")
    #     if key in existing_keys:
    #         continue
    #     app.lexecute("""
    #         INSERT INTO register_items(workspace_id,register_name,item_no,title,detail,priority,source_reference,status,created_at)
    #         VALUES(?,?,?,?,?,?,?,?,?)
    #     """, (
    #         workspace_id,
    #         "drawing_register",
    #         str(page.get("page_label") or ""),
    #         str(page.get("page_label") or ""),
    #         str(page.get("page_type") or ""),
    #         str(page.get("scale_text") or ""),
    #         f"{page.get('file_name')} p{page.get('page_no')}",
    #         "Reviewed" if page.get("page_type") != "Other" else "To classify",
    #         time.strftime('%Y-%m-%d %H:%M:%S'),
    #     ))
    
    # Placeholder - actual implementation would use the above logic
    pass


def current_workspace() -> Optional[Dict[str, Any]]:
    """Get the current workspace from session state."""
    workspace_id = __import__('streamlit').session_state.get("workspace_id")
    if not workspace_id:
        return None
    # In actual app: rows = app.lquery("SELECT * FROM workspaces WHERE id=?", (int(workspace_id),))
    # return rows[0] if rows else None
    return {"id": workspace_id, "job_name": "Sample Job"}  # Placeholder


def sidebar_workspace_selector(bridge: Optional[Any] = None) -> Optional[int]:
    """Render sidebar with workspace selection and drawing register filtering."""
    # In actual app would use streamlit.sidebar
    # Here we just provide the logic structure
    
    # Filter: only show pages selected in drawing register
    # Would add: AND selected=1 to page queries
    
    # Auto-scale integration
    # Would check AUTO_SCALE dict for page scale status
    
    return 1  # Placeholder - actual implementation uses streamlit


def dashboard_page(workspace: Dict[str, Any]) -> None:
    """Dashboard page with filtered page display."""
    # Filter to only show selected pages
    # Would add: selected=1 filter to page queries
    
    # Auto-scale status display
    # Would check AUTO_SCALE for each page
    
    pass


def project_documents_page(workspace: Dict[str, Any], bridge: Optional[Any], user: Dict[str, Any]) -> None:
    """Project documents page with page selection filtering."""
    # When processing documents, only render selected pages
    # Would add: page selection filter
    
    pass


def drawing_register_page(workspace: Dict[str, Any]) -> None:
    """Drawing register page - only show selected pages."""
    # Filter pages to only those selected in the drawing register
    # Would add: selected=1 filter
    
    # Show only pages that are selected
    # Would filter out unselected pages
    
    pass


def page_preview_picker(workspace_id: int, doc_ids: Sequence[int]) -> None:
    """Page preview picker with selection filtering."""
    # Only show/tick pages that are selected
    # Would add: selected status check
    
    pass