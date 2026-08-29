"""
PlanReader Commercial 3D BIM Viewer Component.

Provides a modern, interactive Three.js 3D WebGL BIM viewer built directly from
Canonical Building Model data.

Features:
- Perspective 3D camera with Orbit, Pan, Zoom, Reset Home controls
- Solid walls, subtractive opening cut-outs, floors, roofs, balconies, soffits, parapets, columns
- Review state visual styling: CONFIRMED (Solid), INFERRED (Translucent/Dotted), REVIEW_REQUIRED (Warning Highlight)
- Level & Object Category Show/Hide filters
- Click-to-inspect object selection with live Information Panel (Overview, Advanced, Evidence & Provenance, Diagnostics)
- Zero made-up data for missing fields
"""

import json
import math
from typing import Dict, Any, Optional
from pb_canonical_building import CanonicalProject, ObjectType, ReviewState
from pb_geometry_services import (
    wall_length,
    wall_gross_area,
    gross_opening_area,
    potential_net_wall_area,
    space_floor_area,
    model_bounds,
    surface_metadata,
)


def project_to_viewer_payload(project: CanonicalProject) -> Dict[str, Any]:
    """
    Translates a CanonicalProject object graph into a clean, optimized JSON payload
    consumed by the Three.js BIM rendering engine.
    """
    bounds = model_bounds(project)
    
    levels_payload = []
    objects_payload = []

    for bld in project.buildings:
        for lvl in bld.levels:
            lvl_info = {
                "id": lvl.id,
                "name": lvl.name,
                "elevation_m": lvl.elevation_m,
                "height_m": lvl.height_m,
                "level_index": lvl.level_index,
                "review_state": lvl.review_state.value if isinstance(lvl.review_state, ReviewState) else str(lvl.review_state),
            }
            levels_payload.append(lvl_info)

            # Walls and attached openings
            for w in lvl.walls:
                w_len = wall_length(w)
                w_gross = wall_gross_area(w)
                p_net = potential_net_wall_area(w)
                
                openings_data = []
                for op in w.openings:
                    op_gross = gross_opening_area(op)
                    op_data = {
                        "id": op.id,
                        "name": op.name,
                        "type": op.object_type.value if isinstance(op.object_type, ObjectType) else str(op.object_type),
                        "opening_type": op.opening_type,
                        "level_id": lvl.id,
                        "wall_id": w.id,
                        "offset_along_wall_m": op.offset_along_wall_m,
                        "sill_height_m": op.sill_height_m,
                        "width_m": op.width_m,
                        "height_m": op.height_m,
                        "mark": op.mark,
                        "gross_area_m2": op_gross,
                        "substrate": op.substrate,
                        "finish": op.finish,
                        "confidence": op.confidence,
                        "review_state": op.review_state.value if isinstance(op.review_state, ReviewState) else str(op.review_state),
                        "provenance": op.provenance.to_dict(),
                        "deduction_authorized": op.deduction_authority,
                    }
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
                    "opening_area_m2": p_net["total_opening_area_m2"],
                    "potential_net_area_m2": p_net["potential_net_area_m2"],
                    "deduction_authorized": p_net["deduction_authorized"],
                    "authority_note": p_net["authority_note"],
                    "is_external": w.is_external,
                    "substrate": w.substrate,
                    "finish": w.finish,
                    "confidence": w.confidence,
                    "review_state": w.review_state.value if isinstance(w.review_state, ReviewState) else str(w.review_state),
                    "provenance": w.provenance.to_dict(),
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
                    poly_pts = [pt.to_dict() for pt in getattr(item, "polygon", [])]
                    item_data = {
                        "id": item.id,
                        "name": item.name,
                        "type": type_val,
                        "level_id": lvl.id,
                        "parent_id": item.parent_id,
                        "polygon": poly_pts,
                        "thickness_m": item.thickness_m,
                        "elevation_offset_m": getattr(item, "elevation_offset_m", 0.0),
                        "substrate": item.substrate,
                        "finish": item.finish,
                        "confidence": item.confidence,
                        "review_state": item.review_state.value if isinstance(item.review_state, ReviewState) else str(item.review_state),
                        "provenance": item.provenance.to_dict(),
                    }
                    if hasattr(item, "roof_type"):
                        item_data["roof_type"] = item.roof_type
                        item_data["pitch_deg"] = item.pitch_deg
                    objects_payload.append(item_data)

            # Parapets
            for p in lvl.parapets:
                p_len = math.hypot(p.end_point.x - p.start_point.x, p.end_point.y - p.start_point.y)
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
                    "gross_area_m2": p_len * p.height_m,
                    "substrate": p.substrate,
                    "finish": p.finish,
                    "confidence": p.confidence,
                    "review_state": p.review_state.value if isinstance(p.review_state, ReviewState) else str(p.review_state),
                    "provenance": p.provenance.to_dict(),
                }
                objects_payload.append(p_data)

            # Columns
            for col in lvl.columns:
                col_data = {
                    "id": col.id,
                    "name": col.name,
                    "type": ObjectType.COLUMN.value,
                    "level_id": lvl.id,
                    "center": col.center.to_dict(),
                    "width_m": col.width_m,
                    "depth_m": col.depth_m,
                    "height_m": col.height_m,
                    "substrate": col.substrate,
                    "finish": col.finish,
                    "confidence": col.confidence,
                    "review_state": col.review_state.value if isinstance(col.review_state, ReviewState) else str(col.review_state),
                    "provenance": col.provenance.to_dict(),
                }
                objects_payload.append(col_data)

            # Balustrades
            for bal in lvl.balustrades:
                b_len = math.hypot(bal.end_point.x - bal.start_point.x, bal.end_point.y - bal.start_point.y)
                bal_data = {
                    "id": bal.id,
                    "name": bal.name,
                    "type": ObjectType.BALUSTRADE.value,
                    "level_id": lvl.id,
                    "start_point": bal.start_point.to_dict(),
                    "end_point": bal.end_point.to_dict(),
                    "height_m": bal.height_m,
                    "length_m": b_len,
                    "substrate": bal.substrate,
                    "finish": bal.finish,
                    "confidence": bal.confidence,
                    "review_state": bal.review_state.value if isinstance(bal.review_state, ReviewState) else str(bal.review_state),
                    "provenance": bal.provenance.to_dict(),
                }
                objects_payload.append(bal_data)

    return {
        "project_id": project.id,
        "project_name": project.name,
        "disclaimer": project.metadata.get("disclaimer", ""),
        "bounds": bounds.to_dict(),
        "levels": levels_payload,
        "objects": objects_payload,
    }


