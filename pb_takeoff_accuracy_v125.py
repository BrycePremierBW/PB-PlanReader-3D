"""PlanReader v1.2.5 deterministic take-off accuracy hardening."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import pb_takeoff_v12 as v12

PB_ACCURACY_VERSION = "2026.08.13-2"
AUTO_SCALE: Dict[int, float] = {}
REVIEWED = {"verified", "reviewed", "checked", "confirmed", "estimator verified", "manual verified", "manually verified", "approved"}
ACTIVE = {"", "INCLUSION", "INCLUDED", "SEPARATE ITEM", "PROVISIONAL"}
FLOOR_RE = re.compile(r"\b(floor\s*area|internal\s*floor|floor\s*m(?:2|²)|gross\s*floor|net+t?\s*floor)\b", re.I)
ROLLUP_RE = re.compile(r"^\s*(?:(?:grand|sub)\s*)?totals?\b|^\s*(?:sum|average|base\s+totals)\b", re.I)


def clean(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def norm(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(v).lower()).strip()


def schema(app: Any) -> None:
    conn = app.local_connect()
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "takeoff_rows" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(takeoff_rows)")}
            for name, ddl in {
                "ai_baseline_quantity": "REAL", "pre_map_quantity": "REAL",
                "pre_map_quantity_status": "TEXT", "origin": "TEXT DEFAULT ''",
            }.items():
                if name not in cols:
                    conn.execute(f"ALTER TABLE takeoff_rows ADD COLUMN {name} {ddl}")
            conn.execute("UPDATE takeoff_rows SET unit='m²' WHERE LOWER(TRIM(COALESCE(unit,''))) IN ('m2','sqm','sq m')")
            conn.execute("UPDATE takeoff_rows SET unit='lm' WHERE LOWER(TRIM(COALESCE(unit,''))) IN ('m','lin m','lineal m','linear m')")
            conn.execute("""UPDATE takeoff_rows SET element='Floor area',row_role='floor_area',rate_per_unit=0,
                           coats=0,coverage_m2_per_litre=0,productivity_m2_per_hour=0
                           WHERE LOWER(TRIM(COALESCE(section,'')))='internal'
                           AND LOWER(TRIM(COALESCE(element,'')))='floor plan'
                           AND LOWER(COALESCE(location,'')) LIKE '%floor area%'
                           AND LOWER(COALESCE(notes,'')) LIKE '%auto-detected%'""")
        if "pages" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(pages)")}
            if "scale_method" not in cols:
                conn.execute("ALTER TABLE pages ADD COLUMN scale_method TEXT DEFAULT ''")
            if "scale_verified" not in cols:
                conn.execute("ALTER TABLE pages ADD COLUMN scale_verified INTEGER DEFAULT 0")
        if "measurement_lines" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(measurement_lines)")}
            if "measurement_basis" not in cols:
                conn.execute("ALTER TABLE measurement_lines ADD COLUMN measurement_basis TEXT DEFAULT ''")
        conn.commit()
    finally:
        conn.close()


def level_of(v: Any) -> str:
    low = clean(v).lower()
    if re.search(r"\blower\s+ground\b|\blg\b", low): return "Lower Ground"
    m = re.search(r"\bbasement\s*([0-9]+)?\b|\bb\s*([0-9]+)\b", low)
    if m: return f"Basement {int(next((g for g in m.groups() if g), '1'))}"
    if re.search(r"\bground(?:\s+floor)?\b|\bgf\b", low): return "Ground"
    if re.search(r"\bmezz(?:anine)?\b", low): return "Mezzanine"
    m = re.search(r"\b(?:level|lvl)\s*[-:]?\s*([0-9]{1,2})\b", low) or re.search(r"\b([0-9]{1,2})(?:st|nd|rd|th)?\s+floor\b", low)
    if m: return f"Level {int(m.group(1))}"
    for word, n in {"first":1,"second":2,"third":3,"fourth":4,"fifth":5,"sixth":6,"seventh":7,"eighth":8,"ninth":9,"tenth":10}.items():
        if f"{word} floor" in low: return f"Level {n}"
    if "roof" in low: return "Roof"
    return "Unassigned"


def level_sort_key(v: str) -> float:
    if v.startswith("Basement"):
        m = re.search(r"\d+", v); return -100 + float(m.group() if m else 1)
    if v == "Lower Ground": return -10
    if v == "Ground": return 0
    if v == "Mezzanine": return .5
    m = re.match(r"Level\s+(\d+)", v)
    if m: return float(m.group(1))
    return 1000 if v == "Roof" else 2000


def scope_of(v: Any) -> str:
    text, low, parts = clean(v), clean(v).lower(), []
    for p in [r"\bunits?\s*[#:-]?\s*[a-z0-9]+", r"\b(?:apartment|apt|townhouse|villa|lot)\s*[#:-]?\s*[a-z0-9]+", r"\b(?:block|building|wing|stage)\s*[#:-]?\s*[a-z0-9]+"]:
        m = re.search(p, low, re.I)
        if m: parts.append(clean(m.group()).title())
    parts.append(level_of(text))
    return " | ".join(dict.fromkeys(parts))


def is_ai(row: Dict[str, Any]) -> bool:
    if clean(row.get("origin")).lower() == "ai": return True
    text = " ".join(clean(row.get(k)) for k in ("source_reference", "notes", "confidence")).lower()
    return any(x in text for x in ("ai draft", "ai plan review", "ai-generated", "ai generated"))


def mapped_ids(app: Any, wid: int) -> set[int]:
    return {int(r["takeoff_row_id"]) for r in app.lquery("SELECT DISTINCT takeoff_row_id FROM measurement_lines WHERE workspace_id=? AND takeoff_row_id IS NOT NULL", (wid,))}


def floor_rows(app: Any, df: pd.DataFrame) -> List[Dict[str, Any]]:
    if df.empty or "row_role" not in df.columns: return []
    wid = int(df.iloc[0]["workspace_id"]) if "workspace_id" in df.columns else 0
    mapped, chosen = mapped_ids(app, wid) if wid else set(), {}
    for row in df.to_dict("records"):
        if clean(row.get("row_role")) != "floor_area" or app._normalise_unit(row.get("unit")) != "m²": continue
        rid, conf, status = int(row.get("id") or 0), clean(row.get("confidence")).lower(), clean(row.get("quantity_status")).lower()
        rank = (5 if rid in mapped or status == "mapped" else 0, 3 if conf in REVIEWED else (1 if not is_ai(row) else 0), rid)
        key = scope_of(row.get("location"))
        if key not in chosen or rank > chosen[key][0]: chosen[key] = (rank, row)
    return [v[1] for v in chosen.values()]


def floor_by_scope(app: Any, df: pd.DataFrame) -> Dict[str, float]:
    return {scope_of(r.get("location")): max(0.0, app.to_float(r.get("quantity"))) for r in floor_rows(app, df)}


def floor_for_scope(floors: Dict[str, float], scope: str) -> float:
    if scope in floors: return floors[scope]
    level = scope.split(" | ")[-1]
    vals = [v for k,v in floors.items() if k.split(" | ")[-1] == level and v > 0]
    return vals[0] if len(vals) == 1 else 0.0


def dataframe_for_takeoff(app: Any, wid: int) -> pd.DataFrame:
    schema(app)
    df = app.ldf("SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id", (wid,))
    if df.empty: return df
    basis, floors = clean(app.workspace_setting(wid, "internal_pricing_basis", "wall_m2")).lower(), floor_by_scope(app, df)
    groups: Dict[str, List[int]] = {}
    if basis == "floor_m2":
        for idx,row in df.iterrows():
            if clean(row.get("row_role")) != "floor_area" and app._normalise_unit(row.get("unit")) == "m²" and app.is_internal_wall_row(row.get("section"), row.get("element")):
                groups.setdefault(scope_of(row.get("location")), []).append(int(idx))
    allocated: Dict[int,float] = {}
    for scope, idxs in groups.items():
        f = floor_for_scope(floors, scope)
        weights = [max(0.0, app.to_float(df.loc[i,"quantity"])) for i in idxs]; total = sum(weights)
        for i,w in zip(idxs,weights): allocated[i] = f if len(idxs)==1 else (f*w/total if f>0 and total>0 else 0.0)
    paints, hours, priced, labels, values = [],[],[],[],[]
    for idx,row in df.iterrows():
        q, unit, role = max(0.0, app.to_float(row.get("quantity"))), app._normalise_unit(row.get("unit")) or clean(row.get("unit")), clean(row.get("row_role"))
        if role == "floor_area": paints.append(0.0); hours.append(0.0); priced.append(q if unit=="m²" else 0.0); labels.append("Floor area (reference)"); values.append(0.0); continue
        paints.append(app.paint_litres(q, unit, app.to_float(row.get("coats"),2), app.to_float(row.get("coverage_m2_per_litre"),12)))
        hours.append(app.labour_hours(q, unit, app.to_float(row.get("productivity_m2_per_hour"),8)))
        pq = allocated.get(int(idx), q); priced.append(pq); labels.append("Floor m² allocated" if int(idx) in allocated and len(groups.get(scope_of(row.get("location")),[]))>1 else ("Floor m²" if int(idx) in allocated else "Quantity")); values.append(app.row_value(pq, app.to_float(row.get("rate_per_unit"))))
    df["paint_litres"],df["labour_hours"],df["priced_quantity"],df["pricing_basis"],df["value_ex_gst"] = paints,hours,priced,labels,values
    return df


def per_level_summary(app: Any, wid: int) -> pd.DataFrame:
    df = dataframe_for_takeoff(app,wid); cols=["level","rows","m2","floor_m2","lm","count","paint_litres","labour_hours","value_ex_gst"]
    if df.empty: return pd.DataFrame(columns=cols)
    work=app.takeoff_work_rows(df.copy()); work["level"]=[level_of(x) for x in work["location"]]
    fm: Dict[str,float]={}
    for r in floor_rows(app,df): fm[level_of(r.get("location"))]=fm.get(level_of(r.get("location")),0)+max(0.0,app.to_float(r.get("quantity")))
    out=[]
    for lvl in set(work["level"].tolist())|set(fm):
        g=work.loc[work["level"].eq(lvl)]
        out.append({"level":lvl,"rows":len(g),"m2":float(g.loc[g["unit"].eq("m²"),"quantity"].sum()),"floor_m2":fm.get(lvl,0.0),"lm":float(g.loc[g["unit"].eq("lm"),"quantity"].sum()),"count":float(g.loc[g["unit"].isin({"No.","item"}),"quantity"].sum()),"paint_litres":float(g["paint_litres"].sum()),"labour_hours":float(g["labour_hours"].sum()),"value_ex_gst":float(g["value_ex_gst"].sum())})
    r=pd.DataFrame(out,columns=cols); r["_sort"]=r["level"].map(level_sort_key); return r.sort_values(["_sort","level"]).drop(columns="_sort").reset_index(drop=True)


def actual_zoom(app: Any, page: Dict[str, Any]) -> float:
    ctx=dict(page); pid=int(page.get("id") or 0)
    if pid:
        rows=app.lquery("SELECT p.page_no,p.width_px,p.height_px,d.path FROM pages p JOIN documents d ON d.id=p.document_id WHERE p.id=?",(pid,))
        if rows: ctx.update(rows[0])
    path=Path(clean(ctx.get("path"))); w,h=max(0.0,app.to_float(ctx.get("width_px"))),max(0.0,app.to_float(ctx.get("height_px")))
    if app.fitz is not None and path.exists() and path.suffix.lower()==".pdf" and (w or h):
        doc=None
        try:
            doc=app.fitz.open(path); p=doc[max(0,app.to_int(ctx.get("page_no"),1)-1)]; vals=[]
            if w and p.rect.width: vals.append(w/float(p.rect.width))
            if h and p.rect.height: vals.append(h/float(p.rect.height))
            if vals: return sum(vals)/len(vals)
        except Exception: pass
        finally:
            if doc:
                try: doc.close()
                except Exception: pass
    return 1.7


def auto_scale(app: Any, page: Dict[str, Any]) -> Optional[Dict[str,Any]]:
    text,source=str(page.get("extracted_text") or ""),clean(page.get("scale_text")); m=app._SCALE_RATIO_RE.search(source) or app._SCALE_RATIO_RE.search(text) or app._SCALE_IN_RE.search(text)
    if not m: return None
    ratio=int(m.group(1));
    if not 10<=ratio<=2000: return None
    z=actual_zoom(app,page); px=z*1000/(0.352778*ratio); pid=int(page.get("id") or 0)
    if pid: AUTO_SCALE[pid]=px
    return {"ratio":ratio,"px_per_m":round(px,3),"source":m.group(0).strip(),"render_zoom":round(z,6)}


def geom(app: Any, line: Dict[str,Any]) -> Tuple[float,float,float]:
    w,h,ppm=map(lambda x:max(0.0,app.to_float(x)),(line.get("width_px"),line.get("height_px"),line.get("px_per_m")))
    if not w or not h or not ppm: return 0.0,0.0,0.0
    if clean(line.get("kind")).lower()!="polygon":
        dx=(app.to_float(line.get("x2"))-app.to_float(line.get("x1")))/100*w; dy=(app.to_float(line.get("y2"))-app.to_float(line.get("y1")))/100*h
        return math.hypot(dx,dy)/ppm,0.0,0.0
    pts=line.get("points") or []
    if isinstance(pts,str):
        try: pts=json.loads(pts)
        except Exception: pts=[]
    p=[(app.to_float(x[0])/100*w,app.to_float(x[1])/100*h) for x in pts if len(x)>=2]
    if len(p)<3: return 0.0,0.0,0.0
    area=app.polygon_area(p)/(ppm*ppm); per=sum(math.hypot(p[i][0]-p[(i+1)%len(p)][0],p[i][1]-p[(i+1)%len(p)][1]) for i in range(len(p)))/ppm
    return 0.0,area,per


def basis(line: Dict[str,Any]) -> str:
    b=clean(line.get("measurement_basis")).lower(); notes=clean(line.get("notes")).lower()
    if b: return b
    return "footprint_perimeter_height" if "auto-detected envelope" in notes or "footprint perimeter" in notes else "direct"


def measured_qty(app: Any,row:Dict[str,Any],lines:Sequence[Dict[str,Any]]) -> Tuple[float,int,int]:
    u=app.normalise_line_unit(row.get("unit")); height=max(.1,app.to_float(app.workspace_setting(int(row.get("workspace_id") or 0),"default_wall_height_m",2.7),2.7)); total=n=bad=0
    for ln in lines:
        kind="polygon" if clean(ln.get("kind")).lower()=="polygon" else "line"; length,area,per=geom(app,ln)
        if u=="m2":
            if kind!="polygon": bad+=1 if length>0 else 0; continue
            val=per*height if basis(ln)=="footprint_perimeter_height" else (-area if basis(ln)=="deduction" else area)
        elif u=="m": val=length if kind=="line" else per
        else: bad+=1 if length>0 or area>0 else 0; continue
        if abs(val)>0: total+=val; n+=1
    return round(max(0.0,total),3),n,bad


def capture_pre(app:Any,base_exec:Any,wid:int,ids:Iterable[int]) -> None:
    for rid in {int(x) for x in ids if x}:
        r=app.lquery("SELECT quantity,quantity_status,pre_map_quantity FROM takeoff_rows WHERE id=? AND workspace_id=?",(rid,wid))
        if r and r[0].get("pre_map_quantity") is None: base_exec("UPDATE takeoff_rows SET pre_map_quantity=?,pre_map_quantity_status=? WHERE id=?",(app.to_float(r[0].get("quantity")),clean(r[0].get("quantity_status")),rid))


def recompute(app:Any,base_exec:Any,wid:int,ids:Iterable[int]) -> Dict[int,float]:
    out={}; schema(app)
    for rid in {int(x) for x in ids if x}:
        rr=app.lquery("SELECT * FROM takeoff_rows WHERE id=? AND workspace_id=?",(rid,wid))
        if not rr: continue
        row=rr[0]; lines=app.lquery("SELECT ml.*,p.width_px,p.height_px,p.px_per_m FROM measurement_lines ml JOIN pages p ON p.id=ml.page_id WHERE ml.workspace_id=? AND ml.takeoff_row_id=? ORDER BY ml.id",(wid,rid))
        for ln in lines:
            l,a,p=geom(app,ln); base_exec("UPDATE measurement_lines SET length_m=?,area_m2=?,perimeter_m=? WHERE id=?",(round(l,3),round(a,3),round(p,3),ln["id"]))
        q,n,_=measured_qty(app,row,lines)
        if n:
            conf=clean(row.get("confidence")); conf=conf if conf.lower() in REVIEWED else "Measured"; base_exec("UPDATE takeoff_rows SET quantity=?,quantity_status='Mapped',confidence=?,updated_at=? WHERE id=?",(q,conf,app.now_stamp(),rid)); out[rid]=q
        else:
            q=row.get("pre_map_quantity"); q=row.get("ai_baseline_quantity") if q is None else q; q=max(0.0,app.to_float(q)); st=clean(row.get("pre_map_quantity_status")) or ("Provisional measured" if is_ai(row) and q else "To measure"); base_exec("UPDATE takeoff_rows SET quantity=?,quantity_status=?,updated_at=? WHERE id=?",(q,st,app.now_stamp(),rid)); out[rid]=q
    return out


def save_lines(app:Any,base_exec:Any,wid:int,pid:int,lines:Sequence[Dict[str,Any]]) -> Dict[str,Any]:
    schema(app); old={int(r["takeoff_row_id"]) for r in app.lquery("SELECT DISTINCT takeoff_row_id FROM measurement_lines WHERE page_id=? AND takeoff_row_id IS NOT NULL",(pid,))}; new={int(x.get("takeoff_row_id")) for x in lines if x.get("takeoff_row_id")}; capture_pre(app,base_exec,wid,new); base_exec("DELETE FROM measurement_lines WHERE page_id=?",(pid,)); saved=0
    for x in lines:
        rid=int(x.get("takeoff_row_id") or 0) or None; pts=x.get("points") or []; pts=json.dumps([[round(app.to_float(p[0]),3),round(app.to_float(p[1]),3)] for p in pts]) if isinstance(pts,(list,tuple)) else str(pts)
        base_exec("""INSERT INTO measurement_lines(workspace_id,page_id,takeoff_row_id,label,unit,colour,kind,x1,y1,x2,y2,points,length_m,area_m2,perimeter_m,quantity_status,moved,notes,measurement_basis,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(wid,pid,rid,clean(x.get("label")),app.normalise_line_unit(x.get("unit")),clean(x.get("colour")),"polygon" if clean(x.get("kind")).lower()=="polygon" else "line",app.to_float(x.get("x1")),app.to_float(x.get("y1")),app.to_float(x.get("x2")),app.to_float(x.get("y2")),pts,0,0,0,clean(x.get("quantity_status")) or "Mapped",1 if x.get("moved") else 0,clean(x.get("notes")),basis(x),app.now_stamp())); saved+=1
    updated=recompute(app,base_exec,wid,old|new); return {"saved":saved,"synced":sum(1 for v in updated.values() if v>0),"updated_rows":updated}


