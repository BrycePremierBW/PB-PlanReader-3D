from __future__ import annotations

import sqlite3
import unittest

from pb_3d_surface_editor_v1212 import (
    build_surface_takeoff_rows,
    completion_summary,
    default_surface_state,
    derive_mass_surfaces,
    infer_substrate,
    normalise_override,
    selected_surface_from_event,
    surface_records,
    _replace_rows,
)


class SurfaceEditorTests(unittest.TestCase):
    def mass(self, **changes):
        data = {
            "id": 7,
            "label": "Block A",
            "level_name": "Ground",
            "x": 0,
            "y": 0,
            "z": 0,
            "width": 4,
            "depth": 3,
            "height": 2.5,
            "finish": "Rendered block",
            "confidence": "Measured",
            "source_reference": "A-401",
        }
        data.update(changes)
        return data

    def test_cuboid_generates_six_stable_faces_with_correct_areas(self):
        surfaces = derive_mass_surfaces(self.mass())
        self.assertEqual(len(surfaces), 6)
        by_face = {item["face"]: item for item in surfaces}
        self.assertEqual(by_face["front"]["surface_id"], "mass:7:front")
        self.assertEqual(by_face["front"]["area_m2"], 10.0)
        self.assertEqual(by_face["rear"]["area_m2"], 10.0)
        self.assertEqual(by_face["left"]["area_m2"], 7.5)
        self.assertEqual(by_face["right"]["area_m2"], 7.5)
        self.assertEqual(by_face["top"]["area_m2"], 12.0)
        self.assertEqual(by_face["bottom"]["area_m2"], 12.0)

    def test_invalid_mass_does_not_create_fake_surfaces(self):
        self.assertEqual(derive_mass_surfaces(self.mass(width=0)), [])
        self.assertEqual(derive_mass_surfaces(self.mass(id=0)), [])

    def test_default_states_are_safe(self):
        surfaces = derive_mass_surfaces(self.mass())
        front = next(item for item in surfaces if item["face"] == "front")
        underside = next(item for item in surfaces if item["face"] == "bottom")
        self.assertEqual(default_surface_state(front)["status"], "Provisional")
        self.assertEqual(default_surface_state(underside)["status"], "Excluded")
        self.assertEqual(default_surface_state(front)["substrate"], "RBL")

    def test_override_is_kept_separate_from_geometry(self):
        surface = derive_mass_surfaces(self.mass())[0]
        state = normalise_override(
            {"substrate": "SOF", "status": "Paint Included", "progress_pct": 145, "notes": "check access"},
            surface,
        )
        self.assertEqual(state["substrate"], "SOF")
        self.assertEqual(state["status"], "Paint Included")
        self.assertEqual(state["progress_pct"], 100.0)
        self.assertNotIn("area_m2", state)

    def test_progress_summary_excludes_excluded_faces(self):
        surfaces = derive_mass_surfaces(self.mass())
        overrides = {
            "mass:7:front": {"status": "Paint Included", "progress_pct": 50},
            "mass:7:rear": {"status": "Excluded", "progress_pct": 100},
        }
        records = surface_records(surfaces, overrides)
        # Other non-bottom faces remain provisional and are part of the review total.
        result = completion_summary(records)
        expected_total = 10 + 7.5 + 7.5 + 12  # front + left + right + top; rear/bottom excluded
        self.assertEqual(result["total_m2"], expected_total)
        self.assertEqual(result["completed_m2"], 5.0)

    def test_measured_mass_surface_sync_remains_provisional_and_unapproved(self):
        surface = derive_mass_surfaces(self.mass())[0]
        record = surface_records(
            [surface],
            {surface["surface_id"]: {"substrate": "RBL", "status": "Paint Included", "progress_pct": 65}},
        )[0]
        row = build_surface_takeoff_rows([record])[0]
        self.assertEqual(row["quantity"], 10.0)
        self.assertEqual(row["quantity_status"], "Provisional measured")
        self.assertEqual(row["confidence"], "Derived")
        self.assertEqual(row["inclusion_status"], "INCLUSION")
        self.assertEqual(row["rate_per_unit"], 0)
        self.assertEqual(row["coats"], 0)
        self.assertEqual(row["productivity_m2_per_hour"], 0)
        self.assertEqual(row["row_role"], "model_surface")
        self.assertEqual(row["commercial_authority_status"], "REVIEW_REQUIRED")
        self.assertEqual(row["commercial_authority_source"], "A-401")
        self.assertEqual(row["commercial_authority_reviewed_by"], "")
        self.assertEqual(row["commercial_authority_reviewed_at"], "")
        self.assertEqual(row["commercial_authority_fingerprint"], "")
        self.assertIn("Source model confidence: Measured", row["notes"])
        self.assertIn("Commercial estimator review is required", row["notes"])
        self.assertIn("Progress 65%", row["notes"])

    def test_assumed_mass_surface_remains_provisional(self):
        surface = derive_mass_surfaces(self.mass(confidence="Assumed"))[0]
        record = surface_records([surface], {surface["surface_id"]: {"status": "Paint Included"}})[0]
        row = build_surface_takeoff_rows([record])[0]
        self.assertEqual(row["quantity_status"], "Provisional measured")
        self.assertEqual(row["confidence"], "Derived")
        self.assertEqual(row["commercial_authority_status"], "REVIEW_REQUIRED")
        self.assertIn("Source model confidence: Assumed", row["notes"])

    def test_verified_mass_without_source_cannot_manufacture_authority(self):
        surface = derive_mass_surfaces(
            self.mass(confidence="Verified", source_reference="")
        )[0]
        record = surface_records(
            [surface], {surface["surface_id"]: {"status": "Paint Included"}}
        )[0]
        row = build_surface_takeoff_rows([record])[0]
        self.assertEqual(row["quantity_status"], "Provisional measured")
        self.assertEqual(row["confidence"], "Derived")
        self.assertEqual(row["commercial_authority_status"], "REVIEW_REQUIRED")
        self.assertEqual(row["commercial_authority_source"], "")

    def test_replace_rows_persists_fail_closed_authority_metadata(self):
        class DbApp:
            def __init__(self):
                self.conn = sqlite3.connect(":memory:")
                self.conn.execute(
                    """CREATE TABLE takeoff_rows (
                        workspace_id INTEGER, section TEXT, element TEXT, location TEXT,
                        substrate TEXT, finish_system TEXT, quantity REAL, unit TEXT,
                        quantity_status TEXT, source_page TEXT, source_reference TEXT,
                        inclusion_status TEXT, coats REAL, coverage_m2_per_litre REAL,
                        productivity_m2_per_hour REAL, rate_per_unit REAL, confidence TEXT,
                        notes TEXT, row_role TEXT, commercial_authority_status TEXT,
                        commercial_authority_source TEXT, commercial_authority_reviewed_by TEXT,
                        commercial_authority_reviewed_at TEXT, commercial_authority_fingerprint TEXT,
                        created_at TEXT, updated_at TEXT
                    )"""
                )

            def lexecute(self, sql, params=()):
                self.conn.execute(sql, params)
                self.conn.commit()

            def now_stamp(self):
                return "2026-09-04T10:00:00+10:00"

        surface = derive_mass_surfaces(self.mass())[0]
        record = surface_records(
            [surface], {surface["surface_id"]: {"status": "Paint Included"}}
        )[0]
        app = DbApp()
        _replace_rows(app, 101, build_surface_takeoff_rows([record]))
        saved = app.conn.execute(
            """SELECT quantity_status, confidence, row_role,
                      commercial_authority_status, commercial_authority_source,
                      commercial_authority_reviewed_by, commercial_authority_reviewed_at,
                      commercial_authority_fingerprint
                 FROM takeoff_rows"""
        ).fetchone()
        self.assertEqual(
            saved,
            (
                "Provisional measured", "Derived", "model_surface",
                "REVIEW_REQUIRED", "A-401", "", "", "",
            ),
        )
        app.conn.close()

    def test_event_selection_prefers_customdata_then_curve_number(self):
        trace_ids = ["mass:7:front", "mass:7:rear"]
        event = {"selection": {"points": [{"customdata": "mass:7:rear", "curve_number": 0}]}}
        self.assertEqual(selected_surface_from_event(event, trace_ids), "mass:7:rear")
        event2 = {"selection": {"points": [{"curve_number": 1}]}}
        self.assertEqual(selected_surface_from_event(event2, trace_ids), "mass:7:rear")

    def test_substrate_inference_is_conservative(self):
        self.assertEqual(infer_substrate("Soffit FC sheet"), "SOF")
        self.assertEqual(infer_substrate("Rendered block"), "RBL")
        self.assertEqual(infer_substrate("unknown finish"), "OTHER")


if __name__ == "__main__":
    unittest.main()
