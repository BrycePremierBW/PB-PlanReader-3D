from pb_canonical_building import ReviewState
from pb_production_3d_adapter import planreader_to_canonical_model


def _roof(project, roof_id):
    return next(
        roof
        for building in project.buildings
        for level in building.levels
        for roof in level.roofs
        if roof.id == roof_id
    )


def test_phase5m_roof_absolute_z_requires_explicit_level_elevation():
    payload = {
        "levels": [{"id": "L0", "name": "Ground", "elevation_m": 0.0}],
        "walls": [{
            "wall_ref": "W0",
            "level": "Ground",
            "a": {"x": 0, "y": 0},
            "b": {"x": 10, "y": 0},
            "height_m": 3.4,
            "height_status": "confirmed",
        }],
        "roof_data": {
            "evidence": {"pitches_deg": [22.5], "flat": False},
            "caps": [{
                "id": "cap_ground",
                "level": "Ground",
                "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}],
                "z": 3.4,
            }],
        },
    }
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    roof = _roof(project, "cap_ground")
    assert roof.elevation == 3.4
    assert roof.review_state == ReviewState.CONFIRMED


def test_phase5m_roof_ground_name_without_elevation_remains_unresolved():
    payload = {
        "walls": [{
            "wall_ref": "W0",
            "level": "Ground",
            "a": {"x": 0, "y": 0},
            "b": {"x": 10, "y": 0},
            "height_m": 3.4,
            "height_status": "confirmed",
        }],
        "roof_data": {
            "evidence": {"pitches_deg": [22.5], "flat": False},
            "caps": [{
                "id": "cap_unresolved",
                "level": "Ground",
                "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}],
                "z": 3.4,
            }],
        },
    }
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    roof = _roof(project, "cap_unresolved")
    assert roof.elevation is None
    assert roof.review_state == ReviewState.REVIEW_REQUIRED
    level = next(level for building in project.buildings for level in building.levels if roof in level.roofs)
    assert level.elevation_m is None


def test_phase5m_upper_storey_roof_uses_declared_storey_elevation_plus_local_cap_height():
    payload = {
        "levels": [{"id": "L1", "name": "Level 1", "elevation_m": 3.4}],
        "walls": [{
            "wall_ref": "W1",
            "level": "Level 1",
            "a": {"x": 0, "y": 0},
            "b": {"x": 10, "y": 0},
            "height_m": 3.0,
            "height_status": "confirmed",
        }],
        "roof_data": {
            "caps": [{
                "id": "cap_l1",
                "level": "Level 1",
                "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}],
                "z": 3.0,
            }],
        },
    }
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    roof = _roof(project, "cap_l1")
    assert roof.elevation == 6.4
    assert roof.review_state == ReviewState.CONFIRMED