def guarded_exec(app:Any,base_exec:Any):
    def run(sql:str,params:Sequence[Any]=()):
        n=" ".join(str(sql).strip().lower().split()); p=tuple(params or ())
        if n.startswith("update pages set px_per_m=") and len(p)>=2:
            ppm,pid=max(0.0,app.to_float(p[0])),int(p[-1]); before=app.lquery("SELECT workspace_id,scale_method FROM pages WHERE id=?",(pid,)); ids={int(r["takeoff_row_id"]) for r in app.lquery("SELECT DISTINCT takeoff_row_id FROM measurement_lines WHERE page_id=? AND takeoff_row_id IS NOT NULL",(pid,))}; result=base_exec(sql,p); cand=AUTO_SCALE.get(pid); old=clean(before[0].get("scale_method")) if before else ""; auto=cand is not None and abs(ppm-cand)<=max(.001,abs(cand)*.0001)
            method,verified=("auto_detected",0) if auto and old!="auto_detected" else ("manual_calibration",1); base_exec("UPDATE pages SET scale_method=?,scale_verified=? WHERE id=?",(method,verified,pid));
            if before and ids: recompute(app,base_exec,int(before[0]["workspace_id"]),ids)
            return result
        if n.startswith("delete from measurement_lines where page_id=") and p:
            pid=int(p[0]); rows=app.lquery("SELECT workspace_id,takeoff_row_id FROM measurement_lines WHERE page_id=? AND takeoff_row_id IS NOT NULL",(pid,)); result=base_exec(sql,p); groups:Dict[int,set[int]]={}
            for r in rows: groups.setdefault(int(r["workspace_id"]),set()).add(int(r["takeoff_row_id"]))
            for wid,ids in groups.items(): recompute(app,base_exec,wid,ids)
            return result
        return base_exec(sql,p)
    return run


