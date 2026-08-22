"""PlanReader v1.3.8 registered substrate assignment.

Maps already-resolved take-off/material evidence onto registered facade sides and
wall refs. Geometry and semantic evidence remain separate; ambiguous mixed
facades stay Review rather than receiving invented percentage splits.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

VERSION="1.3.8"


def _text(*values:Any)->str:
    return " ".join(str(v or "") for v in values).lower()


def evidence_for_side(app:Any,workspace_id:int,side:str)->List[Dict[str,Any]]:
    rows=app.lquery("SELECT element,location,substrate,finish_system,source_page,source_reference,confidence,notes FROM takeoff_rows WHERE workspace_id=?",(int(workspace_id),))
    out=[]
    needle=side.lower()
    for row in rows:
        hay=_text(row.get("location"),row.get("source_page"),row.get("source_reference"),row.get("notes"))
        substrate=str(row.get("substrate") or "").strip()
        if needle in hay and substrate and substrate.lower() not in {"unknown","other","to confirm","tbc"}:
            out.append(dict(row))
    return out


def assign_substrates(app:Any,workspace_id:int,walls:Iterable[Dict[str,Any]])->List[Dict[str,Any]]:
    result=[]
    cache={}
    for raw in walls or []:
        wall=dict(raw); side=str(wall.get("side") or "")
        if side not in cache: cache[side]=evidence_for_side(app,workspace_id,side)
        evidence=cache[side]
        # Exact wall reference in source text wins; otherwise a single side-wide
        # material can be applied. Multiple side materials require zoning/review.
        exact=[e for e in evidence if str(wall.get("wall_ref") or "").lower() in _text(e.get("location"),e.get("source_reference"),e.get("notes"))]
        choices=exact or evidence
        unique={str(e.get("substrate") or "").strip() for e in choices if str(e.get("substrate") or "").strip()}
        if len(unique)==1:
            substrate=next(iter(unique)); status="Resolved from drawing/take-off evidence"; confidence="High"
        elif len(unique)>1:
            substrate="Mixed / zone required"; status="Multiple substrates on elevation; boundary review required"; confidence="Review"
        else:
            substrate="To confirm"; status="No resolved substrate evidence for registered wall"; confidence="Review"
        wall.update({"substrate":substrate,"substrate_status":status,"substrate_confidence":confidence,
                     "substrate_evidence_count":len(choices)})
        result.append(wall)
    return result


def apply(app:Any)->None:
    if getattr(app,"_pb_registered_substrates_v138_applied",False): return
    app._pb_registered_substrates_v138_applied=True
    app.registered_substrates_v138=lambda wid,walls: assign_substrates(app,int(wid),walls)
