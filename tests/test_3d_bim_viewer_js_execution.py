import json
import shutil
import subprocess

import pytest

from pb_bim_viewer import generate_bim_viewer_html


def _extract_function(html: str, name: str) -> str:
    marker = f"function {name}("
    start = html.index(marker)
    brace = html.index("{", start)
    depth = 0
    in_string = None
    escaped = False
    for idx in range(brace, len(html)):
        ch = html[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start:idx + 1]
    raise AssertionError(f"Could not extract JS function {name}")


def test_phase5m_js_code_structure():
    html = generate_bim_viewer_html({})
    assert "elevOff" not in html
    assert "mesh.position.y = renderZ;" in html
    assert "if (!op || op.is_host_attached === false || !op.wall_id) return null;" in html
    for state in ("wrong_host", "wrong_level", "invalid_geometry", "conflict_overlap", "evidence_only"):
        assert state in html


def test_phase5m_js_runtime_executes_polygon_and_opening_functions(tmp_path):
    """Actually execute the production viewer functions in Node.

    This is intentionally stronger than node --check or string inspection: a
    runtime ReferenceError inside either function fails the test.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")

    html = generate_bim_viewer_html({})
    polygon_fn = _extract_function(html, "createPolygonMesh")
    opening_fn = _extract_function(html, "createOpeningMesh")

    script = f"""
class Position {{
  constructor() {{ this.x = 0; this.y = 0; this.z = 0; }}
  set(x, y, z) {{ this.x = x; this.y = y; this.z = z; }}
}}
class Shape {{
  constructor() {{ this.holes = []; this.points = []; }}
  moveTo(x,y) {{ this.points.push([x,y]); }}
  lineTo(x,y) {{ this.points.push([x,y]); }}
  closePath() {{}}
}}
class Path extends Shape {{}}
class ShapeGeometry {{ constructor(shape) {{ this.shape=shape; }} rotateX(v) {{ this.rx=v; }} }}
class ExtrudeGeometry {{
  constructor(shape, settings) {{ this.shape=shape; this.settings=settings; }}
  rotateX(v) {{ this.rx=v; }}
  translate(x,y,z) {{ this.translation=[x,y,z]; }}
}}
class BoxGeometry {{ constructor(...args) {{ this.args=args; }} }}
class Mesh {{
  constructor(geometry, material) {{
    this.geometry=geometry; this.material=material; this.position=new Position();
    this.rotation={{x:0,y:0,z:0}}; this.castShadow=false; this.receiveShadow=false;
  }}
}}
const THREE = {{ Shape, Path, ShapeGeometry, ExtrudeGeometry, BoxGeometry, Mesh }};
const objectDataMap = new Map();
{polygon_fn}
{opening_fn}
function check(cond, msg) {{ if (!cond) throw new Error(msg); }}
const tri = [{{x:0,y:0}},{{x:4,y:0}},{{x:0,y:3}}];
let floor = createPolygonMesh({{type:'FLOOR', polygon:tri, thickness_m:null, elevation:null, elevation_offset_m:0}}, 2.5, {{}});
check(floor !== null, 'floor polygon returned null');
check(Math.abs(floor.position.y - 2.5) < 1e-9, 'floor renderZ wrong');
let thick = createPolygonMesh({{type:'FLOOR', polygon:tri, thickness_m:0.2, elevation:null, elevation_offset_m:0.4}}, 2.5, {{}});
check(thick !== null, 'thick polygon returned null');
check(Math.abs(thick.position.y - 2.9) < 1e-9, 'relative polygon renderZ wrong');
let roof = createPolygonMesh({{type:'ROOF', polygon:tri, thickness_m:null, elevation:6.4, elevation_offset_m:null}}, null, {{}});
check(roof !== null, 'absolute roof returned null');
check(Math.abs(roof.position.y - 6.4) < 1e-9, 'absolute roof renderZ wrong');
let unknownRoof = createPolygonMesh({{type:'ROOF', polygon:tri, thickness_m:null, elevation:null, elevation_offset_m:null}}, null, {{}});
check(unknownRoof === null, 'unknown roof should fail closed');
objectDataMap.set('wall_W1', {{id:'wall_W1', start_point:{{x:0,y:0}}, end_point:{{x:10,y:0}}}});
const valid = {{id:'op1', wall_id:'wall_W1', is_host_attached:true, physical_state:'physical_not_authorised', width_m:1, height_m:2.1, sill_height_m:0, offset_along_wall_m:2}};
let opMesh = createOpeningMesh(valid, 0, {{}});
check(opMesh !== null, 'valid hosted opening returned null');
check(Math.abs(opMesh.position.y - 1.05) < 1e-9, 'opening vertical position wrong');
let wrong = createOpeningMesh({{...valid, id:'op2', wall_id:null, is_host_attached:false, physical_state:'wrong_host'}}, 0, {{}});
check(wrong === null, 'wrong-host opening created a mesh');
let wrongLevel = createOpeningMesh({{...valid, id:'op3', physical_state:'wrong_level'}}, 0, {{}});
check(wrongLevel === null, 'wrong-level opening created a mesh');
console.log(JSON.stringify({{floorY:floor.position.y, thickY:thick.position.y, roofY:roof.position.y, openingY:opMesh.position.y, wrongHost:null}}));
"""
    js_path = tmp_path / "phase5m_viewer_runtime.js"
    js_path.write_text(script, encoding="utf-8")
    proc = subprocess.run([node, str(js_path)], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {
        "floorY": 2.5,
        "thickY": 2.9,
        "roofY": 6.4,
        "openingY": 1.05,
        "wrongHost": None,
    }
