from pb_accuracy_v13_engines_v145 import (
    split_segments_at_intersections, extract_planar_faces, attach_room_labels,
    detect_openings, room_quantity_summary, reconcile_overhead_regions,
    reconcile_facade, facade_net_area, semantic_assignment, benchmark_variance,
)


def test_room_topology_and_openings():
    segs=[((0,0),(4,0)),((4,0),(4,3)),((4,3),(0,3)),((0,3),(0,0)),((2,-1),(2,4))]
    split=split_segments_at_intersections(segs)
    assert len(split) >= 8
    faces=extract_planar_faces(segs)
    assert len(faces) == 2
    rooms=attach_room_labels(faces,[{"x":1,"y":1,"label":"Bedroom","evidence":"A101"},{"x":3,"y":1,"label":"Living","evidence":"A101"}])
    assert round(sum(r['floor_area_m2'] for r in rooms),2)==12.00
    openings=detect_openings([
        {"tag":"D01","swing":True,"width_m":0.82,"height_m":2.04,"evidence":["A103"]},
        {"tag":"W01","parallel_lines":3,"width_m":1.8,"height_m":1.2,"evidence":["A104"]},
    ])
    q=room_quantity_summary(rooms,openings)
    assert q['door_count']==1 and q['window_count']==1 and q['opening_deduction_m2']>3.8


def test_overhead_scope_separates_external_from_internal_and_dedupes():
    result=reconcile_overhead_regions([
        {"geometry_id":"c1","label":"Apartment ceiling","page_type":"RCP","area_m2":100,"evidence":["RCP1"]},
        {"geometry_id":"s1","label":"Balcony soffit","page_type":"RCP","area_m2":25,"evidence":["RCP2"]},
        {"geometry_id":"b1","label":"Breezeway common circulation","area_m2":40,"evidence":["RCP3"]},
        {"geometry_id":"b1","label":"Breezeway common circulation","area_m2":40,"evidence":["RCP3"]},
    ])
    assert result['internal_ceiling_m2']==100
    assert result['external_overhead_m2']==65
    assert result['duplicate_geometry_ids']==['b1']


def test_cross_view_reconciliation_and_substrate_net_area():
    assert reconcile_facade(20.0,20.2,2.7,2.7)['status']=='verified'
    assert reconcile_facade(20.0,22.0,2.7,2.7)['status']=='conflict'
    net=facade_net_area([
        {"substrate":"Render","area_m2":50,"evidence":["E1"]},
        {"substrate":"FC","area_m2":30,"evidence":["E1"]},
    ],[
        {"substrate":"Render","area_m2":6,"deduct":True},
        {"substrate":"FC","area_m2":4,"deduct":False},
    ])
    assert net['Render']['net_m2']==44
    assert net['FC']['net_m2']==30


def test_semantic_reasoning_is_evidence_bound_and_honours_by_others():
    schedules=[
        {"code":"PT01","description":"Walls","substrate":"Plasterboard","colour":"White","coating_system":"3 coat","evidence":["FIN-01"]},
        {"code":"WF1","description":"Timber finish by others","substrate":"Timber","evidence":["FIN-01"]},
    ]
    pt=semantic_assignment('PT01',schedules)
    assert pt['status']=='resolved' and pt['included'] is True and pt['geometry_confidence'] is None
    wf=semantic_assignment('WF1',schedules)
    assert wf['status']=='excluded' and wf['included'] is False
    missing=semantic_assignment('PT99',schedules)
    assert missing['status']=='review' and missing['included'] is None


def test_benchmark_variance():
    b=benchmark_variance(100,103)
    assert b['variance']==3 and b['variance_pct']==3
