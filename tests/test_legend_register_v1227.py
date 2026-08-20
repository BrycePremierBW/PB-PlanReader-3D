from __future__ import annotations

import json

import pb_legend_register_v1227 as legend


def test_legend_parser_registers_general_level_and_material_abbreviations():
    text = """
    LEGEND & ABBREVIATIONS
    AFFL  ABOVE FINISHED FLOOR LEVEL
    FCL - FINISHED CEILING LEVEL
    TYP : TYPICAL
    EC7 - JAMES HARDIE LINEA 180MM EXTERNAL CLADDING
    PT01 - KEY PLAN WALL PIECES
    """
    rows = legend.parse_legend_text(text, 4, "A-001")
    by_code = {item["code"]: item for item in rows}
    assert by_code["AFFL"]["category"] == "level / dimension"
    assert by_code["FCL"]["category"] == "level / dimension"
    assert by_code["TYP"]["category"] == "drawing abbreviation"
    assert by_code["EC7"]["category"] == "material / substrate"
    assert by_code["EC7"]["substrate"] == "Lineaboard Cladding"
    assert by_code["PT01"]["category"] == "drawing tag"


def test_non_finish_pt01_legend_tag_never_enters_material_dictionary():
    state = {
        "dictionary": {
            "PT01": {
                "code": "PT01", "description": "KEY PLAN WALL PIECES", "category": "drawing tag",
                "status": "Confirmed", "substrate": "", "finish": "",
                "sources": [{"page_id": 1, "page_label": "A-001", "source_line": "PT01 KEY PLAN WALL PIECES"}],
            },
            "EC7": {
                "code": "EC7", "description": "James Hardie Linea external cladding", "category": "material / substrate",
                "status": "Confirmed", "substrate": "Lineaboard Cladding", "finish": "",
                "sources": [{"page_id": 1, "page_label": "A-001", "source_line": "EC7 James Hardie Linea external cladding"}],
            },
        }
    }
    entries = legend.legend_material_entries(state)
    assert "PT01" not in entries
    assert entries["EC7"]["substrate"] == "Lineaboard Cladding"


def test_expansion_only_adds_abbreviations_actually_used_on_drawing():
    dictionary = {
        "AFFL": {"description": "Above Finished Floor Level", "status": "Confirmed"},
        "FCL": {"description": "Finished Ceiling Level", "status": "Confirmed"},
        "BAD": {"description": "Conflicting definition", "status": "Conflict"},
    }
    expanded = legend.expand_abbreviations("2700 AFFL\nFCL 5400", dictionary)
    assert "AFFL = Above Finished Floor Level" in expanded
    assert "FCL = Finished Ceiling Level" in expanded
    assert "BAD" not in expanded


def test_explicit_material_schedule_wins_and_legend_fills_missing_codes():
    base = {
        "dictionary": {
            "EC1": {"code": "EC1", "description": "Explicit schedule Linea", "substrate": "Lineaboard Cladding", "finish": "", "status": "Confirmed", "sources": []},
        },
        "issues": [],
    }
    state = {
        "legend_pages": [1],
        "dictionary": {
            "EC1": {"code": "EC1", "description": "External cladding type 1", "category": "material / substrate", "status": "Confirmed", "substrate": "", "finish": "", "sources": [{"page_id": 1, "page_label": "A-001", "source_line": "EC1 External cladding type 1"}]},
            "EC7": {"code": "EC7", "description": "James Hardie Linea external cladding", "category": "material / substrate", "status": "Confirmed", "substrate": "Lineaboard Cladding", "finish": "", "sources": [{"page_id": 1, "page_label": "A-001", "source_line": "EC7 Linea"}]},
        },
    }
    merged = legend.merge_legend_into_material_dictionary(base, state)
    assert merged["dictionary"]["EC1"]["description"] == "Explicit schedule Linea"
    assert merged["dictionary"]["EC1"]["legend_cross_reference"] == "External cladding type 1"
    assert merged["dictionary"]["EC7"]["legend"] is True


class _App:
    def __init__(self):
        self.settings = {}
        self.rows = [
            {"id": 1, "page_no": 1, "page_label": "A-001", "page_type": legend.PAGE_TYPE, "extracted_text": "LEGEND & ABBREVIATIONS\nAFFL - ABOVE FINISHED FLOOR LEVEL\nEC7 - JAMES HARDIE LINEA EXTERNAL CLADDING", "document_id": 1, "image_path": "", "render_zoom": 1.0, "selected": 1},
            {"id": 2, "page_no": 2, "page_label": "A-002", "page_type": legend.PAGE_TYPE, "extracted_text": "LEGEND\nEC9 - DESELECTED CLADDING", "document_id": 1, "image_path": "", "render_zoom": 1.0, "selected": 0},
            {"id": 3, "page_no": 3, "page_label": "A-201", "page_type": "Floor Plan", "extracted_text": "LEVEL 1 PLAN AFFL", "document_id": 1, "image_path": "", "render_zoom": 1.0, "selected": 1},
        ]

    def lquery(self, sql, params=()):
        if "FROM pages" in sql:
            return [dict(row) for row in self.rows if row["selected"]]
        return []

    def set_workspace_setting(self, workspace_id, key, value):
        self.settings[(int(workspace_id), str(key))] = str(value)

    def workspace_setting(self, workspace_id, key, default=""):
        return self.settings.get((int(workspace_id), str(key)), default)

    def now_stamp(self):
        return "2026-08-20T23:10:00"


def test_only_selected_legend_pages_build_active_dictionary():
    app = _App()
    original = legend.auto._pdf_word_lines
    try:
        legend.auto._pdf_word_lines = lambda _app, _page: []
        state = legend.build_legend_register(app, 7)
    finally:
        legend.auto._pdf_word_lines = original
    assert state["legend_pages"] == [1]
    assert "AFFL" in state["dictionary"]
    assert "EC7" in state["dictionary"]
    assert "EC9" not in state["dictionary"]
    assert any(item["code"] == "AFFL" and item["page_id"] == 3 for item in state["occurrences"])


def test_legend_sheet_detection_is_strong_but_key_plan_is_not_a_legend():
    assert legend.is_legend_page({"page_type": "Other", "page_label": "A001", "extracted_text": "ARCHITECTURAL LEGEND\nAFFL - ABOVE FINISHED FLOOR LEVEL\nFCL - FINISHED CEILING LEVEL"})
    assert not legend.is_legend_page({"page_type": "Floor Plan", "page_label": "A201", "extracted_text": "KEY PLAN\nPT01 KEY PLAN WALL PIECES\nLEVEL 01 FLOOR PLAN"})


if __name__ == "__main__":
    tests = [name for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for name in tests:
        globals()[name]()
    print(f"{len(tests)} v1.2.27 legend-register tests passed")
