from __future__ import annotations

import pb_substrate_qa_v131 as qa


class FakeApp:
    def __init__(self, pages):
        self.pages = pages

    def lquery(self, sql, params=()):
        if "FROM pages" in sql:
            return self.pages
        return []


def test_page_groups_separate_elevations_and_artist_impressions():
    app = FakeApp([
        {"id": 1, "page_type": "Elevation", "page_label": "A301 North Elevation"},
        {"id": 2, "page_type": "Render / Artist's Impression", "page_label": "Perspective 1"},
        {"id": 3, "page_type": "Finishes Schedule", "page_label": "External finishes"},
        {"id": 4, "page_type": "Floor Plan", "page_label": "Ground"},
    ])
    groups = qa._page_groups(app, 7)
    assert [p["id"] for p in groups["elevations"]] == [1]
    assert [p["id"] for p in groups["artists"]] == [2]
    assert [p["id"] for p in groups["support"]] == [3]


def test_schema_forces_review_statuses_and_evidence_fields():
    schema = qa._schema()
    surface = schema["properties"]["surfaces"]["items"]
    statuses = surface["properties"]["status"]["enum"]
    assert statuses == ["confirmed", "probable", "needs_check", "conflict"]
    required = set(surface["required"])
    assert {"surface_id", "substrate_code", "status", "elevation_reference", "artist_reference", "reason"} <= required


def test_qa_map_keeps_surface_ids_stable():
    payload = {
        "result": {
            "surfaces": [
                {"surface_id": "mass:4:front", "status": "needs_check"},
                {"surface_id": "mass:4:right", "status": "confirmed"},
            ]
        }
    }
    mapped = qa._qa_map(payload)
    assert mapped["mass:4:front"]["status"] == "needs_check"
    assert mapped["mass:4:right"]["status"] == "confirmed"


def test_status_colours_include_unresolved_states():
    assert "needs_check" in qa.STATUS_COLOURS
    assert "conflict" in qa.STATUS_COLOURS
    assert qa.STATUS_COLOURS["needs_check"] != qa.STATUS_COLOURS["confirmed"]
