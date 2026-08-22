from pb_elevation_profile_v136 import rl_values, solve_height_from_text
from pb_unified_building_v139 import wall_cells, takeoff_rows
from pb_roof_envelope_v140 import _PITCH_RE


def test_rl_height_solver_prefers_storey_difference():
    solved = solve_height_from_text("GROUND RL 12.400\nFCL RL 15.100")
    assert solved["height_m"] == 2.7
    assert solved["confidence"] == "Verified"


def test_wall_cells_cut_rectangular_opening():
    cells = wall_cells(4.0, 3.0, [(1.0, 2.0, 0.0, 2.1)])
    area = sum((x1-x0)*(z1-z0) for x0,x1,z0,z1 in cells)
    assert round(area, 3) == round(12.0 - 2.1, 3)


def test_takeoff_uses_same_net_wall_area():
    rows = takeoff_rows([{
        "wall_ref":"N01","side":"North","substrate":"Render",
        "gross_m2":20.0,"opening_deduction_m2":3.5,"net_m2":16.5,
        "height_confidence":"Verified","substrate_confidence":"High",
        "height_status":"Verified from RL difference","substrate_status":"Resolved",
    }])
    assert rows[0]["quantity"] == 16.5
    assert rows[0]["confidence"] == "Measured"


def test_roof_pitch_pattern_reads_explicit_pitch():
    match = _PITCH_RE.search("ROOF PITCH: 22.5 deg")
    assert match and float(match.group(1)) == 22.5
