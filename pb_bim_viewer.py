"""
PlanReader Commercial 3D BIM Viewer Component.

Provides a modern, interactive Three.js 3D WebGL BIM viewer built directly from
Canonical Building Model data.

Features:
- Genuine physical wall cut-outs for floor-touching doors (sill=0) and windows
- Base64 UTF-8 JSON encoding preventing script-context XSS break-out
- 2-Pass scene building resolving opening host walls regardless of payload order
- Complete declared type support (CEILING, SCREEN, SURFACE, BALUSTRADE, PARAPET, etc.)
- Review state visual styling across ALL geometry types (CONFIRMED, INFERRED, REVIEW_REQUIRED)
- ZERO invented physical fallbacks:
  - Unknown level elevation is NOT treated as ground level 0.0
  - Missing sill_height_m or offset_along_wall_m does NOT position physical openings at origin/floor (skipped)
  - Missing thickness renders as 2D flat planar/line geometry, NOT arbitrary blocks
  - Missing parapet/linear length removes || 1.0 fallbacks (must come strictly from endpoints)
  - Missing elevation_offset_m in createPolygonMesh fails closed (does NOT default to 0.0)
"""

import base64
import json
import math
from typing import Dict, Any, Optional
from pb_canonical_building import CanonicalProject, ObjectType, ReviewState, parse_strict_bool
from pb_geometry_services import (
    wall_length,
    wall_gross_area,
    gross_opening_area,
    potential_net_wall_area,
    space_floor_area,
    model_bounds,
    surface_metadata,
    validate_wall_geometry,
    validate_opening_geometry,
)


