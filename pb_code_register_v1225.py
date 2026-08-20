"""PlanReader v1.2.25 one-step project code register.

Unknown architectural/paint/material codes are resolved from the Review Issues panel
without forcing the estimator to edit a hidden schedule. Manual project definitions
outrank automatic schedule parsing and are retained in workspace settings.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import pb_material_schedule_v1222 as material

VERSION = "1.2.25"
SETTING_KEY = "manual_material_codes_v1225"
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9._-]{1,15}$")

DEFAULT_ELEMENTS = [
    "Internal walls", "Ceilings", "Doors / frames", "Skirting / architraves",
    "External walls / cladding", "Soffits / eaves", "Screens", "Balustrades",
    "Metalwork", "Timberwork", "Specialist finish", "Other",
]
DEFAULT_SUBSTRATES = [
    "Lineaboard Cladding", "Textureboard Cladding", "Easylap Cladding", "Fibre Cement Cladding",
    "Rendered / Blockwork", "Render", "Plasterboard", "Soffits / Eaves", "Timber / Weatherboard Cladding",
    "Screens", "Balustrade", "Structural steel", "Metalwork", "Timber trim / joinery", "Other",
]


def _load(app: Any, workspace_id: int) -> Dict[str, Dict[str, Any]]:
    rows = app.lquery("SELECT value FROM workspace_settings WHERE workspace_id=? AND key=?", (int(workspace_id), SETTING_KEY))
    if not rows:
        return {}
    try:
        raw = json.loads(str(rows[0].get("value") or "{}"))
        return {str(k).upper(): dict(v) for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save(app: Any, workspace_id: int, overrides: Dict[str, Dict[str, Any]]) -> None:
    app.lexecute(
        """INSERT INTO workspace_settings(workspace_id,key,value,updated_at) VALUES(?,?,?,?)
           ON CONFLICT(workspace_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (int(workspace_id), SETTING_KEY, json.dumps(overrides, separators=(",", ":"), default=str), app.now_stamp()),
    )


