"""PlanReader v1.3.7 opening geometry attachment.

Attaches opening records to registered wall objects and derives reviewable
positions from explicit labels/wall refs. It never invents deduction policy.
"""
from __future__ import annotations

import json, math
from typing import Any, Dict, List

VERSION="1.3.7"
SETTING_KEY="opening_geometry_v137"


def _num(v:Any,default:float=0.0)->float:
    try:
        x=float(v); return x if math.isfinite(x) else default
    except Exception:return default


def attach_openings(app:Any,workspace_id:int,walls:List[Dict[str,Any]])->List[Dict[str,Any]]:
    try:
        raw=app.workspace_setting(int(workspace_id),"opening_register_v134","[]")
        openings=json.loads(str(raw or "[]"))
        if not isinstance(openings,list): openings=[]
    except Exception: openings=[]
    by_ref={str(w.get("wall_ref")):w for w in walls}
    attached=[]
    for raw in openings:
        item=app.normalise_opening(raw) if hasattr(app,"normalise_opening") else dict(raw)
        wall_ref=str(item.get("wall_ref") or "")
        wall=by_ref.get(wall_ref)
        if not wall and wall_ref in {"North","East","South","West"}:
            side_walls=[w for w in walls if w.get("side")==wall_ref]
            wall=max(side_walls,key=lambda w:_num(w.get("length_m")),default=None)
        length=_num(wall.get("length_m")) if wall else 0.0
        width=_num(item.get("width_m")); height=_num(item.get("height_m"))
        # Without explicit positional evidence, centre placement is visual-only.
        x0=max(0.0,(length-width)/2.0) if length else 0.0
        attached.append({**item,
            "resolved_wall_ref":str(wall.get("wall_ref")) if wall else "",
            "offset_m":round(x0,3),"sill_m":0.0 if str(item.get("kind")).lower().startswith("door") else 0.9,
            "geometry_status":"Visual centre placement; position needs elevation evidence" if wall else "Unassigned opening",
            "geometry_confidence":"Review",
            "area_m2":round(width*height*max(1,int(_num(item.get("quantity"),1))),4)})
    app.set_workspace_setting(int(workspace_id),SETTING_KEY,json.dumps(attached,separators=(",",":")))
    return attached


def apply(app:Any)->None:
    if getattr(app,"_pb_opening_geometry_v137_applied",False): return
    app._pb_opening_geometry_v137_applied=True
    app.attach_openings_v137=lambda wid,walls: attach_openings(app,int(wid),walls)
