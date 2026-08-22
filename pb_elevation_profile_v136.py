"""PlanReader v1.3.6 elevation height/profile evidence solver.

Uses elevation/section text and registered side widths to solve vertical geometry
without allowing guessed heights to become verified. RL differences and explicit
vertical dimensions outrank defaults.
"""
from __future__ import annotations

import json, math, re
from typing import Any, Dict, Iterable, List

VERSION = "1.3.6"
SETTING_KEY = "elevation_profiles_v136"
_RL_RE = re.compile(r"\b(?:RL|AHD)\s*[:=]?\s*(-?\d{1,3}(?:\.\d{1,3})?)\b", re.I)
_DIM_RE = re.compile(r"(?<![:\d])(\d{3,5}(?:\.\d+)?)\s*(mm|m)?(?!\s*[:\d])", re.I)


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v); return x if math.isfinite(x) else default
    except Exception:
        return default


def rl_values(text: Any) -> List[float]:
    return sorted(set(round(_num(m.group(1)), 4) for m in _RL_RE.finditer(str(text or ""))))


def vertical_dimension_candidates(text: Any) -> List[float]:
    out=[]
    for m in _DIM_RE.finditer(str(text or "")):
        raw=_num(m.group(1)); unit=(m.group(2) or "").lower()
        if unit=="mm" or (not unit and raw>=100): raw/=1000.0
        elif unit!="m": continue
        if 1.8 <= raw <= 12.0: out.append(round(raw,4))
    return sorted(set(out))


def solve_height_from_text(text: Any, default_height: float = 2.7) -> Dict[str, Any]:
    rls=rl_values(text); dims=vertical_dimension_candidates(text)
    rl_diffs=[]
    for i,a in enumerate(rls):
        for b in rls[i+1:]:
            d=round(abs(b-a),4)
            if 1.8 <= d <= 12.0: rl_diffs.append(d)
    candidates=sorted(set(rl_diffs+dims))
    if rl_diffs:
        # Prefer the smallest credible storey-height RL difference.
        h=min(rl_diffs, key=lambda x: abs(x-3.0))
        return {"height_m":h,"status":"Verified from RL difference","confidence":"Verified","rls":rls,"dimensions":dims}
    if dims:
        h=min(dims, key=lambda x: abs(x-3.0))
        return {"height_m":h,"status":"Measured from elevation dimension","confidence":"High","rls":rls,"dimensions":dims}
    return {"height_m":round(max(.5,_num(default_height,2.7)),3),"status":"Default height; elevation evidence unresolved","confidence":"Review","rls":rls,"dimensions":dims}


def build_profiles(app: Any, workspace_id: int) -> Dict[str, Any]:
    reg=app.register_elevations_v135(int(workspace_id)) if hasattr(app,"register_elevations_v135") else {"elevations":[]}
    default=max(.5,_num(app.workspace_setting(int(workspace_id),"default_wall_height_m",2.7),2.7))
    pages={int(r["id"]):dict(r) for r in app.lquery("SELECT id,extracted_text,page_label,page_type FROM pages WHERE workspace_id=?",(int(workspace_id),))}
    profiles=[]
    for item in reg.get("elevations") or []:
        page=pages.get(int(item.get("page_id") or 0),{})
        solved=solve_height_from_text(page.get("extracted_text"),default)
        profiles.append({"side":item.get("orientation"),"page_id":item.get("page_id"),"page_label":item.get("page_label"),**solved})
    payload={"version":VERSION,"profiles":profiles}
    app.set_workspace_setting(int(workspace_id),SETTING_KEY,json.dumps(payload,separators=(",",":")))
    return payload


def height_by_side(app: Any, workspace_id: int) -> Dict[str, Dict[str, Any]]:
    payload=build_profiles(app,workspace_id)
    return {str(p.get("side")):p for p in payload.get("profiles") or [] if p.get("side")}


def apply(app: Any) -> None:
    if getattr(app,"_pb_elevation_profile_v136_applied",False): return
    app._pb_elevation_profile_v136_applied=True
    app.solve_elevation_height_v136=solve_height_from_text
    app.elevation_profiles_v136=lambda wid: build_profiles(app,int(wid))
    app.elevation_height_by_side_v136=lambda wid: height_by_side(app,int(wid))
