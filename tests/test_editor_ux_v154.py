from pathlib import Path

import pb_editor_ux_v154 as ux


EXPECTED_INTERNAL = {
    "PB": "Plasterboard / Gyprock",
    "FC": "Fibre Cement Sheet",
    "MDF": "MDF / Timber Trim",
    "PLY": "Plywood",
    "CON": "Concrete / Masonry",
    "MET": "Metal / Steel",
}


def test_internal_substrate_presets_are_available():
    actual = {item["code"]: item["name"] for item in ux.INTERNAL_SUBSTRATES}
    assert actual == EXPECTED_INTERNAL
    assert all(str(item.get("color", "")).startswith("#") for item in ux.INTERNAL_SUBSTRATES)


def test_merge_presets_preserves_existing_and_is_idempotent():
    existing = [
        {"code": "EC1", "name": "Lineaboard Cladding", "color": "#111111"},
        {"code": "PB", "name": "Existing PB name", "color": "#222222"},
    ]
    once = ux.merge_substrate_presets(existing)
    twice = ux.merge_substrate_presets(once)

    codes = [item["code"] for item in once]
    assert codes.count("PB") == 1
    assert next(item for item in once if item["code"] == "PB")["name"] == "Existing PB name"
    for code in EXPECTED_INTERNAL:
        assert code in codes
    assert twice == once


def test_takeoff_studio_loads_zoom_patch_after_main_script():
    index = Path("planreader_takeoff_studio/frontend/index.html").read_text(encoding="utf-8")
    assert '<script src="studio.js"></script>' in index
    assert '<script src="zoom_patch.js"></script>' in index
    assert index.index("studio.js") < index.index("zoom_patch.js")


def test_zoom_patch_has_visible_controls_and_safe_limits():
    source = Path("planreader_takeoff_studio/frontend/zoom_patch.js").read_text(encoding="utf-8")
    assert 'id="pbZoomOut"' in source
    assert 'id="pbZoomFit"' in source
    assert 'id="pbZoomIn"' in source
    assert "MIN_ZOOM = 0.5" in source
    assert "MAX_ZOOM = 4.0" in source
    assert "pbZoomReadout" in source
    assert "MutationObserver" in source


def test_zoom_patch_does_not_emit_or_modify_measurement_values():
    source = Path("planreader_takeoff_studio/frontend/zoom_patch.js").read_text(encoding="utf-8")
    assert "streamlit:setComponentValue" not in source
    assert "pxPerM" not in source
    assert "manual_m2" not in source
    assert "areaM2" not in source
