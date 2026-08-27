from pathlib import Path

from PIL import Image

import pb_material_preview_guard_v152 as guard
import pb_material_schedule_v1222 as material


def test_normalise_bbox_orders_and_clamps_xyxy():
    box = guard.normalise_bbox([140, 150, -20, -10], "xyxy", 100, 80)
    assert box == (0.0, 0.0, 99.0, 79.0)


def test_normalise_bbox_clamps_xywh_past_bottom_edge():
    box = guard.normalise_bbox([95, 150, 30, 40], "xywh", 100, 80)
    assert box is not None
    x0, y0, x1, y1 = box
    assert 0 <= x0 <= x1 <= 99
    assert 0 <= y0 <= y1 <= 79


def test_guarded_preview_does_not_crash_for_bbox_below_image(tmp_path: Path):
    image_path = tmp_path / "page.png"
    Image.new("RGB", (120, 90), "white").save(image_path)

    guard.apply()
    result = material.issue_preview_bytes(
        {"image_path": str(image_path)},
        {
            "category": "Unknown material code",
            "bbox": [20, 1000, 80, 1040],
            "bbox_mode": "xyxy",
        },
    )

    assert isinstance(result, bytes)
    assert len(result) > 100


def test_guarded_preview_handles_reversed_bbox(tmp_path: Path):
    image_path = tmp_path / "page.png"
    Image.new("RGB", (120, 90), "white").save(image_path)

    guard.apply()
    result = material.issue_preview_bytes(
        {"image_path": str(image_path)},
        {"category": "Review", "bbox": [90, 70, 10, 5], "bbox_mode": "xyxy"},
    )

    assert isinstance(result, bytes)
    assert len(result) > 100
