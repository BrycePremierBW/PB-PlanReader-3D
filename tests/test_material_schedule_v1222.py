from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

import pb_material_schedule_v1222 as mat


class _App:
    def __init__(self, pages):
        self.pages = pages

    def lquery(self, sql, params=()):
        if "FROM pages" in sql:
            return [dict(row) for row in self.pages]
        return []


class MaterialScheduleV1222Tests(unittest.TestCase):
    def test_schedule_code_resolves_substrate(self):
        rows = mat.parse_schedule_text(
            "FINISH SCHEDULE\nEC1 - James Hardie Linea 180mm cladding\nEC2 - Textureboard cladding\nPT1 - Dulux Natural White low sheen",
            9,
            "A900",
        )
        by_code = {row["code"]: row for row in rows}
        self.assertEqual(by_code["EC1"]["substrate"], "Lineaboard Cladding")
        self.assertEqual(by_code["EC2"]["substrate"], "Textureboard Cladding")
        self.assertIn("Dulux Natural White", by_code["PT1"]["finish"])

    def test_conflicting_schedule_definition_is_not_silently_confirmed(self):
        app = _App([
            {"id": 1, "page_label": "A900", "page_type": "Finishes Schedule", "extracted_text": "EC1 Linea cladding", "image_path": "", "document_id": 1, "page_no": 1, "render_zoom": 1},
            {"id": 2, "page_label": "A901", "page_type": "Finishes Schedule", "extracted_text": "EC1 Rendered blockwork", "image_path": "", "document_id": 1, "page_no": 2, "render_zoom": 1},
        ])
        state = mat.build_material_dictionary(app, 4)
        self.assertEqual(state["dictionary"]["EC1"]["status"], "Conflict")
        self.assertTrue(any(issue["category"] == "Schedule conflict" for issue in state["issues"]))

    def test_resolved_code_replaces_raw_code_for_geometry(self):
        resolver = {"EC1": {"status": "Confirmed", "substrate": "Lineaboard Cladding"}}
        token = mat._resolver_context.set(resolver)
        try:
            rows = mat.resolved_substrates_from_text(lambda _text: [{"code": "EC1", "name": "EC1"}], "South elevation EC1")
        finally:
            mat._resolver_context.reset(token)
        self.assertEqual(rows, [{"code": "EC1", "name": "Lineaboard Cladding"}])

    def test_unknown_code_occurrence_becomes_review_issue(self):
        original = mat.auto._pdf_word_lines
        try:
            mat.auto._pdf_word_lines = lambda _app, _page: [{"text": "EC9", "bbox": [10, 20, 40, 35], "center": [25, 27]}]
            page = {"id": 3, "page_label": "A301", "page_type": "Elevation", "extracted_text": "EC9"}
            rows = mat._page_occurrences(object(), page, {})
        finally:
            mat.auto._pdf_word_lines = original
        self.assertEqual(rows[0]["status"], "Unknown")
        self.assertEqual(rows[0]["bbox"], [10, 20, 40, 35])

    def test_issue_preview_draws_marker_without_full_resolution_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sheet.png"
            Image.new("RGB", (2400, 1600), "white").save(path)
            payload = mat.issue_preview_bytes(
                {"image_path": str(path)},
                {"category": "Unknown material code", "bbox": [200, 300, 700, 600], "bbox_mode": "xyxy"},
            )
            self.assertTrue(payload)
            from io import BytesIO
            with Image.open(BytesIO(payload)) as image:
                self.assertLessEqual(max(image.size), 1200)


if __name__ == "__main__":
    unittest.main()
