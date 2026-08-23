"""Persistent browser login for Premier Brushworks PlanReader.

Passwords are never stored in the browser. A random revocable token is saved in
browser localStorage and mapped to the authenticated user locally, with a
best-effort JobHub mirror for cross-restart recovery.

Startup rule: a remembered session must never write to JobHub on every Streamlit
rerun. Tokens are persisted once when created, restored locally first, and only
fall back to JobHub when the local persistent-disk copy is unavailable.
"""

from __future__ import annotations

import json
import secrets
from typing import Optional

_STORAGE_KEY = "pb_planreader_remember_token_v1"
_REMEMBER_KEY = "_pb_planreader_remember_login"
_TOKEN_KEY = "_pb_planreader_auth_token"
_CLEAR_KEY = "_pb_planreader_clear_remember"
_SAVED_SESSION_KEY = "_pb_planreader_auth_token_saved_this_session"
_PREFIX = "planreader_auth_token:"


def apply(app) -> None:
    if getattr(app, "_pb_persistent_login_v1229_applied", False):
        return
    app._pb_persistent_login_v1229_applied = True

    st = app.st
    base_set_page_config = st.set_page_config
    base_login_screen = app.login_screen
    base_sidebar_selector = app.sidebar_workspace_selector

    def emit_script(script: str) -> None:
        try:
            st.html(f"<script>{script}</script>", unsafe_allow_javascript=True)
        except Exception:
            pass

    def browser_bootstrap() -> None:
        storage_key = json.dumps(_STORAGE_KEY)
        emit_script(
            f"""
            (() => {{
              try {{
                const w = window.parent;
                const url = new URL(w.location.href);
                if (!url.searchParams.get('pr_auth')) {{
                  const token = w.localStorage.getItem({storage_key});
                  if (token) {{
                    url.searchParams.set('pr_auth', token);
                    w.location.replace(url.toString());
                  }}
                }}
              }} catch (e) {{}}
            }})();
            """
        )

    def clear_browser_token() -> None:
        storage_key = json.dumps(_STORAGE_KEY)
        emit_script(
            f"""
            (() => {{
              try {{
                const w = window.parent;
                w.localStorage.removeItem({storage_key});
                const url = new URL(w.location.href);
                url.searchParams.delete('pr_auth');
                w.history.replaceState({{}}, '', url.toString());
              }} catch (e) {{}}
            }})();
            """
        )

    def sync_browser_token(token: str) -> None:
        storage_key = json.dumps(_STORAGE_KEY)
        token_js = json.dumps(str(token))
        remember = bool(st.session_state.get(_REMEMBER_KEY, False))
        action = (
            f"w.localStorage.setItem({storage_key}, {token_js});"
            if remember
            else f"w.localStorage.removeItem({storage_key});"
        )
        emit_script(
            f"""
            (() => {{
              try {{
                const w = window.parent;
                {action}
                const url = new URL(w.location.href);
                url.searchParams.delete('pr_auth');
                w.history.replaceState({{}}, '', url.toString());
              }} catch (e) {{}}
            }})();
            """
        )

    def set_page_config_with_bootstrap(*args, **kwargs):
        result = base_set_page_config(*args, **kwargs)
        if st.session_state.pop(_CLEAR_KEY, False):
            clear_browser_token()
        else:
            browser_bootstrap()
        return result

    st.set_page_config = set_page_config_with_bootstrap

    def ensure_local_table() -> None:
        conn = app.local_connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS persistent_login_tokens (
                    token TEXT PRIMARY KEY,
                    user_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def save_local_token(token: str, user: dict) -> None:
        payload = json.dumps(user)
        ensure_local_table()
        conn = app.local_connect()
        try:
            conn.execute("DELETE FROM persistent_login_tokens WHERE token=?", (token,))
            conn.execute(
                "INSERT INTO persistent_login_tokens (token, user_json, created_at) VALUES (?, ?, ?)",
                (token, payload, app.now_stamp()),
            )
            conn.commit()
        finally:
            conn.close()

    def load_local_token(token: str) -> Optional[dict]:
        try:
            ensure_local_table()
            conn = app.local_connect()
            try:
                row = conn.execute(
                    "SELECT user_json FROM persistent_login_tokens WHERE token=?",
                    (token,),
                ).fetchone()
                if row:
                    user = json.loads(str(row[0] or "{}"))
                    return user if isinstance(user, dict) and user else None
            finally:
                conn.close()
        except Exception:
            pass
        return None

    def save_token(bridge, token: str, user: dict) -> None:
        """Persist once locally; mirror to JobHub only when the token is created."""
        save_local_token(token, user)
        payload = json.dumps(user)
        if bridge is not None:
            try:
                if "app_settings" in set(bridge.table_names()):
                    key = f"{_PREFIX}{token}"
                    bridge.execute("DELETE FROM app_settings WHERE setting_key=?", (key,))
                    bridge.execute(
                        "INSERT INTO app_settings (setting_key, setting_value) VALUES (?, ?)",
                        (key, payload),
                    )
            except Exception:
                # Local persistent-disk token remains fully usable.
                pass

    def load_token(bridge, token: str) -> Optional[dict]:
        if not token:
            return None

        # Fast path: persistent PlanReader disk. This avoids a Postgres round-trip
        # every time a remembered browser opens the app.
        user = load_local_token(token)
        if user:
            return user

        # Recovery path only: a token may have been created before this local
        # database copy existed. Read the shared mirror once, then seed local.
        if bridge is not None:
            try:
                if "app_settings" in set(bridge.table_names()):
                    rows = bridge.query(
                        "SELECT setting_value FROM app_settings WHERE setting_key=?",
                        (f"{_PREFIX}{token}",),
                    )
                    if rows:
                        user = json.loads(str(rows[0].get("setting_value") or "{}"))
                        if isinstance(user, dict) and user:
                            save_local_token(token, user)
                            return user
            except Exception:
                pass
        return None

    def delete_token(bridge, token: str) -> None:
        if not token:
            return
        try:
            ensure_local_table()
            conn = app.local_connect()
            try:
                conn.execute("DELETE FROM persistent_login_tokens WHERE token=?", (token,))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        # Sign-out is user-triggered, so it is fine to revoke the shared mirror
        # here. Unlike the old code this is not run during ordinary page reruns.
        if bridge is not None:
            try:
                if "app_settings" in set(bridge.table_names()):
                    bridge.execute(
                        "DELETE FROM app_settings WHERE setting_key=?",
                        (f"{_PREFIX}{token}",),
                    )
            except Exception:
                pass

    class RememberForm:
        def __init__(self, form):
            self.form = form

        def __enter__(self):
            entered = self.form.__enter__()
            st.checkbox(
                "Stay signed in on this device",
                key=_REMEMBER_KEY,
                help="Keeps this device signed in until you sign out or the saved session is revoked.",
            )
            return entered

        def __exit__(self, exc_type, exc, tb):
            return self.form.__exit__(exc_type, exc, tb)

    base_form = st.form

    def form_with_remember(key, *args, **kwargs):
        form = base_form(key, *args, **kwargs)
        if str(key) in {"login_form", "local_login"}:
            return RememberForm(form)
        return form

    st.form = form_with_remember

    def login_screen_with_restore(bridge) -> None:
        try:
            token = str(st.query_params.get("pr_auth") or "").strip()
        except Exception:
            token = ""
        if token:
            user = load_token(bridge, token)
            if user:
                st.session_state["planreader_user"] = user
                st.session_state[_TOKEN_KEY] = token
                st.session_state[_REMEMBER_KEY] = True
                st.session_state[_SAVED_SESSION_KEY] = True
                st.rerun()
            delete_token(bridge, token)
            st.session_state[_CLEAR_KEY] = True
            clear_browser_token()
        base_login_screen(bridge)

    app.login_screen = login_screen_with_restore

    def sidebar_with_persistence(bridge):
        user = st.session_state.get("planreader_user")
        if user:
            token = str(st.session_state.get(_TOKEN_KEY) or "").strip()
            remember = bool(st.session_state.get(_REMEMBER_KEY, False))
            if remember and not token:
                token = secrets.token_urlsafe(32)
                st.session_state[_TOKEN_KEY] = token
                save_token(bridge, token, dict(user))
                st.session_state[_SAVED_SESSION_KEY] = True
            if token:
                if remember:
                    # Critical startup fix: do not DELETE+INSERT the same token in
                    # remote JobHub on every Streamlit rerun.
                    sync_browser_token(token)
                else:
                    delete_token(bridge, token)
                    st.session_state.pop(_TOKEN_KEY, None)
                    st.session_state.pop(_SAVED_SESSION_KEY, None)
                    clear_browser_token()

        # Intercept the existing Sign out button just long enough to revoke the
        # remembered token before the base function clears session_state/reruns.
        from streamlit.delta_generator import DeltaGenerator

        base_button = DeltaGenerator.button

        def button_with_revoke(self, label, *args, **kwargs):
            clicked = base_button(self, label, *args, **kwargs)
            if clicked and str(label) == "Sign out":
                token = str(st.session_state.get(_TOKEN_KEY) or "").strip()
                delete_token(bridge, token)
                st.session_state.pop(_TOKEN_KEY, None)
                st.session_state.pop(_REMEMBER_KEY, None)
                st.session_state.pop(_SAVED_SESSION_KEY, None)
                st.session_state[_CLEAR_KEY] = True
            return clicked

        DeltaGenerator.button = button_with_revoke
        try:
            return base_sidebar_selector(bridge)
        finally:
            DeltaGenerator.button = base_button

    app.sidebar_workspace_selector = sidebar_with_persistence
