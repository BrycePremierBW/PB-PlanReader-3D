"""Phase 5M regression for calibrated floor geometry with unresolved storey.

Weak/free-form sheet text is not storey authority, but valid metric XY geometry
must not be discarded solely because the vertical/storey identity is unresolved.
"""

from pb_canonical_building import ReviewState
from pb_production_3d_adapter import planreader_to_canonical_model


def test_calibrated_xy_floor_with_weak_sheet_text_is_retained_unresolved():
    payload = {
        "workspace_id": 101,
        "mapper_shapes": [{
            "box_id": "weak-level-floor",
            "page_width_px": 1000.0,
            "page_height_px": 1000.0,
            "px_per_m": 50.0,
            "raw_box": {"x": 10.0, "y": 10.0, "w": 20.0, "h": 20.0},
            "_source_level": "Ground Floor",
            "_source_level_label": "Ground Floor",
        }],
    }

    project, skipped = planreader_to_canonical_model(
        payload,
        is_validated_internal_workspace=True,
    )

    building = project.buildings[0]
    unresolved = [level for level in building.levels if level.id == "lvl_unresolved_review"]
    assert len(unresolved) == 1

    level = unresolved[0]
    assert level.elevation_m is None
    assert level.review_state == ReviewState.REVIEW_REQUIRED
    assert level.metadata.get("registered_storey") is False

    assert len(level.floors) == 1
    floor = level.floors[0]
    assert floor.level_id == level.id
    assert floor.review_state == ReviewState.REVIEW_REQUIRED
    assert floor.takeoff_eligible is False
    assert floor.metadata.get("level_status") == "unresolved"

    # Percentage -> pixel -> metre conversion remains physical in XY:
    # 10% of 1000 px = 100 px / 50 px/m = 2.0 m
    # 30% of 1000 px = 300 px / 50 px/m = 6.0 m
    assert floor.polygon[0].x == 2.0
    assert floor.polygon[0].y == 2.0
    assert floor.polygon[2].x == 6.0
    assert floor.polygon[2].y == 6.0

    # No trusted Ground level may be manufactured from the weak sheet text.
    assert not any(
        level.name.strip().lower() in {"ground", "ground floor"}
        and level.elevation_m == 0.0
        for level in building.levels
    )

    # Geometry was retained, so the floor must not be reported as skipped merely
    # because the storey identity is unresolved.
    assert not any(
        item.get("type") == "FLOOR" and item.get("id") == "weak-level-floor"
        for item in skipped
    )
