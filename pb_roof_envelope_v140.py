"""PlanReader v1.4.0 conservative roof/parapet envelope.

Creates roof-cap geometry from calibrated floor polygons and explicit roof/parapet
text evidence. Unknown pitch/ridge geometry remains Review instead of fabricated.
"""
from __future__ import annotations

import math, re
from typing import Any, Dict, List
import plotly.graph_objects as go

VERSION="1.4.0"
_PITCH_RE=re.compile(r"\b(?:roof\s*)?pitch\s*[:=]?\s*(\d{1,2}(?:\.\d+)?)\s*(?:deg|°)?",re.I)


def _num(v:Any,default:float=0.0)->float:
    try:
        x=float(v); return x if math.isfinite(x) else default
    except Exception:return default


def roof_evidence(app:Any,workspace_id:int)->Dict[str,Any]:
    rows=app.lquery("SELECT page_label,page_type,extracted_text FROM pages WHERE workspace_id=?",(int(workspace_id),))
    text="\n".join(str(r.get("extracted_text") or "") for r in rows if any(k in (str(r.get("page_type") or "")+str(r.get("page_label") or "")).lower() for k in ("roof","elevation","section")))
    pitches=sorted(set(round(_num(m.group(1)),2) for m in _PITCH_RE.finditer(text) if 0<_num(m.group(1))<60))
    parapet="parapet" in text.lower(); flat=any(k in text.lower() for k in ("flat roof","roof terrace","membrane roof"))
    if flat or parapet:
        status="Flat/parapet roof evidence identified"; confidence="High"
    elif len(pitches)==1:
        status="Roof pitch identified; ridge direction still requires roof-plan evidence"; confidence="Review"
    else:
        status="Roof profile unresolved; cap shown for envelope QA only"; confidence="Review"
    return {"pitches_deg":pitches,"parapet":parapet,"flat":flat,"status":status,"confidence":confidence}


def roof_caps(app:Any,workspace_id:int,walls:List[Dict[str,Any]])->List[Dict[str,Any]]:
    prisms=app.build_precision_prisms(int(workspace_id)) if hasattr(app,"build_precision_prisms") else []
    evidence=roof_evidence(app,workspace_id); max_h=max([_num(w.get("height_m"),2.7) for w in walls] or [2.7])
    caps=[]
    for p in prisms:
        pts=list(p.get("points") or []); tris=list(p.get("triangles") or [])
        if len(pts)>=3:
            caps.append({"points":pts,"triangles":tris,"z":max_h,"level":p.get("level_name"),**evidence})
    return caps


def add_roof_traces(fig:go.Figure,caps:List[Dict[str,Any]],xray:bool=False)->go.Figure:
    for cap in caps or []:
        pts=cap.get("points") or []; tris=cap.get("triangles") or []
        if not pts or not tris: continue
        i,j,k=zip(*tris)
        fig.add_trace(go.Mesh3d(x=[p[0] for p in pts],y=[p[1] for p in pts],z=[_num(cap.get("z"))]*len(pts),i=list(i),j=list(j),k=list(k),opacity=.22 if xray else .65,flatshading=True,name="Roof envelope",hovertemplate=f"<b>Roof envelope</b><br>{cap.get('status')}<br>Pitch evidence: {cap.get('pitches_deg')}<extra></extra>",showscale=False))
    return fig


def apply(app:Any)->None:
    if getattr(app,"_pb_roof_envelope_v140_applied",False): return
    app._pb_roof_envelope_v140_applied=True
    app.roof_evidence_v140=lambda wid: roof_evidence(app,int(wid))
    app.roof_caps_v140=lambda wid,walls: roof_caps(app,int(wid),walls)
    app.add_roof_traces_v140=add_roof_traces
