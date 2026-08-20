from __future__ import annotations

import tempfile
from pathlib import Path

import pb_code_register_v1225 as codes
import pb_page_registration_v1225 as registration
import pb_premier_takeoff_v1225 as pb
import pb_registration_priority_guard_v1225 as priority


def test_title_block_partition_plan_beats_elevation_references():
    full_text = """
    LEVEL 05 PARTITION PLAN
    REFER ELEVATION A-301 FOR EXTERNAL FINISHES
    SECTION 4/A-502
    NORTH ELEVATION REFERENCE ONLY
    """
    kind, confidence, evidence = priority.weighted_page_type(
        full_text,
        "architectural_combined.pdf",
        "A-205\nLEVEL 05 PARTITION PLAN\nSCALE 1:100",
    )
    assert kind == "Floor Plan"
    assert confidence >= 80
    assert "title block" in evidence


def test_title_block_rcp_beats_floor_plan_references():
    kind, confidence, _ = priority.weighted_page_type(
        "REFER FLOOR PLAN A-201. CEILING DETAILS BELOW.",
        "plans.pdf",
        "A-401\nLEVEL 02 REFLECTED CEILING PLAN\n1:100",
    )
    assert kind == "Reflected Ceiling Plan"
    assert confidence >= 80


def test_drawing_number_rejects_finish_code_but_accepts_issued_sheet_no():
    assert registration._candidate_code("PT01 KEY PLAN WALL PIECES") == ""
    assert registration._candidate_code("PT01 KEY PLAN WALL PIECES | DRAWING NO A-205 | REV B") == "A-205"


def test_fallback_classification_does_not_turn_partition_plan_into_elevation():
    page_type, _label = priority.classify_page(
        "LEVEL 03 PARTITION PLAN\nREFER EXTERNAL ELEVATION A301\nSEE SECTION A501",
        "architectural.pdf",
        8,
    )
    assert page_type == "Floor Plan"


class _SettingsApp:
    def __init__(self):
        self.settings = {}

    def now_stamp(self):
        return "2026-08-20T10:00:00"

    def lquery(self, sql, params=()):
        if "FROM workspace_settings" in sql:
            value = self.settings.get((int(params[0]), str(params[1])))
            return [{"value": value}] if value is not None else []
        return []

    def lexecute(self, sql, params=()):
        if "INSERT INTO workspace_settings" in sql:
            self.settings[(int(params[0]), str(params[1]))] = str(params[2])
        return 1


def test_manual_code_override_resolves_unknown_code():
    app = _SettingsApp()
    codes.set_manual_code(
        app,
        7,
        "EC7",
        "James Hardie Linea 180 mm weatherboard",
        "Lineaboard Cladding",
        "Dulux Weathershield low sheen",
        "External walls / cladding",
        "Confirmed by estimator from finish schedule A601",
    )

    def base_builder(_app, _workspace_id):
        return {
            "dictionary": {},
            "schedule_pages": [],
            "issues": [{"category": "Unknown material code", "code": "EC7"}],
        }

    state = codes.merged_dictionary(app, 7, base_builder)
    assert state["dictionary"]["EC7"]["status"] == "Confirmed"
    assert state["dictionary"]["EC7"]["manual"] is True
    assert state["dictionary"]["EC7"]["substrate"] == "Lineaboard Cladding"
    assert not [issue for issue in state["issues"] if issue.get("code") == "EC7"]


