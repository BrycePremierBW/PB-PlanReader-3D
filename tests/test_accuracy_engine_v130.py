from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ["PLANREADER_DATA_DIR"] = tempfile.mkdtemp(prefix="planreader_v130_")
os.environ["JOBHUB_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="planreader_v130_jobhub_")) / "jobhub.db")

import fitz
import pb_planreader_3d_app as app
import pb_vector_geometry_v130 as vector
import pb_accuracy_benchmark_v130 as benchmark

app.init_local_db()
benchmark.apply(app)


class VectorGeometryTests(unittest.TestCase):
    def test_snap_geometry_merges_near_endpoints(self):
        segs = [
            {"id": "a", "x1": 0, "y1": 0, "x2": 100, "y2": 0},
            {"id": "b", "x1": 100.5, "y1": 0.2, "x2": 100.5, "y2": 80},
        ]
        graph = vector.snap_geometry(segs, tolerance_pt=1.0)
        self.assertEqual(len(graph["nodes"]), 3)
        self.assertEqual(sum(1 for n in graph["nodes"] if n["degree"] == 2), 1)

    def test_wall_pair_detection_uses_parallel_overlap(self):
        segs = [
            {"id": "a", "x1": 0, "y1": 0, "x2": 200, "y2": 0},
            {"id": "b", "x1": 5, "y1": 10, "x2": 195, "y2": 10},
            {"id": "c", "x1": 0, "y1": 100, "x2": 0, "y2": 200},
        ]
        pairs = vector.detect_wall_pairs(segs, px_per_m=100.0)
        self.assertTrue(any(p["face_a"] == "a" and p["face_b"] == "b" for p in pairs))
        pair = next(p for p in pairs if p["face_a"] == "a" and p["face_b"] == "b")
        self.assertAlmostEqual(float(pair["wall_width_m"]), 0.1, places=3)

    def test_scale_solver_requires_independent_evidence_for_verified(self):
        result = vector.solve_scale([
            {"method": "dimension_line", "px_per_m": 100.0, "weight": 5},
            {"method": "dimension_line", "px_per_m": 101.0, "weight": 5},
            {"method": "printed_scale", "px_per_m": 100.2, "weight": 1},
        ])
        self.assertTrue(result["verified"])
        self.assertGreaterEqual(result["confidence"], 80)
        self.assertLess(abs(result["px_per_m"] - 100.5), 1.0)

    def test_native_pdf_extracts_vector_lines_and_words(self):
        doc = fitz.open()
        page = doc.new_page(width=500, height=400)
        page.draw_line((50, 50), (300, 50))
        page.draw_line((50, 62), (300, 62))
        page.insert_text((100, 100), "2500")
        native = vector.extract_native_page(page)
        self.assertGreaterEqual(native["segment_count"], 2)
        self.assertTrue(any(w["text"] == "2500" for w in native["words"]))
        doc.close()


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.wid = app.lexecute(
            "INSERT INTO workspaces(job_no,job_name,builder_client,site_address,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("BENCH", "Benchmark", "PB", "Test", "Draft", app.now_stamp(), app.now_stamp()),
        )

    def test_numeric_ground_truth_reports_mape(self):
        benchmark.upsert_truth(app, self.wid, "floor_area", "unit-1", expected_numeric=100.0, unit="m²")
        benchmark.record_prediction(app, self.wid, "floor_area", "unit-1", predicted_numeric=102.0, unit="m²", confidence=95, method="test", engine_version="1.3.0")
        report = benchmark.evaluate_workspace(app, self.wid, "1.3.0")
        self.assertAlmostEqual(report["categories"]["floor_area"]["mape"], 0.02, places=6)
        self.assertTrue(report["categories"]["floor_area"]["passes_target"])

    def test_text_ground_truth_reports_accuracy(self):
        benchmark.upsert_truth(app, self.wid, "finish_association", "wall-1", expected_text="PT01")
        benchmark.record_prediction(app, self.wid, "finish_association", "wall-1", predicted_text="PT01", confidence=90, method="test", engine_version="1.3.0")
        report = benchmark.evaluate_workspace(app, self.wid, "1.3.0")
        self.assertEqual(report["categories"]["finish_association"]["accuracy"], 1.0)
        self.assertTrue(report["categories"]["finish_association"]["passes_target"])

    def test_missing_prediction_is_visible(self):
        benchmark.upsert_truth(app, self.wid, "door_count", "level-1", expected_numeric=10, unit="No.")
        report = benchmark.evaluate_workspace(app, self.wid, "1.3.0")
        self.assertEqual(report["categories"]["door_count"]["matched_rate"], 0.0)
        self.assertEqual(report["details"][0]["error"], "missing_prediction")


if __name__ == "__main__":
    unittest.main()