def project_to_viewer_payload(project: CanonicalProject) -> Dict[str, Any]:
    """
    Translates a CanonicalProject object graph into a clean JSON payload
    consumed by the Three.js BIM rendering engine.
    """
    bounds_ok, bounds = model_bounds(project)

    levels_payload = []
    objects_payload = []

    for bld in project.buildings:
        for lvl in bld.levels:
            rev_str = lvl.review_state.value if isinstance(lvl.review_state, ReviewState) else str(lvl.review_state or "REVIEW_REQUIRED")
            lvl_info = {
                "id": lvl.id,
                "name": lvl.name,
                "elevation_m": lvl.elevation_m if (lvl.elevation_m is not None and not math.isnan(lvl.elevation_m)) else None,
                "height_m": lvl.height_m if (lvl.height_m is not None and not math.isnan(lvl.height_m)) else None,
                "level_index": lvl.level_index,
                "review_state": rev_str,
            }
            levels_payload.append(lvl_info)

            # Walls and attached openings
            for w in lvl.walls:
                valid_w, _ = validate_wall_geometry(w)
                if not valid_w:
                    continue

                w_len = wall_length(w)
                w_gross = wall_gross_area(w)
                p_net = potential_net_wall_area(w)
                w_rev = w.review_state.value if isinstance(w.review_state, ReviewState) else str(w.review_state or "REVIEW_REQUIRED")

                openings_data = []
                for op in w.openings:
                    valid_op, _ = validate_opening_geometry(op, w)
                    if not valid_op:
                        continue

                    op_gross = gross_opening_area(op)
                    op_rev = op.review_state.value if isinstance(op.review_state, ReviewState) else str(op.review_state or "REVIEW_REQUIRED")
                    phys_state = str(op.metadata.get("physical_state") or ("physical_b5_authorised" if op.deduction_authority else "physical_not_authorised"))
                    
                    # SECTION 3 & 4: Do NOT replace op.wall_id with w.id for wrong-host or non-matching records!
                    eff_wall_id = w.id if op.wall_id == w.id else None
                    is_host_attached = (eff_wall_id == w.id) and (phys_state not in ("wrong_host", "wrong_level", "invalid_geometry", "conflict_overlap", "evidence_only"))

                    op_data = {
                        "id": op.id,
                        "name": op.name,
                        "type": op.object_type.value if isinstance(op.object_type, ObjectType) else str(op.object_type),
                        "opening_type": op.opening_type,
                        "level_id": lvl.id,
                        "wall_id": eff_wall_id,
                        "physical_state": phys_state,
                        "is_host_attached": is_host_attached,
                        "offset_along_wall_m": op.offset_along_wall_m,
                        "sill_height_m": op.sill_height_m,
                        "width_m": op.width_m,
                        "height_m": op.height_m,
                        "mark": op.mark,
                        "gross_area_m2": op_gross,
                        "substrate": op.substrate,
                        "finish": op.finish,
                        "confidence": op.confidence,
                        "review_state": op_rev,
                        "provenance": op.provenance.to_dict() if op.provenance else {},
                        "deduction_authorized": parse_strict_bool(op.deduction_authority),
                    }
                    if is_host_attached:
                        openings_data.append(op_data)
                    objects_payload.append(op_data)

                w_data = {
                    "id": w.id,
                    "name": w.name,
                    "type": ObjectType.WALL.value,
                    "level_id": lvl.id,
                    "start_point": w.start_point.to_dict(),
                    "end_point": w.end_point.to_dict(),
                    "thickness_m": w.thickness_m,
                    "height_m": w.height_m,
                    "length_m": w_len,
                    "gross_area_m2": w_gross,
                    "observed_opening_area_m2": p_net["observed_opening_area_m2"],
                    "potential_net_area_m2": p_net["potential_net_area_m2"],
                    "authorized_opening_deduction_area_m2": p_net["authorized_opening_deduction_area_m2"],
                    "authorized_net_area_m2": p_net["authorized_net_area_m2"],
                    "unauthorized_opening_area_m2": p_net["unauthorized_opening_area_m2"],
                    "valid_opening_count": p_net["valid_opening_count"],
                    "invalid_unresolved_opening_count": p_net["invalid_unresolved_opening_count"],
                    "authorized_opening_count": p_net["authorized_opening_count"],
                    "unauthorized_opening_count": p_net["unauthorized_opening_count"],
                    "has_overlapping_openings": p_net["has_overlapping_openings"],
                    "all_deductions_authorized": p_net["all_deductions_authorized"],
                    "authority_note": p_net["authority_note"],
                    "is_external": parse_strict_bool(w.is_external),
                    "substrate": w.substrate,
                    "finish": w.finish,
                    "confidence": w.confidence,
                    "review_state": w_rev,
                    "provenance": w.provenance.to_dict() if w.provenance else {},
                    "openings": openings_data,
                }
                objects_payload.append(w_data)

            # Polygon elements (Floors, Ceilings, Roofs, Soffits, Balconies)
            poly_groups = [
                ("floors", lvl.floors, ObjectType.FLOOR.value),
                ("ceilings", lvl.ceilings, ObjectType.CEILING.value),
                ("roofs", lvl.roofs, ObjectType.ROOF.value),
                ("soffits", lvl.soffits, ObjectType.SOFFIT.value),
                ("balconies", lvl.balconies, ObjectType.BALCONY.value),
            ]
            for grp_name, items, type_val in poly_groups:
                for item in items:
                    poly_pts = [pt.to_dict() for pt in getattr(item, "polygon", []) if pt and pt.is_valid()]
                    if len(poly_pts) < 3:
                        continue
                    item_rev = item.review_state.value if isinstance(item.review_state, ReviewState) else str(item.review_state or "REVIEW_REQUIRED")
                    
                    elev_off = getattr(item, "elevation_offset_m", None)
                    cap_z = getattr(item, "elevation", None)
                    if cap_z is not None and lvl.elevation_m is not None:
                        elev_off = cap_z - lvl.elevation_m

                    item_data = {
                        "id": item.id,
                        "name": item.name,
                        "type": type_val,
                        "level_id": lvl.id,
                        "parent_id": item.parent_id,
                        "polygon": poly_pts,
                        "thickness_m": item.thickness_m,
                        "elevation": cap_z,
                        "elevation_offset_m": elev_off,
                        "substrate": item.substrate,
                        "finish": item.finish,
                        "confidence": item.confidence,
                        "review_state": item_rev,
                        "provenance": item.provenance.to_dict() if item.provenance else {},
                    }
                    if hasattr(item, "roof_type"):
                        item_data["roof_type"] = item.roof_type
                        item_data["pitch_deg"] = item.pitch_deg
                    objects_payload.append(item_data)

            # Parapets
            for p in lvl.parapets:
                p_len = math.hypot(p.end_point.x - p.start_point.x, p.end_point.y - p.start_point.y) if (p.start_point and p.start_point.is_valid() and p.end_point and p.end_point.is_valid()) else 0.0
                if p_len <= 1e-4:
                    continue
                p_rev = p.review_state.value if isinstance(p.review_state, ReviewState) else str(p.review_state or "REVIEW_REQUIRED")
                p_data = {
                    "id": p.id,
                    "name": p.name,
                    "type": ObjectType.PARAPET.value,
                    "level_id": lvl.id,
                    "start_point": p.start_point.to_dict(),
                    "end_point": p.end_point.to_dict(),
                    "height_m": p.height_m,
                    "thickness_m": p.thickness_m,
                    "length_m": p_len,
                    "gross_area_m2": p_len * p.height_m if p.height_m else 0.0,
                    "substrate": p.substrate,
                    "finish": p.finish,
                    "confidence": p.confidence,
                    "review_state": p_rev,
                    "provenance": p.provenance.to_dict() if p.provenance else {},
                }
                objects_payload.append(p_data)

            # Columns
            for col in lvl.columns:
                col_rev = col.review_state.value if isinstance(col.review_state, ReviewState) else str(col.review_state or "REVIEW_REQUIRED")
                col_data = {
                    "id": col.id,
                    "name": col.name,
                    "type": ObjectType.COLUMN.value,
                    "level_id": lvl.id,
                    "center": col.center.to_dict() if (col.center and col.center.is_valid()) else None,
                    "width_m": col.width_m,
                    "depth_m": col.depth_m,
                    "height_m": col.height_m,
                    "substrate": col.substrate,
                    "finish": col.finish,
                    "confidence": col.confidence,
                    "review_state": col_rev,
                    "provenance": col.provenance.to_dict() if col.provenance else {},
                }
                objects_payload.append(col_data)

            # Balustrades and Screens
            linear_groups = [
                (lvl.balustrades, ObjectType.BALUSTRADE.value),
                (lvl.screens, ObjectType.SCREEN.value),
            ]
            for items, type_val in linear_groups:
                for lin in items:
                    b_len = math.hypot(lin.end_point.x - lin.start_point.x, lin.end_point.y - lin.start_point.y) if (lin.start_point and lin.start_point.is_valid() and lin.end_point and lin.end_point.is_valid()) else 0.0
                    if b_len <= 1e-4:
                        continue
                    lin_rev = lin.review_state.value if isinstance(lin.review_state, ReviewState) else str(lin.review_state or "REVIEW_REQUIRED")
                    lin_data = {
                        "id": lin.id,
                        "name": lin.name,
                        "type": type_val,
                        "level_id": lvl.id,
                        "start_point": lin.start_point.to_dict(),
                        "end_point": lin.end_point.to_dict(),
                        "height_m": lin.height_m,
                        "length_m": b_len,
                        "substrate": lin.substrate,
                        "finish": lin.finish,
                        "confidence": lin.confidence,
                        "review_state": lin_rev,
                        "provenance": lin.provenance.to_dict() if lin.provenance else {},
                    }
                    objects_payload.append(lin_data)

            # Surfaces
            for s in lvl.surfaces:
                s_rev = s.review_state.value if isinstance(s.review_state, ReviewState) else str(s.review_state or "REVIEW_REQUIRED")
                s_data = {
                    "id": s.id,
                    "name": s.name,
                    "type": ObjectType.SURFACE.value,
                    "level_id": lvl.id,
                    "parent_element_id": s.parent_element_id,
                    "surface_area_m2": s.surface_area_m2,
                    "orientation": s.orientation,
                    "substrate": s.substrate,
                    "finish": s.finish,
                    "confidence": s.confidence,
                    "review_state": s_rev,
                    "provenance": s.provenance.to_dict() if s.provenance else {},
                }
                objects_payload.append(s_data)

    return {
        "project_id": project.id,
        "project_name": project.name,
        "is_synthetic_demo": parse_strict_bool(getattr(project, "is_synthetic_demo", False)),
        "bounds_available": bounds_ok,
        "bounds": bounds.to_dict() if bounds_ok and bounds else None,
        "levels": levels_payload,
        "objects": objects_payload,
    }


