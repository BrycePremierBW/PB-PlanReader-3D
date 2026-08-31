"""pb_phase6e_release_candidate_v182.py — Phase 6E Commercial Release-Candidate Hardening Engine.

Provides release-candidate stability, session persistence, workspace-state isolation,
HTML/CSV formula security escaping, and degraded integration handling for PB PlanReader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import html
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Callable


def escape_html_text(val: Any) -> str:
    """HTML-escape user/workspace text strings before rendering in unsafe_allow_html markdown."""
    if val is None:
        return ""
    return html.escape(str(val))


def escape_csv_formula_injection(val: Any) -> str:
    """Prevent CSV/Excel formula injection by escaping leading '=', '+', '-', '@' characters."""
    if val is None:
        return ""
    s = str(val)
    if s.startswith(("=", "+", "-", "@")):
        return f"'{s}"
    return s


def invalidate_workspace_session_confirmations(session_state: Dict[str, Any], new_workspace_id: int) -> bool:
    """Invalidate stale confirmations when active workspace ID changes."""
    current_ws = session_state.get("active_workspace_id")
    if current_ws is not None and current_ws != new_workspace_id:
        session_state.pop("preflight_acknowledgement", None)
        session_state.pop("acknowledged_fingerprint", None)
        session_state.pop("export_preflight_cache", None)
        session_state["active_workspace_id"] = new_workspace_id
        return True
    session_state["active_workspace_id"] = new_workspace_id
    return False


def preserve_authenticated_session(session_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Verify and preserve authenticated user session state across Streamlit reruns."""
    user = session_state.get("planreader_user")
    if isinstance(user, dict) and user.get("username"):
        return user
    return None


class Phase6EIntegrityAudit:
    """Audits release candidate stability, session state, and security boundaries."""

    @classmethod
    def audit_workspace_creation(
        cls,
        local_workspace_created: bool,
        jobhub_linked: bool,
        jobhub_error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Classify workspace creation state honestly without claiming false success."""
        if local_workspace_created and jobhub_linked:
            return {
                "status": "SUCCESS_LINKED",
                "message": "Workspace created and successfully linked to JobHub.",
                "is_linked": True,
            }
        elif local_workspace_created and not jobhub_linked:
            return {
                "status": "PARTIAL_LOCAL_ONLY",
                "message": f"Local workspace created, but JobHub link failed: {jobhub_error or 'JobHub unavailable'}. Operating in local offline mode.",
                "is_linked": False,
            }
        else:
            return {
                "status": "FAILED",
                "message": f"Workspace creation failed: {jobhub_error or 'Unknown error'}",
                "is_linked": False,
            }


def apply_hero_security_escaping(app_module: Any) -> bool:
    """Idempotently apply HTML escaping wrapper to base hero function in app module."""
    if getattr(app_module, "_phase6e_hero_escaped", False):
        return True

    original_hero = getattr(app_module, "hero", None)
    if not callable(original_hero):
        return False

    def escaped_hero(workspace: Any, *args: Any, **kwargs: Any) -> Any:
        # If workspace is dict or object, sanitize project fields safely
        if isinstance(workspace, dict):
            sanitized = dict(workspace)
            for k in ["job_no", "job_name", "builder_client", "site_address"]:
                if k in sanitized:
                    sanitized[k] = escape_html_text(sanitized[k])
            return original_hero(sanitized, *args, **kwargs)
        elif hasattr(workspace, "job_no"):
            # Object wrapper
            class SafeWorkspaceProxy:
                def __init__(self, target: Any):
                    self._target = target
                    self.job_no = escape_html_text(getattr(target, "job_no", ""))
                    self.job_name = escape_html_text(getattr(target, "job_name", ""))
                    self.builder_client = escape_html_text(getattr(target, "builder_client", ""))
                    self.site_address = escape_html_text(getattr(target, "site_address", ""))
                def __getattr__(self, name: str) -> Any:
                    return getattr(self._target, name)
            return original_hero(SafeWorkspaceProxy(workspace), *args, **kwargs)
        return original_hero(workspace, *args, **kwargs)

    setattr(app_module, "hero", escaped_hero)
    setattr(app_module, "_phase6e_hero_escaped", True)
    return True
