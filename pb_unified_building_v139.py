"""PlanReader v1.3.9 unified registered building model.

Builds the visible external wall model and external wall take-off from the same
registered wall objects. Door/window rectangles are cut from wall meshes when
attached to a wall; deduction policy still comes from the estimator checkbox.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import plotly.graph_objects as go

VERSION="1.3.9"


def _num(v:Any,default:float=0.0)->float:
    try:
        x=float(v); return x if math.isfinite(x) else default
    except Exception:return default


def _wall_basis(wall:Dict[str,Any])->Tuple[Tuple[float,float],Tuple[float,float],float]:
    a=wall.get("a") or [0,0]; b=wall.get("b") or [0,0]
    ax,ay=float(a[0]),float(a[1]); bx,by=float(b[0]),float(b[1])
    length=math.hypot(bx-ax,by-ay)
    if length<=1e-9:return ((ax,ay),(1.0,0.0),0.0)
    return ((ax,ay),((bx-ax)/length,(by-ay)/length),length)


def _opening_rects(openings:Iterable[Dict[str,Any]],wall_ref:str,width:float,height:float)->List[Tuple[float,float,float,float]]:
    rects=[]
    for o in openings or []:
        if str(o.get("resolved_wall_ref") or o.get("wall_ref") or "")!=wall_ref: continue
        ow=max(0.0,_num(o.get("width_m"))); oh=max(0.0,_num(o.get("height_m")))
        x0=max(0.0,min(width,_num(o.get("offset_m"),(width-ow)/2.0))); z0=max(0.0,_num(o.get("sill_m"),0.0))
        x1=min(width,x0+ow); z1=min(height,z0+oh)
        if x1>x0 and z1>z0: rects.append((x0,x1,z0,z1))
    return rects


def wall_cells(width:float,height:float,openings:Iterable[Tuple[float,float,float,float]])->List[Tuple[float,float,float,float]]:
    """Grid-subdivide wall and remove cells whose centres fall in opening rects."""
    rects=list(openings or [])
    xs={0.0,width}; zs={0.0,height}
    for x0,x1,z0,z1 in rects:
        xs.update([max(0.0,min(width,x0)),max(0.0,min(width,x1))]); zs.update([max(0.0,min(height,z0)),max(0.0,min(height,z1))])
    xs=sorted(xs); zs=sorted(zs); cells=[]
    for xa,xb in zip(xs,xs[1:]):
        for za,zb in zip(zs,zs[1:]):
            if xb-xa<=1e-6 or zb-za<=1e-6: continue
            cx,cz=(xa+xb)/2.0,(za+zb)/2.0
            if any(x0<=cx<=x1 and z0<=cz<=z1 for x0,x1,z0,z1 in rects): continue
            cells.append((xa,xb,za,zb))
    return cells


def build_registered_walls(app:Any,workspace_id:int)->List[Dict[str,Any]]:
    reg=app.register_elevations_v135(int(workspace_id))
    profiles=app.elevation_height_by_side_v136(int(workspace_id)) if hasattr(app,"elevation_height_by_side_v136") else {}
    base=app.registered_wall_records_v135(int(workspace_id))
    segments={s.get("wall_ref"):s for side in (reg.get("facades") or {}).values() for s in side.get("segments") or []}
    walls=[]
    for row in base:
        side=str(row.get("side") or ""); seg=segments.get(row.get("wall_ref"),{})
        profile=profiles.get(side,{})
        height=max(.5,_num(profile.get("height_m"),row.get("height_m") or 2.7))
        wall={**row,**{"a":seg.get("a"),"b":seg.get("b"),"height_m":round(height,3),
             "height_status":profile.get("status") or row.get("height_status"),"height_confidence":profile.get("confidence") or "Review"}}
        walls.append(wall)
    walls=app.registered_substrates_v138(int(workspace_id),walls) if hasattr(app,"registered_substrates_v138") else walls
    openings=app.attach_openings_v137(int(workspace_id),walls) if hasattr(app,"attach_openings_v137") else []
    by_ref={}
    for o in openings: by_ref.setdefault(str(o.get("resolved_wall_ref") or ""),[]).append(o)
    for wall in walls:
        attached=by_ref.get(str(wall.get("wall_ref")),[])
        gross=_num(wall.get("length_m"))*_num(wall.get("height_m"))
        deducted=sum(_num(o.get("area_m2")) for o in attached if bool(o.get("deduct",True)))
        wall["gross_m2"]=round(gross,3); wall["opening_deduction_m2"]=round(deducted,3); wall["net_m2"]=round(max(0.0,gross-deducted),3); wall["openings"]=attached
    return walls


def build_figure(walls:Sequence[Dict[str,Any]],xray:bool=False)->go.Figure:
    fig=go.Figure()
    for wall in walls:
        origin,u,width=_wall_basis(wall); height=_num(wall.get("height_m"),2.7)
        if width<=0 or height<=0: continue
        rects=_opening_rects(wall.get("openings") or [],str(wall.get("wall_ref")),width,height)
        for xa,xb,za,zb in wall_cells(width,height,rects):
            pts=[]
            for x,z in ((xa,za),(xb,za),(xb,zb),(xa,zb)):
                pts.append((origin[0]+u[0]*x,origin[1]+u[1]*x,z))
            xs=[p[0] for p in pts]; ys=[p[1] for p in pts]; zs=[p[2] for p in pts]
            hover=(f"<b>{wall.get('wall_ref')} · {wall.get('side')}</b><br>Length {width:.2f} m<br>Height {height:.2f} m<br>"
                   f"Substrate: {wall.get('substrate')}<br>Gross: {_num(wall.get('gross_m2')):.2f} m²<br>"
                   f"Deductions: {_num(wall.get('opening_deduction_m2')):.2f} m²<br>Net: {_num(wall.get('net_m2')):.2f} m²<extra></extra>")
            fig.add_trace(go.Mesh3d(x=xs,y=ys,z=zs,i=[0,0],j=[1,2],k=[2,3],opacity=.35 if xray else .88,flatshading=True,name=str(wall.get("wall_ref")),hovertemplate=hover,showscale=False))
        # Opening outlines make the holes selectable/visible even when not deducted.
        for o,(x0,x1,z0,z1) in zip(wall.get("openings") or [],rects):
            outline=[(x0,z0),(x1,z0),(x1,z1),(x0,z1),(x0,z0)]
            fig.add_trace(go.Scatter3d(x=[origin[0]+u[0]*x for x,z in outline],y=[origin[1]+u[1]*x for x,z in outline],z=[z for x,z in outline],mode="lines",line=dict(width=5),name=str(o.get("label") or o.get("kind") or "Opening"),hovertemplate=f"<b>{o.get('label') or o.get('kind')}</b><br>{'Deducted' if o.get('deduct',True) else 'Not deducted'}<extra></extra>"))
    fig.update_layout(height=760,margin=dict(l=0,r=0,t=10,b=0),showlegend=False,scene=dict(aspectmode="data",xaxis_title="X (m)",yaxis_title="Y (m)",zaxis_title="Height (m)",camera=dict(eye=dict(x=1.5,y=-1.5,z=1.0))))
    return fig


def takeoff_rows(walls:Iterable[Dict[str,Any]])->List[Dict[str,Any]]:
    rows=[]
    for w in walls or []:
        rows.append({"section":"External","element":f"Registered external wall · {w.get('substrate') or 'To confirm'}","location":f"{w.get('side')} · {w.get('wall_ref')}","substrate":w.get("substrate") or "To confirm","finish_system":"To be confirmed","quantity":round(_num(w.get("net_m2")),2),"unit":"m²","quantity_status":"Measured plan length + registered elevation height" if w.get("height_confidence") in {"Verified","High"} else "Provisional measured","source_page":"Registered plan/elevation geometry","source_reference":f"PB Unified Building v{VERSION} · {w.get('wall_ref')}","inclusion_status":"INCLUSION","coats":0,"coverage_m2_per_litre":0,"productivity_m2_per_hour":0,"rate_per_unit":0,"confidence":"Measured" if w.get("height_confidence")=="Verified" and w.get("substrate_confidence")!="Review" else "Derived","notes":f"Gross {_num(w.get('gross_m2')):.2f} m²; selected opening deductions {_num(w.get('opening_deduction_m2')):.2f} m². {w.get('height_status')}. {w.get('substrate_status')}."})
    return rows


def panel(app:Any,workspace:Dict[str,Any])->None:
    wid=int(workspace["id"]); walls=build_registered_walls(app,wid)
    app.st.markdown("### Reconstructed building · unified geometry")
    app.st.caption("The wall surfaces below are the same objects used to calculate external wall m². Openings are cut from those surfaces; the estimator deduction checkbox controls whether each opening reduces net m².")
    if not walls:
        app.st.warning("No registered external walls are available yet."); return
    c1,c2,c3,c4=app.st.columns(4)
    c1.metric("Walls",len(walls)); c2.metric("Gross wall m²",f"{sum(_num(w.get('gross_m2')) for w in walls):,.2f}"); c3.metric("Opening deductions",f"{sum(_num(w.get('opening_deduction_m2')) for w in walls):,.2f}"); c4.metric("Net wall m²",f"{sum(_num(w.get('net_m2')) for w in walls):,.2f}")
    xray=app.st.toggle("X-ray reconstructed walls",value=False,key=f"unified_xray_{wid}")
    app.st.plotly_chart(build_figure(walls,xray=xray),use_container_width=True,key=f"unified_building_{wid}")
    app.st.dataframe(app.pd.DataFrame([{k:v for k,v in w.items() if k not in {"openings","a","b"}} for w in walls]),use_container_width=True,hide_index=True)


def apply(app:Any)->None:
    if getattr(app,"_pb_unified_building_v139_applied",False): return
    app._pb_unified_building_v139_applied=True
    base=app.model_3d_page
    def _model(workspace,session_api_key="",ai_provider="OpenAI"):
        with app.st.expander("Unified reconstructed building",expanded=True): panel(app,workspace)
        return base(workspace,session_api_key,ai_provider)
    app.build_registered_walls_v139=lambda wid: build_registered_walls(app,int(wid))
    app.registered_building_figure_v139=build_figure
    app.registered_wall_takeoff_rows_v139=takeoff_rows
    app.model_3d_page=_model