def generate_bim_viewer_html(payload: Dict[str, Any], height_px: int = 750) -> str:
    """
    Generates an HTML/JS document featuring an interactive Three.js 3D WebGL viewer
    with Base64 UTF-8 JSON script encoding (XSS-safe), 2-pass scene building,
    genuine physical wall cut-outs, and ZERO invented physical fallbacks.
    """
    json_bytes = json.dumps(payload).encode("utf-8")
    b64_payload = base64.b64encode(json_bytes).decode("ascii")

    html_code = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>PlanReader Commercial 3D BIM Viewer</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
        body, html {{ width: 100%; height: 100%; overflow: hidden; background-color: #111827; color: #f3f4f6; }}
        
        #container {{ display: flex; width: 100%; height: 100vh; position: relative; }}
        #canvas-wrap {{ flex: 1; height: 100%; position: relative; background: radial-gradient(circle at center, #1f2937 0%, #0f172a 100%); }}
        #webgl-canvas {{ width: 100%; height: 100%; display: block; }}

        .top-banner {{
            position: absolute; top: 12px; left: 16px; z-index: 10;
            background: rgba(15, 23, 42, 0.85); backdrop-filter: blur(8px);
            border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 8px;
            padding: 8px 16px; display: flex; align-items: center; gap: 12px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
        }}
        .top-banner h1 {{ font-size: 15px; font-weight: 600; color: #f9fafb; letter-spacing: -0.2px; }}
        .badge-demo {{ background: #d97706; color: #fff; font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; text-transform: uppercase; display: none; }}

        .toolbar {{
            position: absolute; top: 12px; right: 16px; z-index: 10;
            display: flex; gap: 8px;
        }}
        .btn {{
            background: rgba(30, 41, 59, 0.85); backdrop-filter: blur(8px);
            border: 1px solid rgba(255, 255, 255, 0.15); color: #e2e8f0;
            padding: 7px 12px; border-radius: 6px; font-size: 12px; font-weight: 500;
            cursor: pointer; transition: all 0.15s ease;
        }}
        .btn:hover {{ background: rgba(51, 65, 85, 0.95); border-color: rgba(255, 255, 255, 0.3); color: #fff; }}
        .btn:active {{ transform: translateY(1px); }}

        .filter-panel {{
            position: absolute; bottom: 16px; left: 16px; z-index: 10;
            background: rgba(15, 23, 42, 0.85); backdrop-filter: blur(8px);
            border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 8px;
            padding: 12px; width: 260px; max-height: 240px; overflow-y: auto;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
        }}
        .filter-title {{ font-size: 11px; font-weight: 700; text-transform: uppercase; color: #94a3b8; margin-bottom: 8px; letter-spacing: 0.5px; }}
        .filter-group {{ margin-bottom: 10px; }}
        .checkbox-label {{ display: flex; align-items: center; gap: 8px; font-size: 12px; color: #cbd5e1; margin-bottom: 4px; cursor: pointer; }}
        .checkbox-label input {{ accent-color: #3b82f6; cursor: pointer; }}

        #side-panel {{
            width: 340px; height: 100%; background: #0f172a; border-left: 1px solid rgba(255, 255, 255, 0.1);
            display: flex; flex-direction: column; z-index: 20; box-shadow: -4px 0 20px rgba(0,0,0,0.5);
        }}
        .panel-header {{
            padding: 16px; border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            display: flex; justify-content: space-between; align-items: center; background: #1e293b;
        }}
        .panel-title {{ font-size: 14px; font-weight: 600; color: #f8fafc; }}
        .review-badge {{
            padding: 3px 8px; border-radius: 999px; font-size: 10px; font-weight: 700; text-transform: uppercase;
        }}
        .badge-confirmed {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.4); }}
        .badge-inferred {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.4); }}
        .badge-review {{ background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(248, 113, 113, 0.4); }}

        .tab-bar {{ display: flex; background: #0f172a; border-bottom: 1px solid rgba(255, 255, 255, 0.1); }}
        .tab-btn {{ flex: 1; padding: 10px 4px; font-size: 11px; font-weight: 600; text-align: center; color: #64748b; background: none; border: none; cursor: pointer; border-bottom: 2px solid transparent; }}
        .tab-btn.active {{ color: #38bdf8; border-bottom-color: #38bdf8; background: rgba(56, 189, 248, 0.05); }}

        .tab-content {{ flex: 1; padding: 16px; overflow-y: auto; }}
        .info-row {{ display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid rgba(255, 255, 255, 0.05); font-size: 12px; }}
        .info-label {{ color: #94a3b8; font-weight: 500; }}
        .info-val {{ color: #f1f5f9; font-weight: 600; text-align: right; word-break: break-word; max-width: 180px; }}
        .info-section-title {{ font-size: 11px; font-weight: 700; text-transform: uppercase; color: #38bdf8; margin: 14px 0 6px 0; letter-spacing: 0.5px; }}

        .empty-state {{ text-align: center; color: #64748b; margin-top: 80px; font-size: 13px; line-height: 1.6; padding: 0 20px; }}
    </style>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
</head>
<body>
    <div id="container">
        <div id="canvas-wrap">
            <div class="top-banner">
                <h1 id="proj-title">PlanReader 3D BIM Model</h1>
                <span id="proj-badge" class="badge-demo">Demo Fixture</span>
            </div>

            <div class="toolbar">
                <button id="btn-reset" class="btn">Reset Camera</button>
                <button id="btn-wireframe" class="btn">Toggle Mesh Wireframe</button>
            </div>

            <div class="filter-panel">
                <div class="filter-group">
                    <div class="filter-title">Level Visibility</div>
                    <div id="level-filters"></div>
                </div>
                <div class="filter-group">
                    <div class="filter-title">Object Categories</div>
                    <div id="category-filters"></div>
                </div>
            </div>

            <canvas id="webgl-canvas"></canvas>
        </div>

        <div id="side-panel">
            <div class="panel-header">
                <div class="panel-title" id="sel-title">No Object Selected</div>
                <div id="sel-badge" class="review-badge badge-review" style="display:none;">REVIEW REQUIRED</div>
            </div>

            <div class="tab-bar">
                <button id="tab-btn-overview" class="tab-btn active">Overview</button>
                <button id="tab-btn-advanced" class="tab-btn">Advanced</button>
                <button id="tab-btn-evidence" class="tab-btn">Evidence</button>
                <button id="tab-btn-diagnostics" class="tab-btn">Diagnostics</button>
            </div>

            <div id="tab-overview" class="tab-content">
                <div class="empty-state">Click any 3D element (wall, window, door, balcony, roof, ceiling, screen) in the viewer to inspect provenance, dimensions, substrate, and review state.</div>
            </div>
            <div id="tab-advanced" class="tab-content" style="display:none;">
                <div class="empty-state">Select an object to inspect structural hierarchy and bounding geometry.</div>
            </div>
            <div id="tab-evidence" class="tab-content" style="display:none;">
                <div class="empty-state">Select an object to inspect source PDF drawing trace evidence.</div>
            </div>
            <div id="tab-diagnostics" class="tab-content" style="display:none;">
                <div class="empty-state">Select an object to view geometry diagnostic details.</div>
            </div>
        </div>
    </div>

    <script>
        const b64Data = "{b64_payload}";
        const jsonText = new TextDecoder().decode(Uint8Array.from(atob(b64Data), c => c.charCodeAt(0)));
        const modelData = JSON.parse(jsonText);

        let scene, camera, renderer, controls;
        let meshMap = new Map();
        let objectDataMap = new Map();
        let selectedMesh = null;
        let selectedObjectData = null;
        let activeTab = 'overview';
        let isWireframeActive = false;

        function getMaterialForObject(obj) {{
            const rev = obj.review_state || 'REVIEW_REQUIRED';
            const type = obj.type || 'UNKNOWN';

            let baseColor = 0xe2e8f0;
            let opacity = 1.0;
            let transparent = false;

            if (type === 'WALL') baseColor = 0xe2e8f0;
            else if (type === 'DOOR') baseColor = 0xb45309;
            else if (type === 'WINDOW') {{ baseColor = 0x38bdf8; opacity = 0.55; transparent = true; }}
            else if (type === 'FLOOR') baseColor = 0x64748b;
            else if (type === 'CEILING') {{ baseColor = 0xf8fafc; opacity = 0.65; transparent = true; }}
            else if (type === 'BALCONY') baseColor = 0x0ea5e9;
            else if (type === 'SOFFIT') baseColor = 0x94a3b8;
            else if (type === 'PARAPET') baseColor = 0x475569;
            else if (type === 'ROOF') baseColor = 0x334155;
            else if (type === 'COLUMN') baseColor = 0x94a3b8;
            else if (type === 'BALUSTRADE') {{ baseColor = 0x0284c7; opacity = 0.7; transparent = true; }}
            else if (type === 'SCREEN') {{ baseColor = 0xd97706; opacity = 0.75; transparent = true; }}

            if (rev === 'REVIEW_REQUIRED') {{
                baseColor = (type === 'WINDOW' || type === 'CEILING') ? 0xf87171 : 0xef4444;
                opacity = 0.85;
                transparent = true;
            }} else if (rev === 'INFERRED') {{
                baseColor = (type === 'WINDOW' || type === 'CEILING') ? 0x38bdf8 : 0x38bdf8;
                opacity = 0.75;
                transparent = true;
            }}

            return new THREE.MeshStandardMaterial({{
                color: baseColor,
                roughness: 0.5,
                metalness: 0.1,
                opacity: opacity,
                transparent: transparent,
                wireframe: isWireframeActive
            }});
        }}

        const highlightMaterial = new THREE.MeshStandardMaterial({{
            color: 0xf59e0b, roughness: 0.2, metalness: 0.5, emissive: 0x78350f
        }});

        function init() {{
            const titleElem = document.getElementById('proj-title');
            titleElem.textContent = modelData.project_name || "PlanReader 3D Model";

            const badgeElem = document.getElementById('proj-badge');
            if (modelData.is_synthetic_demo) {{
                badgeElem.style.display = 'inline-block';
            }} else {{
                badgeElem.style.display = 'none';
            }}

            const canvas = document.getElementById('webgl-canvas');
            const wrap = document.getElementById('canvas-wrap');

            scene = new THREE.Scene();
            scene.background = new THREE.Color(0x0f172a);

            camera = new THREE.PerspectiveCamera(45, wrap.clientWidth / wrap.clientHeight, 0.1, 1000);

            renderer = new THREE.WebGLRenderer({{ canvas: canvas, antialias: true, alpha: true }});
            renderer.setSize(wrap.clientWidth, wrap.clientHeight);
            renderer.setPixelRatio(window.devicePixelRatio);
            renderer.shadowMap.enabled = true;
            renderer.shadowMap.type = THREE.PCFSoftShadowMap;

            controls = new THREE.OrbitControls(camera, renderer.domElement);
            controls.enableDamping = true;
            controls.dampingFactor = 0.05;

            // Lighting
            const ambient = new THREE.AmbientLight(0xffffff, 0.75);
            scene.add(ambient);

            const sun = new THREE.DirectionalLight(0xffffff, 0.85);
            sun.position.set(20, 40, 30);
            sun.castShadow = true;
            sun.shadow.mapSize.width = 2048;
            sun.shadow.mapSize.height = 2048;
            scene.add(sun);

            const fillLight = new THREE.DirectionalLight(0x38bdf8, 0.3);
            fillLight.position.set(-20, 20, -20);
            scene.add(fillLight);

            const grid = new THREE.GridHelper(40, 40, 0x334155, 0x1e293b);
            grid.position.y = -0.01;
            scene.add(grid);

            buildScene();
            setupFilters();
            resetCamera();

            document.getElementById('btn-reset').addEventListener('click', resetCamera);
            document.getElementById('btn-wireframe').addEventListener('click', toggleWireframe);

            ['overview', 'advanced', 'evidence', 'diagnostics'].forEach(tabName => {{
                document.getElementById('tab-btn-' + tabName).addEventListener('click', (e) => switchTab(tabName, e));
            }});

            window.addEventListener('resize', onWindowResize);
            canvas.addEventListener('pointerdown', onPointerDown);

            animate();
        }}

        // 2-Pass Scene Building: ZERO INVENTED PHYSICAL FALLBACKS
        function buildScene() {{
            if (!modelData.objects || modelData.objects.length === 0) return;

            // PASS 1: Index ALL objects in objectDataMap first
            modelData.objects.forEach(obj => objectDataMap.set(obj.id, obj));

            const levelMap = new Map();
            if (modelData.levels) {{
                modelData.levels.forEach(l => levelMap.set(l.id, l));
            }}

            // PASS 2: Create and render 3D meshes
            modelData.objects.forEach(obj => {{
                const lvl = levelMap.get(obj.level_id);
                if (!lvl || lvl.elevation_m === null || lvl.elevation_m === undefined || isNaN(lvl.elevation_m)) {{
                    return;
                }}
                const zElev = lvl.elevation_m;

                let mat = getMaterialForObject(obj);
                let mesh = null;

                if (obj.type === 'WALL') {{
                    mesh = createWallMeshWithHoles(obj, zElev, mat);
                }} else if (obj.type === 'DOOR' || obj.type === 'WINDOW' || obj.type === 'OPENING') {{
                    mesh = createOpeningMesh(obj, zElev, mat);
                }} else if (obj.type === 'FLOOR' || obj.type === 'CEILING' || obj.type === 'ROOF' || obj.type === 'BALCONY' || obj.type === 'SOFFIT') {{
                    mesh = createPolygonMesh(obj, zElev, mat);
                }} else if (obj.type === 'PARAPET') {{
                    mesh = createParapetMesh(obj, zElev, mat);
                }} else if (obj.type === 'COLUMN') {{
                    mesh = createColumnMesh(obj, zElev, mat);
                }} else if (obj.type === 'BALUSTRADE' || obj.type === 'SCREEN') {{
                    mesh = createLinearMesh(obj, zElev, mat);
                }}

                if (mesh) {{
                    mesh.userData = {{ id: obj.id, level_id: obj.level_id, type: obj.type, originalMaterial: mat }};
                    scene.add(mesh);
                    meshMap.set(obj.id, mesh);
                }}
            }});
        }}

        function createWallMeshWithHoles(wall, zElev, mat) {{
            const p1 = wall.start_point;
            const p2 = wall.end_point;
            if (!p1 || !p2 || p1.x === null || p1.y === null || p2.x === null || p2.y === null) return null;

            const dx = p2.x - p1.x;
            const dy = p2.y - p1.y;
            const len = Math.hypot(dx, dy);
            if (isNaN(len) || len < 0.001) return null;

            if (wall.height_m === null || wall.height_m === undefined || isNaN(wall.height_m) || wall.height_m <= 0) {{
                const lineGeom = new THREE.BufferGeometry().setFromPoints([
                    new THREE.Vector3(p1.x, zElev, -p1.y),
                    new THREE.Vector3(p2.x, zElev, -p2.y)
                ]);
                const lineMat = new THREE.LineDashedMaterial({{ color: 0xef4444, dashSize: 0.2, gapSize: 0.1 }});
                const line = new THREE.Line(lineGeom, lineMat);
                line.computeLineDistances();
                return line;
            }}

            const hWall = wall.height_m;

            if (wall.thickness_m === null || wall.thickness_m === undefined || isNaN(wall.thickness_m) || wall.thickness_m <= 0) {{
                const planeGeom = new THREE.PlaneGeometry(len, hWall);
                const mesh = new THREE.Mesh(planeGeom, mat);
                const angle = Math.atan2(dy, dx);
                mesh.rotation.y = -angle;
                const midX = (p1.x + p2.x) / 2;
                const midY = (p1.y + p2.y) / 2;
                mesh.position.set(midX, zElev + hWall / 2, -midY);
                return mesh;
            }}

            const th = wall.thickness_m;
            const shape = new THREE.Shape();
            shape.moveTo(0, 0);
            shape.lineTo(len, 0);
            shape.lineTo(len, hWall);
            shape.lineTo(0, hWall);
            shape.closePath();

            if (wall.openings && wall.openings.length > 0) {{
                wall.openings.forEach(op => {{
                    const isAttached = op.is_host_attached !== false && (!op.wall_id || op.wall_id === wall.id);
                    const physState = op.physical_state || '';
                    const isRejected = (physState === 'wrong_host' || physState === 'wrong_level' || physState === 'invalid_geometry' || physState === 'conflict_overlap' || physState === 'evidence_only');

                    if (isAttached && !isRejected) {{
                        const off = op.offset_along_wall_m;
                        const wOp = op.width_m;
                        const hOp = op.height_m;
                        const sill = op.sill_height_m;

                        if (off !== null && off !== undefined && sill !== null && sill !== undefined &&
                            wOp !== null && hOp !== null && wOp > 0 && hOp > 0 && off >= 0 && sill >= 0 &&
                            (off + wOp) <= (len + 0.05) && (sill + hOp) <= (hWall + 0.05)) {{
                            const hole = new THREE.Path();
                            hole.moveTo(off, sill);
                            hole.lineTo(off + wOp, sill);
                            hole.lineTo(off + wOp, sill + hOp);
                            hole.lineTo(off, sill + hOp);
                            hole.closePath();
                            shape.holes.push(hole);
                        }}
                    }}
                }});
            }}

            const extrudeSettings = {{ steps: 1, depth: th, bevelEnabled: false }};
            const geom = new THREE.ExtrudeGeometry(shape, extrudeSettings);
            geom.translate(0, 0, -th / 2);

            const mesh = new THREE.Mesh(geom, mat);
            mesh.castShadow = true;
            mesh.receiveShadow = true;

            const angle = Math.atan2(dy, dx);
            mesh.rotation.y = -angle;
            mesh.position.set(p1.x, zElev, -p1.y);
            return mesh;
        }}

        function createOpeningMesh(op, zElev, mat) {{
            if (!op || op.is_host_attached === false || !op.wall_id) return null;
            const physState = op.physical_state || '';
            if (physState === 'wrong_host' || physState === 'wrong_level' || physState === 'invalid_geometry' || physState === 'conflict_overlap' || physState === 'evidence_only') {{
                return null;
            }}
            if (op.width_m === null || op.height_m === null || op.width_m <= 0 || op.height_m <= 0) return null;
            if (op.sill_height_m === null || op.sill_height_m === undefined || isNaN(op.sill_height_m) || op.sill_height_m < 0) return null;
            if (op.offset_along_wall_m === null || op.offset_along_wall_m === undefined || isNaN(op.offset_along_wall_m) || op.offset_along_wall_m < 0) return null;

            const wWidth = op.width_m;
            const wHeight = op.height_m;
            const sill = op.sill_height_m;
            const off = op.offset_along_wall_m;

            const geom = new THREE.BoxGeometry(wWidth, wHeight, 0.06);
            const mesh = new THREE.Mesh(geom, mat);
            mesh.castShadow = true;

            const wall = objectDataMap.get(op.wall_id);
            if (wall && wall.start_point && wall.end_point) {{
                const p1 = wall.start_point;
                const p2 = wall.end_point;
                if (p1.x !== null && p1.y !== null && p2.x !== null && p2.y !== null) {{
                    const len = Math.hypot(p2.x - p1.x, p2.y - p1.y);
                    if (len > 0.001) {{
                        const ux = (p2.x - p1.x) / len;
                        const uy = (p2.y - p1.y) / len;

                        const cx = p1.x + ux * (off + wWidth / 2);
                        const cy = p1.y + uy * (off + wWidth / 2);
                        const angle = Math.atan2(p2.y - p1.y, p2.x - p1.x);

                        mesh.rotation.y = -angle;
                        mesh.position.set(cx, zElev + sill + wHeight / 2, -cy);
                    }}
                }}
            }}
            return mesh;
        }}

        function createPolygonMesh(polyObj, zElev, mat) {{
            if (!polyObj.polygon || polyObj.polygon.length < 3) return null;

            // FAIL CLOSED: elevation_offset_m === null means offset is unknown unless objective elevation is given!
            if ((polyObj.elevation_offset_m === null || polyObj.elevation_offset_m === undefined) && (polyObj.elevation === null || polyObj.elevation === undefined)) {{
                return null;
            }}

            let renderZ = null;
            if (polyObj.elevation !== null && polyObj.elevation !== undefined && !isNaN(polyObj.elevation)) {{
                renderZ = polyObj.elevation;
            }} else if (polyObj.elevation_offset_m !== null && polyObj.elevation_offset_m !== undefined && !isNaN(polyObj.elevation_offset_m) && zElev !== null && zElev !== undefined && !isNaN(zElev)) {{
                renderZ = zElev + polyObj.elevation_offset_m;
            }} else if (zElev !== null && zElev !== undefined && !isNaN(zElev) && polyObj.type !== 'ROOF') {{
                renderZ = zElev;
            }}

            if (renderZ === null || renderZ === undefined || isNaN(renderZ)) {{
                return null;
            }}

            const shape = new THREE.Shape();
            polyObj.polygon.forEach((pt, idx) => {{
                if (pt.x !== null && pt.y !== null) {{
                    if (idx === 0) shape.moveTo(pt.x, -pt.y);
                    else shape.lineTo(pt.x, -pt.y);
                }}
            }});

            if (polyObj.thickness_m === null || polyObj.thickness_m === undefined || isNaN(polyObj.thickness_m) || polyObj.thickness_m <= 0) {{
                const geom = new THREE.ShapeGeometry(shape);
                geom.rotateX(Math.PI / 2);
                const mesh = new THREE.Mesh(geom, mat);
                mesh.position.y = renderZ;
                mesh.receiveShadow = true;
                return mesh;
            }}

            const th = polyObj.thickness_m;
            const extrudeSettings = {{ steps: 1, depth: th, bevelEnabled: false }};
            const geom = new THREE.ExtrudeGeometry(shape, extrudeSettings);
            geom.rotateX(Math.PI / 2);

            const mesh = new THREE.Mesh(geom, mat);
            mesh.position.y = renderZ;
            mesh.receiveShadow = true;
            return mesh;
        }}

        function createParapetMesh(p, zElev, mat) {{
            if (p.height_m === null || p.height_m <= 0) return null;
            if (p.length_m === null || p.length_m === undefined || isNaN(p.length_m) || p.length_m <= 0) return null;
            const len = p.length_m;

            const dx = p.end_point.x - p.start_point.x;
            const dy = p.end_point.y - p.start_point.y;
            const angle = Math.atan2(dy, dx);
            const midX = (p.start_point.x + p.end_point.x) / 2;
            const midY = (p.start_point.y + p.end_point.y) / 2;

            if (p.thickness_m === null || p.thickness_m === undefined || isNaN(p.thickness_m) || p.thickness_m <= 0) {{
                const geom = new THREE.PlaneGeometry(len, p.height_m);
                const mesh = new THREE.Mesh(geom, mat);
                mesh.rotation.y = -angle;
                mesh.position.set(midX, zElev + p.height_m / 2, -midY);
                return mesh;
            }}

            const geom = new THREE.BoxGeometry(len, p.height_m, p.thickness_m);
            const mesh = new THREE.Mesh(geom, mat);
            mesh.castShadow = true;
            mesh.rotation.y = -angle;
            mesh.position.set(midX, zElev + p.height_m / 2, -midY);
            return mesh;
        }}

        function createColumnMesh(col, zElev, mat) {{
            if (!col.center || col.center.x === null || col.center.y === null || col.width_m === null || col.depth_m === null || col.height_m === null) {{
                return null;
            }}
            const geom = new THREE.BoxGeometry(col.width_m, col.height_m, col.depth_m);
            const mesh = new THREE.Mesh(geom, mat);
            mesh.castShadow = true;
            mesh.position.set(col.center.x, zElev + col.height_m / 2, -col.center.y);
            return mesh;
        }}

        function createLinearMesh(lin, zElev, mat) {{
            if (lin.height_m === null || lin.height_m <= 0) return null;
            if (lin.length_m === null || lin.length_m === undefined || isNaN(lin.length_m) || lin.length_m <= 0) return null;
            const len = lin.length_m;
            const geom = new THREE.PlaneGeometry(len, lin.height_m);
            const mesh = new THREE.Mesh(geom, mat);
            mesh.castShadow = true;

            const dx = lin.end_point.x - lin.start_point.x;
            const dy = lin.end_point.y - lin.start_point.y;
            const angle = Math.atan2(dy, dx);
            mesh.rotation.y = -angle;

            const midX = (lin.start_point.x + lin.end_point.x) / 2;
            const midY = (lin.start_point.y + lin.end_point.y) / 2;
            mesh.position.set(midX, zElev + lin.height_m / 2, -midY);
            return mesh;
        }}

        function resetCamera() {{
            if (!modelData.bounds_available || !modelData.bounds) {{
                controls.target.set(0, 0, 0);
                camera.position.set(15, 15, 20);
                controls.update();
                return;
            }}
            const b = modelData.bounds;
            if (!b.min_point || !b.max_point || b.min_point.x === null || b.max_point.x === null) {{
                controls.target.set(0, 0, 0);
                camera.position.set(15, 15, 20);
                controls.update();
                return;
            }}

            const cx = (b.min_point.x + b.max_point.x) / 2;
            const cy = (b.min_point.z + b.max_point.z) / 2;
            const cz = -(b.min_point.y + b.max_point.y) / 2;

            controls.target.set(cx, cy, cz);
            camera.position.set(cx + 18, cy + 16, cz + 24);
            controls.update();
        }}

        function setupFilters() {{
            const lvlContainer = document.getElementById('level-filters');
            lvlContainer.innerHTML = '';
            if (modelData.levels) {{
                modelData.levels.forEach(l => {{
                    const lbl = document.createElement('label');
                    lbl.className = 'checkbox-label';
                    
                    const chk = document.createElement('input');
                    chk.type = 'checkbox';
                    chk.checked = true;
                    chk.dataset.levelId = l.id;
                    chk.addEventListener('change', (e) => toggleLevel(l.id, e.target.checked));
                    
                    lbl.appendChild(chk);
                    lbl.appendChild(document.createTextNode(' ' + (l.name || l.id)));
                    lvlContainer.appendChild(lbl);
                }});
            }}

            const catContainer = document.getElementById('category-filters');
            catContainer.innerHTML = '';
            const cats = ['WALL', 'DOOR', 'WINDOW', 'FLOOR', 'CEILING', 'BALCONY', 'SOFFIT', 'PARAPET', 'ROOF', 'COLUMN', 'BALUSTRADE', 'SCREEN', 'SURFACE'];
            cats.forEach(cat => {{
                const lbl = document.createElement('label');
                lbl.className = 'checkbox-label';
                
                const chk = document.createElement('input');
                chk.type = 'checkbox';
                chk.checked = true;
                chk.dataset.category = cat;
                chk.addEventListener('change', (e) => toggleCategory(cat, e.target.checked));
                
                lbl.appendChild(chk);
                lbl.appendChild(document.createTextNode(' ' + cat + 's'));
                catContainer.appendChild(lbl);
            }});
        }}

        function toggleLevel(levelId, visible) {{
            meshMap.forEach((mesh) => {{
                if (mesh.userData.level_id === levelId) mesh.visible = visible;
            }});
        }}

        function toggleCategory(catType, visible) {{
            meshMap.forEach((mesh) => {{
                if (mesh.userData.type === catType) mesh.visible = visible;
            }});
        }}

        function toggleWireframe() {{
            isWireframeActive = !isWireframeActive;
            meshMap.forEach(mesh => {{
                if (mesh.material && mesh !== selectedMesh) {{
                    mesh.material.wireframe = isWireframeActive;
                }}
            }});
        }}

        function onPointerDown(event) {{
            const wrap = document.getElementById('canvas-wrap');
            const rect = wrap.getBoundingClientRect();
            const mouse = new THREE.Vector2(
                ((event.clientX - rect.left) / wrap.clientWidth) * 2 - 1,
                -((event.clientY - rect.top) / wrap.clientHeight) * 2 + 1
            );

            const raycaster = new THREE.Raycaster();
            raycaster.setFromCamera(mouse, camera);

            const visibleMeshes = Array.from(meshMap.values()).filter(m => m.visible);
            const intersects = raycaster.intersectObjects(visibleMeshes, true);

            if (intersects.length > 0) {{
                let hit = intersects[0].object;
                while (hit && !hit.userData.id && hit.parent) {{
                    hit = hit.parent;
                }}
                if (hit && hit.userData.id) selectObject(hit);
            }}
        }}

        function selectObject(mesh) {{
            if (selectedMesh && selectedMesh.userData.originalMaterial) {{
                selectedMesh.material = selectedMesh.userData.originalMaterial;
            }}

            selectedMesh = mesh;
            mesh.material = highlightMaterial;

            const id = mesh.userData.id;
            selectedObjectData = objectDataMap.get(id);

            updateSidePanel();
        }}

        function updateSidePanel() {{
            if (!selectedObjectData) return;
            const obj = selectedObjectData;

            const titleElem = document.getElementById('sel-title');
            titleElem.textContent = obj.name || obj.id;

            const badge = document.getElementById('sel-badge');
            badge.style.display = 'inline-block';
            
            const revState = obj.review_state || 'REVIEW_REQUIRED';
            badge.textContent = revState;
            badge.className = 'review-badge ' + (
                revState === 'CONFIRMED' ? 'badge-confirmed' :
                revState === 'INFERRED' ? 'badge-inferred' : 'badge-review'
            );

            renderTabContent();
        }}

        function switchTab(tabName, event) {{
            activeTab = tabName;
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            if (event && event.target) {{
                event.target.classList.add('active');
            }} else {{
                const btn = document.getElementById('tab-btn-' + tabName);
                if (btn) btn.classList.add('active');
            }}

            ['overview', 'advanced', 'evidence', 'diagnostics'].forEach(t => {{
                document.getElementById('tab-' + t).style.display = (t === tabName) ? 'block' : 'none';
            }});

            renderTabContent();
        }}

        function addInfoRow(container, labelText, valText) {{
            const row = document.createElement('div');
            row.className = 'info-row';
            
            const lbl = document.createElement('span');
            lbl.className = 'info-label';
            lbl.textContent = labelText;
            
            const val = document.createElement('span');
            val.className = 'info-val';
            val.textContent = valText;
            
            row.appendChild(lbl);
            row.appendChild(val);
            container.appendChild(row);
        }}

        function addSectionTitle(container, titleText) {{
            const title = document.createElement('div');
            title.className = 'info-section-title';
            title.textContent = titleText;
            container.appendChild(title);
        }}

        function renderTabContent() {{
            if (!selectedObjectData) return;
            const obj = selectedObjectData;

            const container = document.getElementById('tab-' + activeTab);
            container.innerHTML = '';

            if (activeTab === 'overview') {{
                addInfoRow(container, 'Type', obj.type || 'N/A');
                addInfoRow(container, 'Level ID', obj.level_id || 'N/A');
                addInfoRow(container, 'Substrate', obj.substrate || 'Not Specified');
                addInfoRow(container, 'Finish', obj.finish || 'Not Specified');

                if (obj.gross_area_m2 !== undefined && obj.gross_area_m2 !== null && !isNaN(obj.gross_area_m2)) {{
                    addInfoRow(container, 'Gross Area', obj.gross_area_m2.toFixed(2) + ' m²');
                }}
                if (obj.observed_opening_area_m2 !== undefined && obj.observed_opening_area_m2 !== null && !isNaN(obj.observed_opening_area_m2)) {{
                    addInfoRow(container, 'Observed Opening Area', obj.observed_opening_area_m2.toFixed(2) + ' m²');
                }}
                if (obj.potential_net_area_m2 !== undefined && obj.potential_net_area_m2 !== null && !isNaN(obj.potential_net_area_m2)) {{
                    addInfoRow(container, 'Potential Net Area', obj.potential_net_area_m2.toFixed(2) + ' m²');
                }}
                if (obj.authorized_opening_deduction_area_m2 !== undefined && obj.authorized_opening_deduction_area_m2 !== null && !isNaN(obj.authorized_opening_deduction_area_m2)) {{
                    addInfoRow(container, 'Authorized Opening Deductions', obj.authorized_opening_deduction_area_m2.toFixed(2) + ' m²');
                    addInfoRow(container, 'Authorized Net Area', obj.authorized_net_area_m2.toFixed(2) + ' m²');
                }}
                if (obj.height_m !== undefined && obj.height_m !== null && !isNaN(obj.height_m)) {{
                    addInfoRow(container, 'Height', obj.height_m.toFixed(2) + ' m');
                }} else {{
                    addInfoRow(container, 'Height', 'Not Specified');
                }}

            }} else if (activeTab === 'advanced') {{
                addInfoRow(container, 'Object ID', obj.id);
                addInfoRow(container, 'Parent ID', obj.parent_id || 'None');
                
                let confText = 'Not Recorded';
                if (obj.confidence !== undefined && obj.confidence !== null && !isNaN(obj.confidence)) {{
                    confText = (obj.confidence * 100).toFixed(0) + '%';
                }}
                addInfoRow(container, 'Confidence Score', confText);

                if (obj.thickness_m !== undefined && obj.thickness_m !== null) {{
                    addInfoRow(container, 'Thickness', obj.thickness_m + ' m');
                }} else {{
                    addInfoRow(container, 'Thickness', 'Not Specified');
                }}
                if (obj.is_external !== undefined) addInfoRow(container, 'Is External', String(obj.is_external));

            }} else if (activeTab === 'evidence') {{
                const p = obj.provenance || {{}};
                addSectionTitle(container, 'Drawing Origin');
                addInfoRow(container, 'Source PDF', p.source_pdf || 'Not Recorded');
                addInfoRow(container, 'Page Number', (p.page_number !== undefined && p.page_number !== null) ? String(p.page_number) : 'N/A');
                addInfoRow(container, 'Drawing Sheet ID', p.drawing_id || 'N/A');
                addInfoRow(container, 'Scale Source', p.scale_source || 'N/A');

                addSectionTitle(container, 'Evidence Traces');
                if (p.contributing_evidence && p.contributing_evidence.length > 0) {{
                    p.contributing_evidence.forEach(ev => {{
                        addInfoRow(container, '•', ev);
                    }});
                }} else {{
                    addInfoRow(container, 'Traces', 'Unspecified');
                }}

            }} else if (activeTab === 'diagnostics') {{
                addSectionTitle(container, 'Model Authority Check');
                addInfoRow(container, 'Review State', obj.review_state || 'REVIEW_REQUIRED');
                
                const authText = obj.all_deductions_authorized ? 'All Deductions Authorized' : 
                               (obj.authorized_opening_deduction_area_m2 > 0 ? 'Partial Deduction Authorized' : 'Unauthorized');
                addInfoRow(container, 'Deduction Status', authText);
                addInfoRow(container, 'Authority Note', obj.authority_note || 'Potential Net Geometry Only');
            }}
        }}

        function onWindowResize() {{
            const wrap = document.getElementById('canvas-wrap');
            camera.aspect = wrap.clientWidth / wrap.clientHeight;
            camera.updateProjectionMatrix();
            renderer.setSize(wrap.clientWidth, wrap.clientHeight);
        }}

        function animate() {{
            requestAnimationFrame(animate);
            controls.update();
            renderer.render(scene, camera);
        }}

        window.onload = init;
    </script>
</body>
</html>"""
    return html_code