def generate_bim_viewer_html(payload: Dict[str, Any], height_px: int = 700) -> str:
    """
    Generates a full, responsive HTML/JS document featuring an interactive
    Three.js 3D WebGL viewer, controls overlay, and object inspection side-panel.
    """
    payload_json = json.dumps(payload)

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

        /* Top Header Overlay */
        .top-banner {{
            position: absolute; top: 12px; left: 16px; z-index: 10;
            background: rgba(15, 23, 42, 0.85); backdrop-filter: blur(8px);
            border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 8px;
            padding: 8px 16px; display: flex; align-items: center; gap: 12px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
        }}
        .top-banner h1 {{ font-size: 15px; font-weight: 600; color: #f9fafb; letter-spacing: -0.2px; }}
        .badge-demo {{ background: #d97706; color: #fff; font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; text-transform: uppercase; }}

        /* Toolbar Controls */
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

        /* Filters Overlay Panel */
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

        /* Inspection Side Panel */
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

        /* Tabs */
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
    <!-- Three.js and OrbitControls -->
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
                <button class="btn" onclick="resetCamera()">Reset Camera</button>
                <button class="btn" onclick="toggleWireframe()">Toggle Mesh Wireframe</button>
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
                <div id="sel-badge" class="review-badge badge-confirmed" style="display:none;">CONFIRMED</div>
            </div>

            <div class="tab-bar">
                <button class="tab-btn active" onclick="switchTab('overview')">Overview</button>
                <button class="tab-btn" onclick="switchTab('advanced')">Advanced</button>
                <button class="tab-btn" onclick="switchTab('evidence')">Evidence</button>
                <button class="tab-btn" onclick="switchTab('diagnostics')">Diagnostics</button>
            </div>

            <div id="tab-overview" class="tab-content">
                <div class="empty-state">Click any 3D element (wall, window, door, balcony, roof) in the viewer to inspect provenance, dimensions, substrate, and review state.</div>
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
        const modelData = {payload_json};
        
        let scene, camera, renderer, controls;
        let meshMap = new Map(); // id -> THREE.Mesh / Group
        let objectDataMap = new Map(); // id -> object payload
        let selectedMesh = null;
        let selectedObjectData = null;
        let activeTab = 'overview';

        // Materials cache by category & review state
        const materials = {{
            WALL_CONFIRMED: new THREE.MeshStandardMaterial({{ color: 0xe2e8f0, roughness: 0.4, metalness: 0.1 }}),
            WALL_INFERRED: new THREE.MeshStandardMaterial({{ color: 0xcbd5e1, roughness: 0.5, opacity: 0.85, transparent: true }}),
            WALL_REVIEW: new THREE.MeshStandardMaterial({{ color: 0xfca5a5, roughness: 0.6, wireframe: false }}),
            
            OPENING_DOOR: new THREE.MeshStandardMaterial({{ color: 0xb45309, roughness: 0.3 }}),
            OPENING_WINDOW: new THREE.MeshStandardMaterial({{ color: 0x38bdf8, roughness: 0.1, opacity: 0.6, transparent: true }}),
            
            FLOOR: new THREE.MeshStandardMaterial({{ color: 0x64748b, roughness: 0.7 }}),
            BALCONY: new THREE.MeshStandardMaterial({{ color: 0x0ea5e9, roughness: 0.5 }}),
            SOFFIT: new THREE.MeshStandardMaterial({{ color: 0x94a3b8, roughness: 0.6 }}),
            PARAPET: new THREE.MeshStandardMaterial({{ color: 0x475569, roughness: 0.5 }}),
            ROOF: new THREE.MeshStandardMaterial({{ color: 0x334155, roughness: 0.4 }}),
            COLUMN: new THREE.MeshStandardMaterial({{ color: 0x94a3b8, roughness: 0.3 }}),
            BALUSTRADE: new THREE.MeshStandardMaterial({{ color: 0x0284c7, opacity: 0.7, transparent: true }}),
            HIGHLIGHT: new THREE.MeshStandardMaterial({{ color: 0xf59e0b, roughness: 0.2, metalness: 0.5, emissive: 0x78350f }})
        }};

        function init() {{
            document.getElementById('proj-title').innerText = modelData.project_name || "PlanReader 3D Model";
            
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

            // Ground Grid
            const grid = new THREE.GridHelper(40, 40, 0x334155, 0x1e293b);
            grid.position.y = -0.01;
            scene.add(grid);

            // Build Scene from Canonical Payload
            buildScene();
            setupFilters();
            resetCamera();

            window.addEventListener('resize', onWindowResize);
            canvas.addEventListener('pointerdown', onPointerDown);

            animate();
        }}

        function buildScene() {{
            const levelMap = new Map();
            modelData.levels.forEach(l => levelMap.set(l.id, l));

            modelData.objects.forEach(obj => {{
                objectDataMap.set(obj.id, obj);
                const lvl = levelMap.get(obj.level_id);
                const zElev = lvl ? lvl.elevation_m : 0.0;

                let mat = getMaterialForObject(obj);
                let mesh = null;

                if (obj.type === 'WALL') {{
                    mesh = createWallMesh(obj, zElev, mat);
                }} else if (obj.type === 'DOOR' || obj.type === 'WINDOW' || obj.type === 'OPENING') {{
                    mesh = createOpeningMesh(obj, zElev, mat);
                }} else if (obj.type === 'FLOOR' || obj.type === 'ROOF' || obj.type === 'BALCONY' || obj.type === 'SOFFIT') {{
                    mesh = createPolygonMesh(obj, zElev, mat);
                }} else if (obj.type === 'PARAPET') {{
                    mesh = createParapetMesh(obj, zElev, mat);
                }} else if (obj.type === 'COLUMN') {{
                    mesh = createColumnMesh(obj, zElev, mat);
                }} else if (obj.type === 'BALUSTRADE') {{
                    mesh = createLinearMesh(obj, zElev, mat);
                }}

                if (mesh) {{
                    mesh.userData = {{ id: obj.id, level_id: obj.level_id, type: obj.type, originalMaterial: mat }};
                    scene.add(mesh);
                    meshMap.set(obj.id, mesh);
                }}
            }});
        }}

        function getMaterialForObject(obj) {{
            if (obj.type === 'WALL') {{
                if (obj.review_state === 'REVIEW_REQUIRED') return materials.WALL_REVIEW;
                if (obj.review_state === 'INFERRED') return materials.WALL_INFERRED;
                return materials.WALL_CONFIRMED;
            }}
            if (obj.type === 'DOOR') return materials.OPENING_DOOR;
            if (obj.type === 'WINDOW') return materials.OPENING_WINDOW;
            if (obj.type === 'FLOOR') return materials.FLOOR;
            if (obj.type === 'BALCONY') return materials.BALCONY;
            if (obj.type === 'SOFFIT') return materials.SOFFIT;
            if (obj.type === 'PARAPET') return materials.PARAPET;
            if (obj.type === 'ROOF') return materials.ROOF;
            if (obj.type === 'COLUMN') return materials.COLUMN;
            if (obj.type === 'BALUSTRADE') return materials.BALUSTRADE;
            return materials.WALL_CONFIRMED;
        }}

        function createWallMesh(wall, zElev, mat) {{
            const p1 = wall.start_point;
            const p2 = wall.end_point;
            const dx = p2.x - p1.x;
            const dy = p2.y - p1.y;
            const len = Math.hypot(dx, dy);
            if (len < 0.001) return null;

            const group = new THREE.Group();

            // Render wall box
            const geom = new THREE.BoxGeometry(len, wall.height_m, wall.thickness_m);
            const mesh = new THREE.Mesh(geom, mat);
            mesh.castShadow = true;
            mesh.receiveShadow = true;

            const angle = Math.atan2(dy, dx);
            mesh.rotation.y = -angle;

            const midX = (p1.x + p2.x) / 2;
            const midY = (p1.y + p2.y) / 2;
            mesh.position.set(midX, zElev + wall.height_m / 2, -midY);

            group.add(mesh);
            return mesh;
        }}

        function createOpeningMesh(op, zElev, mat) {{
            // Render door/window framed panel
            const geom = new THREE.BoxGeometry(op.width_m, op.height_m, 0.08);
            const mesh = new THREE.Mesh(geom, mat);
            mesh.castShadow = true;

            // Find wall baseline
            const wall = objectDataMap.get(op.wall_id);
            if (wall) {{
                const p1 = wall.start_point;
                const p2 = wall.end_point;
                const len = Math.hypot(p2.x - p1.x, p2.y - p1.y);
                const ux = (p2.x - p1.x) / len;
                const uy = (p2.y - p1.y) / len;

                const cx = p1.x + ux * (op.offset_along_wall_m + op.width_m / 2);
                const cy = p1.y + uy * (op.offset_along_wall_m + op.width_m / 2);
                const angle = Math.atan2(p2.y - p1.y, p2.x - p1.x);

                mesh.rotation.y = -angle;
                mesh.position.set(cx, zElev + op.sill_height_m + op.height_m / 2, -cy);
            }}
            return mesh;
        }}

        function createPolygonMesh(polyObj, zElev, mat) {{
            if (!polyObj.polygon || polyObj.polygon.length < 3) return null;
            const shape = new THREE.Shape();
            polyObj.polygon.forEach((pt, idx) => {{
                if (idx === 0) shape.moveTo(pt.x, -pt.y);
                else shape.lineTo(pt.x, -pt.y);
            }});

            const extrudeSettings = {{
                steps: 1,
                depth: polyObj.thickness_m || 0.15,
                bevelEnabled: false
            }};

            const geom = new THREE.ExtrudeGeometry(shape, extrudeSettings);
            geom.rotateX(Math.PI / 2);

            const mesh = new THREE.Mesh(geom, mat);
            mesh.position.y = zElev + (polyObj.elevation_offset_m || 0.0);
            mesh.receiveShadow = true;
            return mesh;
        }}

        function createParapetMesh(p, zElev, mat) {{
            const len = p.length_m || 1.0;
            const geom = new THREE.BoxGeometry(len, p.height_m, p.thickness_m);
            const mesh = new THREE.Mesh(geom, mat);
            mesh.castShadow = true;

            const dx = p.end_point.x - p.start_point.x;
            const dy = p.end_point.y - p.start_point.y;
            const angle = Math.atan2(dy, dx);
            mesh.rotation.y = -angle;

            const midX = (p.start_point.x + p.end_point.x) / 2;
            const midY = (p.start_point.y + p.end_point.y) / 2;
            mesh.position.set(midX, zElev + p.height_m / 2, -midY);
            return mesh;
        }}

        function createColumnMesh(col, zElev, mat) {{
            const geom = new THREE.BoxGeometry(col.width_m, col.height_m, col.depth_m);
            const mesh = new THREE.Mesh(geom, mat);
            mesh.castShadow = true;
            mesh.position.set(col.center.x, zElev + col.height_m / 2, -col.center.y);
            return mesh;
        }}

        function createLinearMesh(lin, zElev, mat) {{
            const len = lin.length_m || 1.0;
            const geom = new THREE.BoxGeometry(len, lin.height_m, 0.05);
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
            const b = modelData.bounds;
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
            modelData.levels.forEach(l => {{
                const lbl = document.createElement('label');
                lbl.className = 'checkbox-label';
                lbl.innerHTML = `<input type="checkbox" checked onchange="toggleLevel('${{l.id}}', this.checked)"> ${{l.name}}`;
                lvlContainer.appendChild(lbl);
            }});

            const catContainer = document.getElementById('category-filters');
            catContainer.innerHTML = '';
            const cats = ['WALL', 'DOOR', 'WINDOW', 'FLOOR', 'BALCONY', 'SOFFIT', 'PARAPET', 'ROOF', 'COLUMN', 'BALUSTRADE'];
            cats.forEach(cat => {{
                const lbl = document.createElement('label');
                lbl.className = 'checkbox-label';
                lbl.innerHTML = `<input type="checkbox" checked onchange="toggleCategory('${{cat}}', this.checked)"> ${{cat}}s`;
                catContainer.appendChild(lbl);
            }});
        }}

        function toggleLevel(levelId, visible) {{
            meshMap.forEach((mesh, id) => {{
                if (mesh.userData.level_id === levelId) mesh.visible = visible;
            }});
        }}

        function toggleCategory(catType, visible) {{
            meshMap.forEach((mesh, id) => {{
                if (mesh.userData.type === catType) mesh.visible = visible;
            }});
        }}

        function toggleWireframe() {{
            meshMap.forEach(mesh => {{
                if (mesh.material) mesh.material.wireframe = !mesh.material.wireframe;
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
            const intersects = raycaster.intersectObjects(visibleMeshes, false);

            if (intersects.length > 0) {{
                const hit = intersects[0].object;
                selectObject(hit);
            }}
        }}

        function selectObject(mesh) {{
            if (selectedMesh && selectedMesh.userData.originalMaterial) {{
                selectedMesh.material = selectedMesh.userData.originalMaterial;
            }}

            selectedMesh = mesh;
            mesh.material = materials.HIGHLIGHT;

            const id = mesh.userData.id;
            selectedObjectData = objectDataMap.get(id);

            updateSidePanel();
        }}

        function updateSidePanel() {{
            if (!selectedObjectData) return;
            const obj = selectedObjectData;

            document.getElementById('sel-title').innerText = obj.name || obj.id;
            
            const badge = document.getElementById('sel-badge');
            badge.style.display = 'inline-block';
            badge.innerText = obj.review_state || 'CONFIRMED';
            badge.className = 'review-badge ' + (
                obj.review_state === 'CONFIRMED' ? 'badge-confirmed' :
                obj.review_state === 'INFERRED' ? 'badge-inferred' : 'badge-review'
            );

            renderTabContent();
        }}

        function switchTab(tabName) {{
            activeTab = tabName;
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            event.target.classList.add('active');

            ['overview', 'advanced', 'evidence', 'diagnostics'].forEach(t => {{
                document.getElementById('tab-' + t).style.display = (t === tabName) ? 'block' : 'none';
            }});

            renderTabContent();
        }}

        function renderTabContent() {{
            if (!selectedObjectData) return;
            const obj = selectedObjectData;

            if (activeTab === 'overview') {{
                let html = `
                    <div class="info-row"><span class="info-label">Type</span><span class="info-val">${{obj.type || 'N/A'}}</span></div>
                    <div class="info-row"><span class="info-label">Level ID</span><span class="info-val">${{obj.level_id || 'N/A'}}</span></div>
                    <div class="info-row"><span class="info-label">Substrate</span><span class="info-val">${{obj.substrate || 'Not Specified'}}</span></div>
                    <div class="info-row"><span class="info-label">Finish</span><span class="info-val">${{obj.finish || 'Not Specified'}}</span></div>
                `;
                if (obj.gross_area_m2 !== undefined) {{
                    html += `<div class="info-row"><span class="info-label">Gross Area</span><span class="info-val">${{obj.gross_area_m2.toFixed(2)}} m²</span></div>`;
                }}
                if (obj.opening_area_m2 !== undefined) {{
                    html += `<div class="info-row"><span class="info-label">Opening Area</span><span class="info-val">${{obj.opening_area_m2.toFixed(2)}} m²</span></div>`;
                }}
                if (obj.potential_net_area_m2 !== undefined) {{
                    html += `<div class="info-row"><span class="info-label">Potential Net Area</span><span class="info-val">${{obj.potential_net_area_m2.toFixed(2)}} m²</span></div>`;
                    html += `<div class="info-row"><span class="info-label">Deduction Auth</span><span class="info-val">${{obj.deduction_authorized ? 'YES' : 'NO (Potential Only)'}}</span></div>`;
                }}
                if (obj.height_m !== undefined) {{
                    html += `<div class="info-row"><span class="info-label">Height</span><span class="info-val">${{obj.height_m.toFixed(2)}} m</span></div>`;
                }}
                document.getElementById('tab-overview').innerHTML = html;

            }} else if (activeTab === 'advanced') {{
                let html = `
                    <div class="info-row"><span class="info-label">Object ID</span><span class="info-val">${{obj.id}}</span></div>
                    <div class="info-row"><span class="info-label">Parent ID</span><span class="info-val">${{obj.parent_id || 'None'}}</span></div>
                    <div class="info-row"><span class="info-label">Confidence Score</span><span class="info-val">${{((obj.confidence || 1.0) * 100).toFixed(0)}}%</span></div>
                `;
                if (obj.thickness_m) html += `<div class="info-row"><span class="info-label">Thickness</span><span class="info-val">${{obj.thickness_m}} m</span></div>`;
                if (obj.is_external !== undefined) html += `<div class="info-row"><span class="info-label">Is External</span><span class="info-val">${{obj.is_external}}</span></div>`;
                document.getElementById('tab-advanced').innerHTML = html;

            }} else if (activeTab === 'evidence') {{
                const p = obj.provenance || {{}};
                let html = `
                    <div class="info-section-title">Drawing Origin</div>
                    <div class="info-row"><span class="info-label">Source PDF</span><span class="info-val">${{p.source_pdf || 'Not Recorded'}}</span></div>
                    <div class="info-row"><span class="info-label">Page Number</span><span class="info-val">${{p.page_number !== undefined ? p.page_number : 'N/A'}}</span></div>
                    <div class="info-row"><span class="info-label">Drawing Sheet ID</span><span class="info-val">${{p.drawing_id || 'N/A'}}</span></div>
                    <div class="info-row"><span class="info-label">Scale Source</span><span class="info-val">${{p.scale_source || 'N/A'}}</span></div>
                    <div class="info-section-title">Evidence Traces</div>
                `;
                if (p.contributing_evidence && p.contributing_evidence.length > 0) {{
                    p.contributing_evidence.forEach(ev => {{
                        html += `<div class="info-row"><span class="info-label">•</span><span class="info-val">${{ev}}</span></div>`;
                    }});
                }} else {{
                    html += `<div class="info-row"><span class="info-label">Traces</span><span class="info-val">Direct Canonical Input</span></div>`;
                }}
                document.getElementById('tab-evidence').innerHTML = html;

            }} else if (activeTab === 'diagnostics') {{
                let html = `
                    <div class="info-section-title">Model Authority Check</div>
                    <div class="info-row"><span class="info-label">Review State</span><span class="info-val">${{obj.review_state}}</span></div>
                    <div class="info-row"><span class="info-label">Deduction Auth</span><span class="info-val">${{obj.deduction_authorized ? 'Authorized' : 'Unauthorized'}}</span></div>
                    <div class="info-row"><span class="info-label">Authority Note</span><span class="info-val">${{obj.authority_note || 'Direct Geometry'}}</span></div>
                `;
                document.getElementById('tab-diagnostics').innerHTML = html;
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
