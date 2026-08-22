"""PlanReader v1.4.2 project/building scope gate.

Multi-building drawing sets often contain more geometry than the painting tender.
This module detects likely Block/Building/Tower/Stage groups, lets the estimator
choose what is Included / Reference only / Excluded, and filters calibrated floor
polygons before elevation registration, reconstruction and take-off.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Sequence

VERSION = "1.4.2"
SETTING_KEY = "project_scope_v142"
MODES = ("Included", "Reference only", "Excluded")
_SCOPE_PATTERNS = (
    re.compile(r"\bblock\s*[-:]?\s*([a-z0-9]+)\b", re.I),
    re.compile(r"\bbuilding\s*[-:]?\s*([a-z0-9]+)\b", re.I),
    re.compile(r"\btower\s*[-:]?\s*([a-z0-9]+)\b", re.I),
    re.compile(r"\bstage\s*[-:]?\s*([a-z0-9]+)\b", re.I),
)


def normalise_group(raw: str) -> str:
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    for prefix in ("block", "building", "tower", "stage"):
        m = re.search(rf"\b{prefix}\s*[-:]?\s*([a-z0-9]+)\b", text, re.I)
        if m:
            return f"{prefix.title()} {m.group(1).upper()}"
    return text


def groups_from_text(value: Any) -> List[str]:
    text = str(value or "")
    out: List[str] = []
    for pattern in _SCOPE_PATTERNS:
        prefix = pattern.pattern.split("\\s")[0].replace("\\b", "").title()
        for match in pattern.finditer(text):
            # Recover human prefix from matched text rather than regex internals.
            matched = match.group(0)
            group = normalise_group(matched)
            if group and group not in out:
                out.append(group)
    return out


def detect_scope_groups(app: Any, workspace_id: int, prisms: Sequence[Dict[str, Any]] | None = None) -> List[str]:
    found: List[str] = []
    def add(values: Iterable[str]) -> None:
        for value in values:
            group = normalise_group(value)
            if group and group not in found:
                found.append(group)

    for prism in prisms or []:
        add(groups_from_text(" ".join([
            str(prism.get("label") or ""), str(prism.get("page_label") or "")
        ])))

    rows = app.lquery(
        "SELECT page_label,page_type,extracted_text FROM pages WHERE workspace_id=? ORDER BY page_no,id",
        (int(workspace_id),),
    )
    for row in rows:
        add(groups_from_text(" ".join([
            str(row.get("page_label") or ""), str(row.get("page_type") or ""),
            str(row.get("extracted_text") or "")[:12000],
        ])))
    return sorted(found)


def load_scope(app: Any, workspace_id: int) -> Dict[str, Any]:
    raw = app.workspace_setting(int(workspace_id), SETTING_KEY, "{}")
    try:
        state = json.loads(str(raw or "{}"))
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("groups", {})
    state.setdefault("enabled", False)
    state.setdefault("include_unassigned", False)
    return state


def save_scope(app: Any, workspace_id: int, state: Dict[str, Any]) -> None:
    app.set_workspace_setting(int(workspace_id), SETTING_KEY, json.dumps(state, separators=(",", ":")))


def prism_groups(prism: Dict[str, Any]) -> List[str]:
    return groups_from_text(" ".join([
        str(prism.get("label") or ""), str(prism.get("page_label") or ""),
        str(prism.get("scope_group") or ""),
    ]))


def filter_prisms(prisms: Sequence[Dict[str, Any]], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not state.get("enabled"):
        return list(prisms or [])
    group_modes = {normalise_group(k): str(v) for k, v in (state.get("groups") or {}).items()}
    included = {g for g, mode in group_modes.items() if mode == "Included"}
    if not included:
        return []
    include_unassigned = bool(state.get("include_unassigned", False))
    out: List[Dict[str, Any]] = []
    for prism in prisms or []:
        groups = prism_groups(prism)
        matched = [g for g in groups if g in group_modes]
        if any(group_modes.get(g) == "Included" for g in matched):
            item = dict(prism); item["scope_status"] = "Included"; item["scope_groups"] = matched
            out.append(item)
        elif not matched and include_unassigned:
            item = dict(prism); item["scope_status"] = "Included (unassigned)"; item["scope_groups"] = []
            out.append(item)
    return out


def scope_summary(state: Dict[str, Any]) -> str:
    groups = state.get("groups") or {}
    inc = [g for g, mode in groups.items() if mode == "Included"]
    return ", ".join(inc) if inc else "Whole project"


def project_scope_page(app: Any, workspace: Dict[str, Any]) -> None:
    wid = int(workspace["id"])
    base_prisms = app._pb_scope_base_build_precision_prisms(wid) if hasattr(app, "_pb_scope_base_build_precision_prisms") else []
    detected = detect_scope_groups(app, wid, base_prisms)
    state = load_scope(app, wid)

    app.st.markdown("### Select project scope")
    app.st.caption(
        "Choose exactly which building/block is being priced before PlanReader reads geometry. "
        "Reference-only and excluded buildings remain available in the drawing set but do not contribute to reconstructed geometry or measured m²."
    )

    enabled = app.st.toggle("Limit this project to selected buildings / stages", value=bool(state.get("enabled")), key=f"scope_enabled_{wid}")
    if not detected:
        app.st.info("No Block / Building / Tower / Stage labels have been detected yet. Upload/index the drawings first, or add a scope group manually below.")

    groups = list(detected)
    for existing in (state.get("groups") or {}):
        if existing not in groups:
            groups.append(existing)

    updated: Dict[str, str] = {}
    if groups:
        app.st.markdown("#### Detected scope groups")
        for idx, group in enumerate(sorted(groups)):
            cols = app.st.columns([2, 2, 4])
            cols[0].markdown(f"**{group}**")
            current = str((state.get("groups") or {}).get(group) or ("Included" if len(groups) == 1 else "Reference only"))
            updated[group] = cols[1].selectbox("Scope", MODES, index=MODES.index(current) if current in MODES else 1, key=f"scope_mode_{wid}_{idx}", label_visibility="collapsed")
            cols[2].caption("Measured + reconstructed" if updated[group] == "Included" else ("Drawing evidence only" if updated[group] == "Reference only" else "Ignored by scope engine"))

    add_cols = app.st.columns([3, 1])
    manual = add_cols[0].text_input("Add scope group manually", placeholder="e.g. Block B", key=f"scope_manual_{wid}")
    if add_cols[1].button("Add", key=f"scope_add_{wid}") and manual.strip():
        group = normalise_group(manual)
        state.setdefault("groups", {})[group] = "Included"
        save_scope(app, wid, state)
        app.st.rerun()

    include_unassigned = app.st.checkbox(
        "Include geometry that cannot be assigned to a named block/building",
        value=bool(state.get("include_unassigned", False)),
        help="Leave this off for a strict Block B-only tender. Turn it on only when shared/common geometry must also be priced.",
        key=f"scope_unassigned_{wid}",
    )

    preview_state = {"enabled": enabled, "groups": updated or state.get("groups", {}), "include_unassigned": include_unassigned}
    filtered = filter_prisms(base_prisms, preview_state)
    c1, c2, c3 = app.st.columns(3)
    c1.metric("Detected groups", len(groups))
    c2.metric("Included floor polygons", len(filtered))
    c3.metric("Active scope", scope_summary(preview_state))

    if enabled and not [g for g, m in preview_state["groups"].items() if m == "Included"]:
        app.st.error("Select at least one scope group as Included before continuing to Read Project.")

    if app.st.button("Save project scope", type="primary", use_container_width=True, key=f"scope_save_{wid}"):
        save_scope(app, wid, preview_state)
        app.st.success(f"Project scope saved: {scope_summary(preview_state)}")
        app.st.rerun()


def apply(app: Any) -> None:
    if getattr(app, "_pb_project_scope_v142_applied", False):
        return
    app._pb_project_scope_v142_applied = True
    base_build = app.build_precision_prisms
    app._pb_scope_base_build_precision_prisms = base_build

    def _scoped_build(workspace_id: int):
        prisms = base_build(int(workspace_id))
        state = load_scope(app, int(workspace_id))
        return filter_prisms(prisms, state)

    app.build_precision_prisms = _scoped_build
    app.project_scope_page = lambda workspace: project_scope_page(app, workspace)
    app.project_scope_state = lambda workspace_id: load_scope(app, int(workspace_id))
    app.project_scope_summary = lambda workspace_id: scope_summary(load_scope(app, int(workspace_id)))