def mapper_row(app:Any,wid:int,section:str,element:str,location:str,substrate:str,finish:str,unit:str,source:str)->int:
    schema(app); role="floor_area" if FLOOR_RE.search(f"{element} {location}") else ""; u=app._normalise_unit(unit) or {"m2":"m²","m":"lm"}.get(app.normalise_line_unit(unit),clean(unit)); old=app.ldf("SELECT id FROM takeoff_rows WHERE workspace_id=? AND section=? AND element=? AND location=? AND unit=?",(wid,section,element,location,u))
    if not old.empty:
        rid=int(old.iloc[0]["id"])
        if role: app.lexecute("UPDATE takeoff_rows SET row_role='floor_area',rate_per_unit=0,coats=0,coverage_m2_per_litre=0,productivity_m2_per_hour=0 WHERE id=?",(rid,))
        return rid
    rate=0 if role else app.default_rate_for(substrate,element,finish,u)
    return app.lexecute("""INSERT INTO takeoff_rows(workspace_id,section,element,location,substrate,finish_system,quantity,unit,quantity_status,source_page,source_reference,inclusion_status,coats,coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,notes,row_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(wid,section,element,location,substrate,finish,0,u,"To measure",source,"","INCLUSION",0 if role else (3 if u=="m²" else 2),0 if role else 12,0 if role else 8,rate,"To review",f"Auto-detected from {source}.",role,app.now_stamp(),app.now_stamp()))


def auto_map(app:Any,wid:int,pid:int,ppm:float)->List[Dict[str,Any]]:
    rows=app.takeoff_rows_for_mapper(wid); pos=app._line_grid_positions(len(rows)); page=app.lquery("SELECT width_px FROM pages WHERE id=?",(pid,)); iw=int(page[0].get("width_px") or 1000) if page else 1000; out=[]
    for i,(r,(x1,y1,x2,y2)) in enumerate(zip(rows,pos)):
        u=app.normalise_line_unit(r.get("unit")); base={"id":f"auto_{i}","takeoff_row_id":int(r["id"]),"label":r.get("label"),"unit":u,"colour":r.get("colour"),"quantity_status":"Placeholder","moved":0}
        if u=="m2":
            c=(x1+x2)/2; h=8; base.update({"kind":"polygon","points":[[max(0,c-h),max(0,y1-h)],[min(100,c+h),max(0,y1-h)],[min(100,c+h),min(100,y1+h)],[max(0,c-h),min(100,y1+h)]],"notes":"Placeholder outline - draw the real area before saving."}); out.append(base)
        elif u=="m":
            if ppm>0 and iw>0: d=min(20,max(4,2*ppm/iw*100)); c=(x1+x2)/2; x1,x2=max(0,c-d/2),min(100,c+d/2)
            base.update({"kind":"line","x1":x1,"y1":y1,"x2":x2,"y2":y2,"points":[],"notes":"Placeholder line - draw the real length before saving."}); out.append(base)
    return out


def auto_envelope(app:Any,wid:int,page:Dict[str,Any],ppm:float,level:str)->List[Dict[str,Any]]:
    ds=app.auto_detect_building_envelope(str(page.get("image_path") or "")); out=[]
    if not ds:return out
    ext=mapper_row(app,wid,"External","External walls / cladding",f"{level} · external envelope","Render","Exterior acrylic","m²",page.get("page_label","")); flr=mapper_row(app,wid,"Internal","Floor area",f"{level} · floor area","Concrete floor","","m²",page.get("page_label","")); w,h=int(page.get("width_px") or 1000),int(page.get("height_px") or 1000); wall=max(.1,app.to_float(app.workspace_setting(wid,"default_wall_height_m",2.7),2.7))
    for i,d in enumerate(ds):
        pts=d["points"]; px=[(p[0]/100*w,p[1]/100*h) for p in pts]; area=app.polygon_area(px)/(ppm*ppm) if ppm>0 else 0; per=sum(math.hypot(px[j][0]-px[(j+1)%len(px)][0],px[j][1]-px[(j+1)%len(px)][1]) for j in range(len(px)))/ppm if ppm>0 else 0
        out += [{"id":f"auto_ext_{i}","takeoff_row_id":ext,"label":"External envelope (auto)","unit":"m2","kind":"polygon","points":pts,"area_m2":area,"perimeter_m":per,"moved":1,"measurement_basis":"footprint_perimeter_height","notes":f"Auto-detected envelope · footprint perimeter × {wall:g} m wall height; verify openings/net area."},{"id":f"auto_floor_{i}","takeoff_row_id":flr,"label":"Floor area (auto)","unit":"m2","kind":"polygon","points":pts,"area_m2":area,"perimeter_m":per,"moved":1,"measurement_basis":"direct","notes":"Auto-detected floor area - adjust to the real internal floor footprint."}]
    return out


def parse_file(app:Any):
    def parse(upload:Any,mapping:Optional[Dict[int,str]]=None,raw_headers:Optional[List[str]]=None,body:Optional[List[List[Any]]]=None):
        name=clean(getattr(upload,"name","takeoff")); warnings=[]
        if raw_headers is None or body is None: raw_headers,body,*_=app.detect_takeoff_columns(upload)
        if mapping is None:
            mapping={}; used=[]
            for i,h in enumerate(raw_headers):
                t=app._match_takeoff_header(h)
                if t=="quantity" or (t and t not in used): mapping[i]=t; used.append(t)
        metrics,direct=v12._metric_columns(raw_headers),v12._direct_indices(raw_headers); has_metrics=any(metrics.values()); rows=[]
        def add(base,q,u,role="",src=""):
            r=dict(base); r.update(quantity=max(0.0,q),unit=app._normalise_unit(u) or u,row_role=role,quantity_status=clean(r.get("quantity_status")) or ("Measured" if q>0 else "To measure"),inclusion_status=clean(r.get("inclusion_status")) or "INCLUSION"); r=v12._normalise_row(app,r); r["row_role"]=role
            if role: r.update(element="Floor area",unit="m²",rate_per_unit=0.0,coats=0.0,coverage_m2_per_litre=0.0,productivity_m2_per_hour=0.0)
            if src:r["notes"]=f"{clean(r.get('notes'))} · Imported quantity channel: {src}".strip(" ·")
            rows.append({c:r.get(c,"") for c in app.TAKEOFF_COLUMNS+["row_role"]})
        for rn,line in enumerate(body,start=2):
            if not any(clean(x).lower() not in {"","nan","none"} for x in line):continue
            base={c:"" for c in app.TAKEOFF_COLUMNS}; floor=0.0
            for i,t in mapping.items():
                if i>=len(line):continue
                if t=="floor_area": floor=max(floor,max(0.0,v12._to_float(app,line[i])));continue
                if t not in app.TAKEOFF_COLUMNS:continue
                if t=="quantity" and not has_metrics:base[t]=app._parse_qty(line[i])
                elif t=="unit":base[t]=app._normalise_unit(line[i])
                elif t in {"coats","coverage_m2_per_litre","productivity_m2_per_hour","rate_per_unit"}:base[t]=v12._to_float(app,line[i])
                else:base[t]=clean(line[i]) if clean(line[i]).lower() not in {"nan","none"} else ""
            for t,i in direct.items():
                if i<len(line): base[t]=v12._to_float(app,line[i]) if t in {"coats","rate_per_unit"} else clean(line[i])
            if not clean(base.get("element")):base["element"]=v12._derive_element(base.get("location"))
            if any(ROLLUP_RE.search(clean(base.get(k))) for k in ("section","element","location")):continue
            base["source_reference"]=clean(base.get("source_reference")) or f"{name} · row {rn}"
            vals=[]
            if has_metrics:
                for u,idxs in metrics.items():
                    q=sum(max(0.0,v12._to_float(app,line[i])) for i in idxs if i<len(line));
                    if q>0: vals.append((q,u,", ".join(raw_headers[i] for i in idxs if i<len(line) and v12._to_float(app,line[i])>0)))
            if not vals:
                q=max(0.0,v12._to_float(app,base.get("quantity"))); u=clean(base.get("unit")) or "m²"; vals=[(q,u,"")] if q>0 or not floor else []
            floor_text=bool(FLOOR_RE.search(f"{base.get('element')} {base.get('location')}")); work=[x for x in vals if x[0]>0]
            if floor>0 and work and not floor_text:
                for q,u,src in work:add(base,q,u,"",src if len(work)>1 else "")
                add(base,floor,"m²","floor_area","Floor area column");continue
            if floor>0 or floor_text:
                q=floor or next((q for q,u,_ in vals if (app._normalise_unit(u) or u)=="m²"),0.0);add(base,q,"m²","floor_area");continue
            for q,u,src in vals:add(base,q,u,"",src if len(vals)>1 else "")
        if not rows:raise RuntimeError("No take-off lines were found. Check the selected header row and column mapping.")
        if has_metrics:warnings.append("PB/JobHub quantity columns detected and imported independently.")
        if any(t=="floor_area" for t in mapping.values()):warnings.append("Floor-area columns are separate unpriced references and never replace work quantities.")
        return pd.DataFrame(rows,columns=app.TAKEOFF_COLUMNS+["row_role"]),warnings
    return parse


def import_ai(app:Any,base:Any):
    def run(wid:int,data:Dict[str,Any]):
        schema(app); before=app.lquery("SELECT COALESCE(MAX(id),0) id FROM takeoff_rows WHERE workspace_id=?",(wid,)); bid=int(before[0]["id"] or 0); counts=base(wid,data); created=app.lquery("SELECT * FROM takeoff_rows WHERE workspace_id=? AND id>? ORDER BY id",(wid,bid))
        for row,source in zip(created,list(data.get("takeoff_rows") or [])):
            q=max(0.0,app.to_float(source.get("quantity",row.get("quantity")))); st=clean(row.get("quantity_status")); st="Provisional measured" if q>0 and st.lower()=="measured" else st; note=f"{clean(row.get('notes'))} · AI draft — verify against the mapped drawing or schedule before final publish.".strip(" ·"); app.lexecute("UPDATE takeoff_rows SET origin='AI',ai_baseline_quantity=?,quantity_status=?,notes=?,rate_per_unit=CASE WHEN row_role='floor_area' THEN 0 ELSE rate_per_unit END WHERE id=?",(q,st,note,row["id"]))
        return counts
    return run


def reconcile(app:Any,wid:int)->pd.DataFrame:
    df=dataframe_for_takeoff(app,wid); cols=["section","element","location","unit","ai_qty","drawn_qty","variance","basis","status"]
    if df.empty:return pd.DataFrame(columns=cols)
    mapped=mapped_ids(app,wid); rec=[]
    for r in df.to_dict("records"):
        rid=int(r.get("id") or 0); ai=is_ai(r); aq=max(0.0,app.to_float(r.get("ai_baseline_quantity") if r.get("ai_baseline_quantity") is not None else r.get("quantity"))) if ai else 0; dq=max(0.0,app.to_float(r.get("quantity"))) if rid in mapped else 0; bases=(['AI'] if ai else [])+(['Drawn'] if rid in mapped else ([] if ai else ['Manual'])); rec.append({"section":clean(r.get("section")),"element":clean(r.get("element")),"location":clean(r.get("location")),"unit":app._normalise_unit(r.get("unit")) or clean(r.get("unit")),"ai_qty":aq,"drawn_qty":dq,"bases":bases})
    out=[]
    for _,g in pd.DataFrame(rec).groupby(["section","element","location","unit"],dropna=False):
        aq,dq=float(g.ai_qty.sum()),float(g.drawn_qty.sum()); var=dq-aq
        status="Manual / not yet measured" if aq==dq==0 else ("Matched" if aq>0 and dq>0 and abs(var)<=max(.01,.02*max(aq,dq)) else ("Difference" if aq>0 and dq>0 else ("AI only (not drawn)" if aq>0 else "Drawn only (no AI draft)")))
        out.append({"section":g.section.iloc[0],"element":g.element.iloc[0],"location":g.location.iloc[0],"unit":g.unit.iloc[0],"ai_qty":aq,"drawn_qty":dq,"variance":var,"basis":", ".join(sorted({b for x in g.bases for b in x})),"status":status})
    return pd.DataFrame(out,columns=cols)


def issues(app:Any,wid:int)->List[Dict[str,Any]]:
    schema(app); out=[]
    def add(sev,code,msg,rid="",source=""):out.append({"severity":sev,"code":code,"row_id":rid,"source":clean(source),"message":msg})
    df=app.ldf("SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id",(wid,))
    if df.empty:add("Critical","NO_TAKEOFF","No take-off rows exist for this workspace.");return out
    for x in app.scale_gate_issues(wid):add("Critical","UNCALIBRATED_SCALE",f"{x.get('page_label') or 'A referenced page'} has no calibrated drawing scale.",source=x.get("page_label"))
    for p in app.lquery("SELECT DISTINCT p.page_label,COALESCE(p.scale_method,'') scale_method FROM measurement_lines ml JOIN pages p ON p.id=ml.page_id WHERE ml.workspace_id=? AND COALESCE(p.px_per_m,0)>0 AND COALESCE(p.scale_verified,0)=0",(wid,)):add("Critical","UNVERIFIED_SCALE",f"{clean(p.get('page_label')) or 'A mapped page'} scale is {clean(p.get('scale_method')) or 'legacy/unknown'} and not manually verified. Re-save known-distance calibration in Plan Mapper → Scale.",source=p.get("page_label"))
    mapped=mapped_ids(app,wid); work=df.loc[df["row_role"].fillna("").ne("floor_area")]
    for r in work.to_dict("records"):
        rid=int(r.get("id") or 0); inc=clean(r.get("inclusion_status")).upper(); st=clean(r.get("quantity_status")).lower(); u=app._normalise_unit(r.get("unit")) or clean(r.get("unit")); q=max(0.0,app.to_float(r.get("quantity")))
        if inc not in ACTIVE or st in {"excluded","not applicable","n/a"}:continue
        if u not in set(app.UNIT_OPTIONS):add("Critical","INVALID_UNIT",f"Row #{rid} has unsupported unit '{u}'.",rid,r.get("source_reference"))
        if "to measure" in st:add("Critical","TO_MEASURE",f"Row #{rid} is still To measure.",rid,r.get("source_reference"))
        elif q<=0 and u not in {"allowance","L"}:add("Critical","ZERO_QUANTITY",f"Row #{rid} is included but has no quantity.",rid,r.get("source_reference"))
        if app.to_float(r.get("rate_per_unit"))<=0 and u!="L":add("Critical","ZERO_RATE",f"Row #{rid} is included but has no rate.",rid,r.get("source_reference"))
        if is_ai(r) and q>0 and rid not in mapped and clean(r.get("confidence")).lower() not in REVIEWED:add("Critical","AI_UNVERIFIED",f"Row #{rid} still relies on an AI quantity; map it or verify it against the issued source.",rid,r.get("source_reference"))
        if rid in mapped:
            lines=app.lquery("SELECT ml.*,p.width_px,p.height_px,p.px_per_m FROM measurement_lines ml JOIN pages p ON p.id=ml.page_id WHERE ml.workspace_id=? AND ml.takeoff_row_id=?",(wid,rid)); expected,n,bad=measured_qty(app,r,lines)
            if n and abs(expected-q)>max(.01,.005*max(expected,q,1)):add("Critical","MEASUREMENT_MISMATCH",f"Row #{rid} stores {q:.3f} {u}, saved geometry calculates {expected:.3f} {u}.",rid,r.get("source_reference"))
            if bad:add("Warning","INCOMPATIBLE_SHAPE",f"Row #{rid} has {bad} incompatible saved shape(s).",rid,r.get("source_reference"))
            if any("auto-detected" in clean(x.get("notes")).lower() or basis(x)=="footprint_perimeter_height" for x in lines) and clean(r.get("confidence")).lower() not in REVIEWED:add("Critical","AUTO_GEOMETRY_UNVERIFIED",f"Row #{rid} uses auto-detected/gross geometry; adjust for the issued drawing/openings and set confidence Verified.",rid,r.get("source_reference"))
    if clean(app.workspace_setting(wid,"internal_pricing_basis","wall_m2")).lower()=="floor_m2":
        floors=floor_by_scope(app,df); rates:Dict[str,set[float]]={}
        for r in work.to_dict("records"):
            if (app._normalise_unit(r.get("unit")) or r.get("unit"))=="m²" and app.is_internal_wall_row(r.get("section"),r.get("element")):
                scope=scope_of(r.get("location"));
                if floor_for_scope(floors,scope)<=0:add("Critical","MISSING_FLOOR_AREA",f"No floor-area basis exists for {scope}.",r.get("id"),r.get("source_reference"))
                rates.setdefault(scope,set()).add(round(app.to_float(r.get("rate_per_unit")),4))
        for scope,rs in rates.items():
            if len({x for x in rs if x>0})>1:add("Critical","AMBIGUOUS_FLOOR_RATES",f"{scope} has multiple floor-pricing rates {sorted(x for x in rs if x>0)}; consolidate/verify the intended rate allocation.")
    seen={}
    for r in work.to_dict("records"):
        if app.to_float(r.get("quantity"))<=0:continue
        k=tuple(norm(r.get(x)) for x in ("section","element","location","substrate","finish_system","unit","inclusion_status"));seen.setdefault(k,[]).append(r)
    for rs in seen.values():
        if len(rs)>1:add("Critical" if {clean(r.get("origin")).lower() for r in rs}=={"ai"} else "Warning","POSSIBLE_DUPLICATE",f"Possible duplicate rows {[r.get('id') for r in rs]}; review to avoid double counting.")
    return out


def apply(app:Any)->None:
    if getattr(app,"_pb_accuracy_v125_applied",False):return
    base_init,base_exec,base_scale,base_ai,base_pub,base_page=app.init_local_db,app.lexecute,app.scale_gate_issues,app.import_ai_result,app.publish_job_to_jobhub,app.subscription_takeoff_page
    def init():base_init();schema(app)
    app.init_local_db=init; app.lexecute=guarded_exec(app,base_exec); app.level_of=level_of; app.level_sort_key=level_sort_key; app.pricing_scope_of=scope_of; app.floor_area_by_level=lambda df:{lvl:sum(app.to_float(r.get("quantity")) for r in floor_rows(app,df) if level_of(r.get("location"))==lvl) for lvl in {level_of(r.get("location")) for r in floor_rows(app,df)}}; app.floor_area_by_scope=lambda df:floor_by_scope(app,df); app.dataframe_for_takeoff=lambda wid:dataframe_for_takeoff(app,wid); app.per_level_summary=lambda wid:per_level_summary(app,wid); app.auto_detect_scale=lambda p:auto_scale(app,p)
    app.scale_gate_issues=lambda wid:list(base_scale(wid) or [])+[dict(page_id=int(r["page_id"]),page_label=clean(r.get("page_label")),page_type=clean(r.get("page_type")),px_per_m=app.to_float(r.get("px_per_m"))) for r in app.lquery("SELECT DISTINCT p.id page_id,p.page_label,p.page_type,p.px_per_m FROM measurement_lines ml JOIN pages p ON p.id=ml.page_id WHERE ml.workspace_id=? AND p.selected=1 AND COALESCE(p.px_per_m,0)<=0",(wid,)) if int(r["page_id"]) not in {int(x.get("page_id") or 0) for x in list(base_scale(wid) or [])}]
    app.scale_gate_blocked=lambda wid:bool(app.scale_gate_issues(wid)); app._ensure_mapper_row=lambda *a:mapper_row(app,*a); app.auto_map_measurements=lambda *a:auto_map(app,*a); app.auto_detect_envelope_shapes=lambda *a:auto_envelope(app,*a); app.save_measurement_lines=lambda wid,pid,lines:save_lines(app,base_exec,wid,pid,lines); app.recompute_takeoff_rows_from_measurements=lambda wid,ids:recompute(app,base_exec,wid,ids); app.parse_takeoff_file=parse_file(app); app.import_ai_result=import_ai(app,base_ai); app.reconcile_ai_vs_drawn=lambda wid:reconcile(app,wid); app.takeoff_accuracy_issues=lambda wid:issues(app,wid)
    def publish(wid,bridge,actor="PlanReader"):
        bad=[x for x in issues(app,wid) if x["severity"]=="Critical"]
        if bad:raise RuntimeError("Take-off accuracy gate blocked final publish: "+"; ".join(x["message"] for x in bad[:6]))
        return base_pub(wid,bridge,actor)
    app.publish_job_to_jobhub=publish
    def page(workspace,key,provider="OpenAI"):
        wid=int(workspace["id"]); rows=app.ldf("SELECT id FROM takeoff_rows WHERE workspace_id=? LIMIT 1",(wid,)); audit=issues(app,wid) if not rows.empty else []
        if audit:
            c=sum(x["severity"]=="Critical" for x in audit); w=sum(x["severity"]=="Warning" for x in audit); (app.st.error if c else app.st.warning)(f"Take-off accuracy gate: {c} critical issue(s), {w} warning(s)." if c else f"Take-off accuracy gate: {w} warning(s) need review.")
            with app.st.expander("Take-off accuracy audit",expanded=bool(c)):app.st.dataframe(pd.DataFrame(audit),use_container_width=True,hide_index=True)
        elif not rows.empty:app.st.success("Take-off accuracy gate: no critical quantity, scale, pricing or verification issues detected.")
        return base_page(workspace,key,provider)
    app.subscription_takeoff_page=page; app.PB_ACCURACY_VERSION=PB_ACCURACY_VERSION; app._pb_accuracy_v125_applied=True