class _PBApp:
    def __init__(self):
        self.pages = [
            {"id": 1, "document_id": 1, "page_no": 2, "page_label": "A-201", "page_type": "Floor Plan", "extracted_text": "LEVEL 05 PARTITION PLAN UNIT 501 UNIT 502", "file_name": "Arch.pdf", "selected": 1},
            {"id": 2, "document_id": 1, "page_no": 3, "page_label": "A-401", "page_type": "Reflected Ceiling Plan", "extracted_text": "LEVEL 05 REFLECTED CEILING PLAN CEIL-01", "file_name": "Arch.pdf", "selected": 1},
            {"id": 3, "document_id": 1, "page_no": 5, "page_label": "A-501", "page_type": "Elevation", "extracted_text": "NORTH ELEVATION EC1", "file_name": "Arch.pdf", "selected": 1},
            {"id": 4, "document_id": 1, "page_no": 7, "page_label": "A-601", "page_type": "Finishes Schedule", "extracted_text": "EC1 LINEA CLADDING\nCEIL-01 BUILDERS WHITE FLAT", "file_name": "Arch.pdf", "selected": 1},
        ]
        self.takeoff = [
            {"id": 10, "section": "Internal", "element": "Floor area", "location": "Unit 501", "substrate": "Other", "finish_system": "To be confirmed", "quantity": 80.0, "unit": "m²", "quantity_status": "Measured", "source_page": "A-201", "source_reference": "PB Auto Geometry v1.2.19 · unit:501", "confidence": "Measured", "notes": "", "row_role": "floor_area"},
            {"id": 11, "section": "Internal", "element": "Floor area", "location": "Unit 502", "substrate": "Other", "finish_system": "To be confirmed", "quantity": 90.0, "unit": "m²", "quantity_status": "Measured", "source_page": "A-201", "source_reference": "PB Auto Geometry v1.2.19 · unit:502", "confidence": "Measured", "notes": "", "row_role": "floor_area"},
            {"id": 12, "section": "External", "element": "External walls / cladding", "location": "North · Linea", "substrate": "Lineaboard Cladding", "finish_system": "Exterior acrylic", "quantity": 120.0, "unit": "m²", "quantity_status": "Measured", "source_page": "A-501", "source_reference": "PB Auto Geometry v1.2.19 · facade:3 · EC1", "confidence": "Documented", "notes": "", "row_role": ""},
        ]

    def now_stamp(self):
        return "2026-08-20T10:00:00"

    def lquery(self, sql, params=()):
        if "FROM pages p JOIN documents" in sql:
            return list(self.pages)
        if "FROM takeoff_rows" in sql:
            return list(self.takeoff)
        if "FROM workspace_settings" in sql:
            return []
        return []


def test_pb_takeoff_structure_matches_premier_workflow():
    app = _PBApp()
    original_material_state = pb._material_state
    try:
        pb._material_state = lambda _app, _workspace_id: {
            "dictionary": {
                "EC1": {"code": "EC1", "description": "Linea cladding", "substrate": "Lineaboard Cladding", "finish": "Dulux Weathershield low sheen", "status": "Confirmed"},
                "CEIL-01": {"code": "CEIL-01", "description": "Builders White Flat ceiling", "substrate": "Plasterboard", "finish": "Builders White Flat", "status": "Confirmed"},
            }
        }
        state = pb.build_pb_schedule(app, 1)
    finally:
        pb._material_state = original_material_state

    rows = state["rows"]
    floor_rows = [row for row in rows if "floor area" in row["element"].lower()]
    assert len(floor_rows) == 2
    assert sum(row["net_qty"] for row in floor_rows) == 170.0

    ceilings = [row for row in rows if row["element"] == "General painted ceilings"]
    assert len(ceilings) == 1
    assert ceilings[0]["net_qty"] == 170.0
    assert "floor-area basis" in ceilings[0]["qty_basis"].lower()

    walls = [row for row in rows if row["element"] == "Internal walls"]
    assert len(walls) == 1
    assert walls[0]["net_qty"] == 0
    assert walls[0]["confidence"] == "Pending"
    assert "surface measurement required" in walls[0]["qty_basis"].lower()

    entries = [row for row in rows if row["element"] == "Apartment entry doors & frames"]
    assert len(entries) == 1
    assert entries[0]["net_qty"] == 2

    external = [row for row in rows if row["element"] == "External walls / cladding"]
    assert len(external) == 1
    assert external[0]["net_qty"] == 120.0
    assert external[0]["finish_code"] == "EC1"


def test_mapper_preflight_never_treats_blank_path_as_a_file():
    assert registration.memory.regular_file("") is None
    assert registration.memory.regular_file(".") is None
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "page.png"
        p.write_bytes(b"not-an-image-but-a-regular-file")
        assert registration.memory.regular_file(str(p)) == p


if __name__ == "__main__":
    tests = [name for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for name in tests:
        globals()[name]()
    print(f"{len(tests)} v1.2.25 registration/PB take-off tests passed")
