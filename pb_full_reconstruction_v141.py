"""PlanReader v1.4.1 full reconstruction QA surface.

Combines registered walls, opening holes, substrate evidence and roof envelope in
one estimator-facing model and can sync the exact wall objects to take-off rows.
"""
from __future__ import annotations

from typing import Any, Dict, List

VERSION="1.4.1"
SOURCE_PREFIX=f"PB Unified Building v1.3.9"


def _values(app:Any,wid:int,row:Dict[str,Any]):
    return (wid,row.get("section","External"),row.get("element",""),row.get("location",""),row.get("substrate","To confirm"),row.get("finish_system","To be confirmed"),row.get("quantity",0),row.get("unit","m²"),row.get("quantity_status","Provisional measured"),row.get("source_page",""),row.get("source_reference",""),row.get("inclusion_status","INCLUSION"),row.get("coats",0),row.get("coverage_m2_per_litre",0),row.get("productivity_m2_per_hour",0),row.get("rate_per_unit",0),row.get("confidence","Derived"),row.get("notes",""),"registered_external_wall",app.now_stamp(),app.now_stamp())


def sync_rows(app:Any,workspace_id:int,walls:List[Dict[str,Any]])->int:
    rows=app.registered_wall_takeoff_rows_v139(walls)
    conn=app.local_connect()
    try:
        conn.execute("DELETE FROM takeoff_rows WHERE workspace_id=? AND source_reference LIKE ?",(int(workspace_id),SOURCE_PREFIX+"%"))
        sql="""INSERT INTO takeoff_rows(workspace_id,section,element,location,substrate,finish_system,quantity,unit,quantity_status,source_page,source_reference,inclusion_status,coats,coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,notes,row_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        conn.executemany(sql,(_values(app,int(workspace_id),r) for r in rows)); conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    return len(rows)


def panel(app:Any,workspace:Dict[str,Any])->None:
    wid=int(workspace["id"]); walls=app.build_registered_walls_v139(wid)
    app.st.markdown("## Reconstructed building")
    app.st.caption("This is the unified geometry path: calibrated plan lengths + registered elevation heights + selectable openings + drawing/schedule substrate evidence. The same wall objects create the 3D surfaces and external wall take-off.")
    if not walls:
        app.st.warning("No registered walls yet. Review the floor-plan/elevation registration above."); return
    gross=sum(float(w.get("gross_m2") or 0) for w in walls); deductions=sum(float(w.get("opening_deduction_m2") or 0) for w in walls); net=sum(float(w.get("net_m2") or 0) for w in walls)
    c1,c2,c3,c4=app.st.columns(4); c1.metric("Registered walls",len(walls)); c2.metric("Gross",f"{gross:,.2f} m²"); c3.metric("Selected deductions",f"{deductions:,.2f} m²"); c4.metric("Net",f"{net:,.2f} m²")
    xray=app.st.toggle("X-ray",value=False,key=f"full_recon_xray_{wid}")
    fig=app.registered_building_figure_v139(walls,xray=xray)
    if hasattr(app,"roof_caps_v140") and hasattr(app,"add_roof_traces_v140"):
        caps=app.roof_caps_v140(wid,walls); fig=app.add_roof_traces_v140(fig,caps,xray=xray)
        roof=app.roof_evidence_v140(wid)
        if roof.get("confidence")=="Review": app.st.warning(str(roof.get("status")))
        else: app.st.success(str(roof.get("status")))
    app.st.plotly_chart(fig,use_container_width=True,key=f"full_recon_fig_{wid}")
    unresolved=[w for w in walls if w.get("height_confidence")=="Review" or w.get("substrate_confidence")=="Review"]
    if unresolved: app.st.warning(f"{len(unresolved)} wall(s) still contain height/substrate evidence that needs review. They remain Derived/Provisional in take-off.")
    else: app.st.success("Registered wall geometry and substrate evidence are resolved for all walls.")
    if app.st.button("Sync reconstructed walls to take-off",type="primary",use_container_width=True,key=f"sync_recon_{wid}"):
        count=sync_rows(app,wid,walls); app.st.success(f"Synced {count} registered wall rows from the same geometry shown above."); app.st.rerun()


def apply(app:Any)->None:
    if getattr(app,"_pb_full_reconstruction_v141_applied",False): return
    app._pb_full_reconstruction_v141_applied=True
    base=app.model_3d_page
    def _model(workspace,session_api_key="",ai_provider="OpenAI"):
        panel(app,workspace)
        app.st.divider()
        return base(workspace,session_api_key,ai_provider)
    app.sync_registered_wall_takeoff_v141=lambda wid,walls: sync_rows(app,int(wid),walls)
    app.model_3d_page=_model
