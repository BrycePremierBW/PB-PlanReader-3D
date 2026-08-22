"""Deterministic Accuracy v1.3 engines for topology, overhead scope, cross-view QA and semantics.

This module deliberately keeps measurement geometry separate from semantic interpretation.
It exposes pure functions for tests/benchmarking and light app hooks for PlanReader.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from math import hypot, atan2, pi
from typing import Any, Dict, Iterable, List, Sequence, Tuple
import re

VERSION = "1.4.5"
Point = Tuple[float, float]
Segment = Tuple[Point, Point]


def _r(v: float, n: int = 4) -> float:
    return round(float(v), n)


def _same(a: Point, b: Point, tol: float = 1e-6) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def segment_length(seg: Segment) -> float:
    return hypot(seg[1][0] - seg[0][0], seg[1][1] - seg[0][1])


def _cross(a: Point, b: Point, c: Point) -> float:
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def _segment_intersection(a: Segment, b: Segment, tol: float = 1e-9) -> Point | None:
    (x1,y1),(x2,y2)=a; (x3,y3),(x4,y4)=b
    den=(x1-x2)*(y3-y4)-(y1-y2)*(x3-x4)
    if abs(den)<=tol:
        return None
    px=((x1*y2-y1*x2)*(x3-x4)-(x1-x2)*(x3*y4-y3*x4))/den
    py=((x1*y2-y1*x2)*(y3-y4)-(y1-y2)*(x3*y4-y3*x4))/den
    def inside(x,a,b): return min(a,b)-tol <= x <= max(a,b)+tol
    if inside(px,x1,x2) and inside(py,y1,y2) and inside(px,x3,x4) and inside(py,y3,y4):
        return (_r(px,8),_r(py,8))
    return None


def split_segments_at_intersections(segments: Sequence[Segment]) -> List[Segment]:
    """Split vector linework at every true crossing before graph traversal."""
    pts: List[List[Point]] = [[tuple(map(float,s[0])), tuple(map(float,s[1]))] for s in segments]
    for i in range(len(segments)):
        for j in range(i+1,len(segments)):
            p=_segment_intersection(segments[i],segments[j])
            if p is not None:
                pts[i].append(p); pts[j].append(p)
    out: List[Segment]=[]
    for original, candidates in zip(segments,pts):
        a,b=original; dx=b[0]-a[0]; dy=b[1]-a[1]
        denom=dx*dx+dy*dy or 1.0
        unique=[]
        for p in candidates:
            if not any(_same(p,q) for q in unique): unique.append(p)
        unique.sort(key=lambda p: ((p[0]-a[0])*dx+(p[1]-a[1])*dy)/denom)
        for p,q in zip(unique,unique[1:]):
            if segment_length((p,q))>1e-7: out.append((p,q))
    return out


def _polygon_area(poly: Sequence[Point]) -> float:
    return abs(sum(poly[i][0]*poly[(i+1)%len(poly)][1]-poly[(i+1)%len(poly)][0]*poly[i][1] for i in range(len(poly)))/2.0)


def _polygon_perimeter(poly: Sequence[Point]) -> float:
    return sum(hypot(poly[(i+1)%len(poly)][0]-poly[i][0],poly[(i+1)%len(poly)][1]-poly[i][1]) for i in range(len(poly)))


def _point_in_polygon(p: Point, poly: Sequence[Point]) -> bool:
    x,y=p; inside=False
    j=len(poly)-1
    for i in range(len(poly)):
        xi,yi=poly[i]; xj,yj=poly[j]
        if ((yi>y)!=(yj>y)) and x < (xj-xi)*(y-yi)/(yj-yi+1e-15)+xi: inside=not inside
        j=i
    return inside


def extract_planar_faces(segments: Sequence[Segment], min_area: float = 0.05) -> List[List[Point]]:
    """Extract bounded faces from split planar linework using directed half-edge traversal."""
    segs=split_segments_at_intersections(segments)
    adj: Dict[Point,List[Point]]={}
    for a,b in segs:
        a=(_r(a[0],8),_r(a[1],8)); b=(_r(b[0],8),_r(b[1],8))
        adj.setdefault(a,[]).append(b); adj.setdefault(b,[]).append(a)
    for node in adj:
        adj[node]=sorted(set(adj[node]),key=lambda q: atan2(q[1]-node[1],q[0]-node[0]))
    used=set(); faces=[]
    for u in list(adj):
        for v in adj[u]:
            if (u,v) in used: continue
            face=[]; a,b=u,v
            for _ in range(max(8,len(segs)*4)):
                if (a,b) in used: break
                used.add((a,b)); face.append(a)
                nbrs=adj[b]
                try: idx=nbrs.index(a)
                except ValueError: break
                # choose clockwise predecessor; keeps bounded face on left
                c=nbrs[(idx-1)%len(nbrs)]
                a,b=b,c
                if _same(a,u) and _same(b,v):
                    break
            if len(face)>=3:
                signed=sum(face[i][0]*face[(i+1)%len(face)][1]-face[(i+1)%len(face)][0]*face[i][1] for i in range(len(face)))/2
                if signed>0 and abs(signed)>=min_area:
                    faces.append(face)
    # de-duplicate cyclic rotations
    unique=[]; keys=set()
    for f in faces:
        rounded=[(_r(x,6),_r(y,6)) for x,y in f]
        rots=[tuple(rounded[i:]+rounded[:i]) for i in range(len(rounded))]
        key=min(rots)
        if key not in keys: keys.add(key); unique.append(f)
    return unique


def attach_room_labels(faces: Sequence[Sequence[Point]], labels: Sequence[Dict[str,Any]]) -> List[Dict[str,Any]]:
    rows=[]
    for idx,poly in enumerate(faces,1):
        matches=[l for l in labels if _point_in_polygon((float(l.get('x',0)),float(l.get('y',0))),poly)]
        label=str(matches[0].get('label')) if matches else f"Room {idx}"
        rows.append({"room_ref":f"R{idx:02d}","label":label,"polygon":[list(p) for p in poly],"floor_area_m2":_r(_polygon_area(poly),3),"ceiling_reference_m2":_r(_polygon_area(poly),3),"perimeter_m":_r(_polygon_perimeter(poly),3),"geometry_confidence":0.98 if matches else 0.9,"evidence":[m.get('evidence') for m in matches if m.get('evidence')]})
    return rows


def detect_openings(opening_candidates: Sequence[Dict[str,Any]]) -> List[Dict[str,Any]]:
    """Deterministically classify openings from line/swing/tag evidence; no invented dimensions."""
    out=[]
    for i,c in enumerate(opening_candidates,1):
        tag=str(c.get('tag') or '').upper(); kind=str(c.get('kind') or '').lower()
        swing=bool(c.get('swing')); parallel=int(c.get('parallel_lines') or 0)
        if not kind:
            if swing or tag.startswith('D'): kind='door'
            elif parallel>=2 or tag.startswith('W'): kind='window'
            else: kind='opening'
        width=c.get('width_m'); height=c.get('height_m')
        measured=width is not None and height is not None
        confidence=0.97 if measured and (tag or swing or parallel>=2) else 0.78 if measured else 0.55
        out.append({"opening_ref":str(c.get('opening_ref') or f"O{i:02d}"),"kind":kind,"tag":tag,"wall_ref":c.get('wall_ref'),"width_m":None if width is None else _r(width,3),"height_m":None if height is None else _r(height,3),"area_m2":None if not measured else _r(float(width)*float(height),3),"deduct":bool(c.get('deduct',True)),"geometry_confidence":confidence,"semantic_confidence":0.95 if tag else 0.75,"evidence":list(c.get('evidence') or [])})
    return out


def room_quantity_summary(rooms: Sequence[Dict[str,Any]], openings: Sequence[Dict[str,Any]]) -> Dict[str,Any]:
    return {"floor_area_m2":_r(sum(float(r.get('floor_area_m2') or 0) for r in rooms),3),"ceiling_reference_m2":_r(sum(float(r.get('ceiling_reference_m2') or 0) for r in rooms),3),"perimeter_m":_r(sum(float(r.get('perimeter_m') or 0) for r in rooms),3),"door_count":sum(1 for o in openings if o.get('kind')=='door'),"window_count":sum(1 for o in openings if o.get('kind')=='window'),"opening_deduction_m2":_r(sum(float(o.get('area_m2') or 0) for o in openings if o.get('deduct')),3)}


_OVERHEAD_PATTERNS=[
    ('breezeway',re.compile(r'\bbreezeway\b|common\s+circulation',re.I)),
    ('balcony_soffit',re.compile(r'balcon(?:y|ies).*soffit|soffit.*balcon',re.I)),
    ('external_soffit',re.compile(r'\bsoffit\b|\beaves?\b|external\s+ceiling',re.I)),
    ('canopy',re.compile(r'\bcanopy\b|awning',re.I)),
    ('bulkhead',re.compile(r'\bbulkhead\b|ceiling\s+drop',re.I)),
    ('internal_ceiling',re.compile(r'\bceiling\b|\brcp\b',re.I)),
]


def classify_overhead_region(region: Dict[str,Any]) -> Dict[str,Any]:
    text=' '.join(str(region.get(k) or '') for k in ('label','page_type','finish_code','notes','evidence_text'))
    kind='unknown'
    for name,pat in _OVERHEAD_PATTERNS:
        if pat.search(text): kind=name; break
    external=kind in {'breezeway','balcony_soffit','external_soffit','canopy'}
    area=region.get('area_m2')
    return {**region,"overhead_kind":kind,"is_external_overhead":external,"quantity_class":"external_overhead" if external else "internal_ceiling","area_m2":None if area is None else _r(area,3),"geometry_confidence":0.95 if area is not None else 0.55,"scope_confidence":0.95 if kind!='unknown' else 0.4,"evidence":list(region.get('evidence') or [])}


def reconcile_overhead_regions(regions: Sequence[Dict[str,Any]]) -> Dict[str,Any]:
    classified=[classify_overhead_region(r) for r in regions]
    internal=sum(float(r.get('area_m2') or 0) for r in classified if r['quantity_class']=='internal_ceiling')
    external=sum(float(r.get('area_m2') or 0) for r in classified if r['quantity_class']=='external_overhead')
    # shared geometry IDs are only counted once in their explicit external class
    seen=set(); dedup=[]; duplicates=[]
    for r in classified:
        gid=r.get('geometry_id')
        key=(gid,r['quantity_class']) if gid else None
        if key and key in seen: duplicates.append(gid); continue
        if key: seen.add(key)
        dedup.append(r)
    internal=sum(float(r.get('area_m2') or 0) for r in dedup if r['quantity_class']=='internal_ceiling')
    external=sum(float(r.get('area_m2') or 0) for r in dedup if r['quantity_class']=='external_overhead')
    return {"regions":dedup,"internal_ceiling_m2":_r(internal,3),"external_overhead_m2":_r(external,3),"duplicate_geometry_ids":duplicates,"review_count":sum(1 for r in dedup if r['scope_confidence']<0.7)}


def reconcile_facade(plan_width_m: float | None, elevation_width_m: float | None, plan_height_m: float | None=None, elevation_height_m: float | None=None, tolerance_ratio: float=0.02) -> Dict[str,Any]:
    checks=[]
    def one(name,a,b):
        if a is None or b is None: return {"name":name,"status":"review","variance_ratio":None}
        denom=max(abs(float(a)),abs(float(b)),1e-9); vr=abs(float(a)-float(b))/denom
        return {"name":name,"status":"pass" if vr<=tolerance_ratio else "conflict","variance_ratio":_r(vr,4),"a":float(a),"b":float(b)}
    checks.append(one('width',plan_width_m,elevation_width_m)); checks.append(one('height',plan_height_m,elevation_height_m))
    status='conflict' if any(c['status']=='conflict' for c in checks) else 'review' if any(c['status']=='review' for c in checks) else 'verified'
    return {"status":status,"checks":checks,"geometry_confidence":0.98 if status=='verified' else 0.5 if status=='conflict' else 0.72}


def facade_net_area(regions: Sequence[Dict[str,Any]], openings: Sequence[Dict[str,Any]]) -> Dict[str,Any]:
    by={}
    for r in regions:
        sub=str(r.get('substrate') or r.get('finish_code') or 'Unassigned')
        by.setdefault(sub,{"gross_m2":0.0,"deductions_m2":0.0,"net_m2":0.0,"evidence":[]})
        by[sub]['gross_m2']+=float(r.get('area_m2') or 0); by[sub]['evidence']+=list(r.get('evidence') or [])
    for o in openings:
        if not o.get('deduct'): continue
        sub=str(o.get('substrate') or o.get('finish_code') or 'Unassigned')
        if sub not in by: continue
        by[sub]['deductions_m2']+=float(o.get('area_m2') or 0)
    for data in by.values():
        data['gross_m2']=_r(data['gross_m2'],3); data['deductions_m2']=_r(data['deductions_m2'],3); data['net_m2']=_r(max(0,data['gross_m2']-data['deductions_m2']),3)
    return by


def semantic_assignment(code: str, schedules: Sequence[Dict[str,Any]], context: Sequence[Dict[str,Any]] | None=None) -> Dict[str,Any]:
    """Resolve finish/scope meaning only from explicit evidence. Never manufactures dimensions."""
    code_u=str(code or '').strip().upper(); matches=[]
    for row in schedules:
        candidate=str(row.get('code') or row.get('finish_code') or '').strip().upper()
        if candidate==code_u: matches.append(row)
    evidence=[]
    for m in matches: evidence += list(m.get('evidence') or [])
    text=' '.join(str(m.get(k) or '') for m in matches for k in ('description','substrate','colour','coating_system','notes'))
    excluded=bool(re.search(r'\bby others\b|\bnot in scope\b|\bexclude(?:d)?\b',text,re.I))
    ambiguous=len(matches)!=1
    m=matches[0] if matches else {}
    result={"code":code_u,"description":m.get('description'),"substrate":m.get('substrate'),"colour":m.get('colour'),"coating_system":m.get('coating_system'),"included":False if excluded else (True if matches else None),"semantic_confidence":0.98 if len(matches)==1 and evidence else 0.86 if len(matches)==1 else 0.35,"geometry_confidence":None,"evidence":evidence,"status":"review" if ambiguous else ("excluded" if excluded else "resolved")}
    if context:
        result['references']=[c.get('reference') for c in context if c.get('reference')]
        result['evidence'] += [c.get('evidence') for c in context if c.get('evidence')]
    return result


def semantic_batch(codes: Iterable[str], schedules: Sequence[Dict[str,Any]], context: Sequence[Dict[str,Any]] | None=None) -> List[Dict[str,Any]]:
    return [semantic_assignment(c,schedules,context) for c in codes]


def benchmark_variance(measured: float, predicted: float) -> Dict[str,float]:
    measured=float(measured); predicted=float(predicted)
    variance=predicted-measured
    pct=0.0 if abs(measured)<1e-9 else variance/measured*100.0
    return {"measured":_r(measured,3),"predicted":_r(predicted,3),"variance":_r(variance,3),"variance_pct":_r(pct,2)}


def apply(app: Any) -> None:
    if getattr(app,"_pb_accuracy_v13_engines_v145_applied",False): return
    app._pb_accuracy_v13_engines_v145_applied=True
    app.split_segments_at_intersections_v145=split_segments_at_intersections
    app.extract_planar_faces_v145=extract_planar_faces
    app.attach_room_labels_v145=attach_room_labels
    app.detect_openings_v145=detect_openings
    app.room_quantity_summary_v145=room_quantity_summary
    app.classify_overhead_region_v145=classify_overhead_region
    app.reconcile_overhead_regions_v145=reconcile_overhead_regions
    app.reconcile_facade_v145=reconcile_facade
    app.facade_net_area_v145=facade_net_area
    app.semantic_assignment_v145=semantic_assignment
    app.benchmark_variance_v145=benchmark_variance
