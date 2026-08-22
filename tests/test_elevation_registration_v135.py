from pb_elevation_registration_v135 import (
    best_dimension_match,
    dimension_candidates_m,
    footprint_facades,
    orientation_from_text,
)


def test_cardinal_elevation_titles_are_recognised():
    assert orientation_from_text("A-301 NORTH ELEVATION") == "North"
    assert orientation_from_text("Elevation - East") == "East"
    assert orientation_from_text("SOUTH FACADE") == "South"
    assert orientation_from_text("West façade") == "West"
    assert orientation_from_text("Elevation 1") == ""


def test_dimension_candidates_convert_mm_to_metres():
    values = dimension_candidates_m("Overall 18450. Window 1200. RL 17.450")
    assert 18.45 in values
    assert 1.2 in values


def test_rectangle_footprint_builds_four_facades():
    prisms = [{
        "id": "ground",
        "points": [(0.0, 0.0), (10.0, 0.0), (10.0, 6.0), (0.0, 6.0)],
        "level_name": "Ground",
        "level_index": 0,
        "confidence": "Measured plan geometry",
    }]
    facades = footprint_facades(prisms)
    assert facades["North"]["projected_width_m"] == 10.0
    assert facades["South"]["projected_width_m"] == 10.0
    assert facades["East"]["projected_width_m"] == 6.0
    assert facades["West"]["projected_width_m"] == 6.0
    assert sum(len(facades[s]["segments"]) for s in facades) == 4


def test_dimension_match_scores_close_cross_view_width():
    result = best_dimension_match(18.45, [4.2, 18.44, 22.0])
    assert result["dimension_m"] == 18.44
    assert result["difference_pct"] < 0.1
    assert result["confidence"] >= 99