def set_manual_code(
    app: Any,
    workspace_id: int,
    code: str,
    description: str,
    substrate: str = "",
    finish: str = "",
    element: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    code = str(code or "").strip().upper()
    if not _CODE_RE.fullmatch(code):
        raise ValueError("Code must start with a letter and contain only letters, numbers, dots, dashes or underscores.")
    if not str(description or "").strip() and not str(substrate or "").strip() and not str(finish or "").strip():
        raise ValueError("Enter at least a description, substrate or finish.")
    overrides = _load(app, int(workspace_id))
    overrides[code] = {
        "code": code,
        "description": str(description or "").strip(),
        "substrate": str(substrate or "").strip(),
        "finish": str(finish or "").strip(),
        "element": str(element or "").strip(),
        "notes": str(notes or "").strip(),
        "updated_at": app.now_stamp(),
    }
    _save(app, int(workspace_id), overrides)
    return dict(overrides[code])


def remove_manual_code(app: Any, workspace_id: int, code: str) -> bool:
    overrides = _load(app, int(workspace_id))
    code = str(code or "").strip().upper()
    if code not in overrides:
        return False
    overrides.pop(code, None)
    _save(app, int(workspace_id), overrides)
    return True


def merged_dictionary(app: Any, workspace_id: int, base_builder) -> Dict[str, Any]:
    state = base_builder(app, int(workspace_id))
    dictionary = dict(state.get("dictionary") or {})
    for code, item in _load(app, int(workspace_id)).items():
        description = str(item.get("description") or "Manual project code")
        dictionary[code] = {
            "code": code,
            "description": description,
            "substrate": str(item.get("substrate") or ""),
            "finish": str(item.get("finish") or ""),
            "element": str(item.get("element") or ""),
            "status": "Confirmed",
            "manual": True,
            "notes": str(item.get("notes") or ""),
            "sources": [{
                "code": code, "description": description, "substrate": str(item.get("substrate") or ""),
                "finish": str(item.get("finish") or ""), "page_id": 0, "page_label": "Manual project code register",
                "source_line": str(item.get("notes") or "Manual estimator definition"),
            }],
        }
        # A deliberate manual definition resolves automatic conflict/unknown issues for that code.
        state["issues"] = [issue for issue in (state.get("issues") or []) if str(issue.get("code") or "").upper() != code]
    state["dictionary"] = dictionary
    return state


def _issue_code(app: Any, workspace_id: int, issues: List[Dict[str, Any]]) -> str:
    selected = str(app.st.session_state.get(f"material_review_issue_{workspace_id}") or "")
    if selected:
        for issue in issues:
            label = f"#{issue.get('id')} · {issue.get('page_label') or 'No page'} · {issue.get('category')} · {issue.get('code') or ''}"
            if label == selected and issue.get("code"):
                return str(issue["code"]).upper()
    unknown = [str(issue.get("code") or "").upper() for issue in issues if issue.get("code")]
    return unknown[0] if unknown else ""


def quick_code_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    state = material._setting_get(app, workspace_id)
    issues = [dict(issue) for issue in (state.get("review_issues") or [])]
    overrides = _load(app, workspace_id)
    suggested = _issue_code(app, workspace_id, issues)
    issue_codes = sorted({str(issue.get("code") or "").upper() for issue in issues if issue.get("code")})

    with app.st.expander("➕ Add / correct a code", expanded=bool(issue_codes)):
        app.st.caption(
            "Use this when PlanReader finds a code but cannot prove what it means. Save it once and the project-wide cross-reference is rebuilt automatically. "
            "Manual project definitions outrank automatic schedule parsing."
        )
        if issue_codes:
            app.st.write("Unresolved codes: " + ", ".join(f"**{code}**" for code in issue_codes[:20]))
        default_code = suggested or (issue_codes[0] if issue_codes else "")
        with app.st.form(f"manual_code_form_v1225_{workspace_id}"):
            c1, c2 = app.st.columns([0.25, 0.75])
            code = c1.text_input("Code", value=default_code, placeholder="e.g. EC1")
            description = c2.text_input("What this code means", placeholder="e.g. James Hardie Linea 180 mm weatherboard")
            c3, c4 = app.st.columns(2)
            substrate_options = [""] + sorted(set(DEFAULT_SUBSTRATES + list(getattr(app, "SUBSTRATES", []))))
            substrate = c3.selectbox("Substrate", substrate_options)
            element = c4.selectbox("Painting element", [""] + DEFAULT_ELEMENTS)
            finish = app.st.text_input("Finish / colour / coating", placeholder="e.g. Dulux Weathershield low sheen · Lexicon Quarter")
            notes = app.st.text_input("Notes / source", placeholder="e.g. confirmed from A602 finish schedule")
            submitted = app.st.form_submit_button("Save code & re-cross-reference", type="primary")
        if submitted:
            try:
                saved = set_manual_code(app, workspace_id, code, description, substrate, finish, element, notes)
            except ValueError as exc:
                app.st.error(str(exc))
            else:
                with app.st.spinner(f"Updating every {saved['code']} reference in the project…"):
                    try:
                        app.run_auto_geometry(workspace_id)
                    except Exception as exc:
                        app.st.warning(f"Code saved, but the automatic geometry refresh needs another run: {exc}")
                app.st.success(f"{saved['code']} saved to the project code register.")
                app.st.rerun()

        if overrides:
            app.st.markdown("#### Manual project codes")
            rows = [{"Code": code, "Element": item.get("element", ""), "Substrate": item.get("substrate", ""), "Finish": item.get("finish", ""), "Description": item.get("description", "")} for code, item in sorted(overrides.items())]
            app.st.dataframe(app.pd.DataFrame(rows), hide_index=True, use_container_width=True)
            remove_code = app.st.selectbox("Remove manual override", [""] + sorted(overrides), key=f"remove_manual_code_v1225_{workspace_id}")
            if remove_code and app.st.button("Remove selected manual code", key=f"remove_manual_code_btn_v1225_{workspace_id}"):
                remove_manual_code(app, workspace_id, remove_code)
                try:
                    app.run_auto_geometry(workspace_id)
                except Exception:
                    pass
                app.st.rerun()


def apply(app: Any) -> None:
    if getattr(app, "_pb_code_register_v1225_applied", False):
        return
    app._pb_code_register_v1225_applied = True

    base_builder = material.build_material_dictionary
    material.build_material_dictionary = lambda app_obj, workspace_id: merged_dictionary(app_obj, int(workspace_id), base_builder)

    base_review = material.review_panel
    def _review_with_quick_code(app_obj: Any, workspace: Dict[str, Any]):
        base_review(app_obj, workspace)
        quick_code_panel(app_obj, workspace)
    material.review_panel = _review_with_quick_code

    app.set_manual_material_code = lambda workspace_id, code, description, substrate="", finish="", element="", notes="": set_manual_code(app, int(workspace_id), code, description, substrate, finish, element, notes)
    app.manual_material_codes = lambda workspace_id: _load(app, int(workspace_id))
