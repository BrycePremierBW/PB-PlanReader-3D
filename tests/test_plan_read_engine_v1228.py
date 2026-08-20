from __future__ import annotations

import os
import tempfile
from pathlib import Path

import fitz
import pandas as pd

import pb_mapper_hard_guard_v1228 as mapper_guard
import pb_plan_read_engine_v1228 as reader


def test_spatial_table_reconstruction_pairs_legend_columns():
    doc = fitz.open()
    page = doc.new_page(width=800, height=600)
    page.insert_text((60, 100), "AFFL", fontsize=10)
    page.insert_text((220, 100), "Above Finished Floor Level", fontsize=10)
    page.insert_text((60, 120), "EC7", fontsize=10)
    page.insert_text((220, 120), "James Hardie Linea external cladding", fontsize=10)
    result = reader.reconstruct_page_text(page, {"page_type": "Legend / Abbreviations / Key"})
    text = result["text"]
    assert "AFFL" in text and "Above Finished Floor Level" in text
    assert "EC7" in text and "James Hardie Linea external cladding" in text
    assert result["word_count"] >= 8
    doc.close()


def test_spatial_title_block_reads_separate_field_cells():
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((610, 500), "DRAWING NO", fontsize=7)
    page.insert_text((735, 500), "A-205", fontsize=10)
    page.insert_text((610, 520), "DRAWING TITLE", fontsize=7)
    page.insert_text((700, 520), "LEVEL 05 PARTITION PLAN", fontsize=11)
    page.insert_text((610, 545), "SCALE", fontsize=7)
    page.insert_text((700, 545), "1:100", fontsize=9)
    page.insert_text((610, 565), "REV", fontsize=7)
    page.insert_text((700, 565), "B", fontsize=9)

    evidence = reader.spatial_title_block_evidence(page, lambda _page: {})
    assert evidence["drawing_no"] == "A-205"
    assert "PARTITION PLAN" in evidence.get("drawing_title", "").upper()
    assert evidence["scale"] == "1:100"
    assert evidence["revision"] == "B"
    doc.close()


def test_split_dimension_tokens_rebuild_3_space_600():
    doc = fitz.open()
    page = doc.new_page(width=800, height=600)
    page.insert_text((300, 200), "3", fontsize=9)
    page.insert_text((307, 200), "600", fontsize=9)
    tokens = reader._dimension_tokens(page)
    values = {item["compact"] for item in tokens}
    assert "3600" in values
    assert reader._dimension_value_m("3 600") == 3.6
    doc.close()


def test_visual_fallback_is_not_enabled_without_gemini_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    class App:
        def _gemini_generate(self, *args, **kwargs):
            raise AssertionError("should not be called")
    assert reader._visual_read_allowed(App(), {"selected": 1, "image_path": "/does/not/exist.png"}, {"word_count": 0, "char_count": 0}) is False


class _MapperApp:
    def __init__(self, rows):
        self.rows = rows
        self.seen_ids = []
        self.hero = lambda workspace: None
        self.st = type("St", (), {
            "error": lambda self, *args, **kwargs: None,
            "info": lambda self, *args, **kwargs: None,
            "dataframe": lambda self, *args, **kwargs: None,
        })()
        self.pd = pd

    def lquery(self, sql, params=()):
        if "FROM pages WHERE workspace_id" in sql:
            return [dict(row) for row in self.rows if int(row.get("selected") or 0) == 1]
        return []

    def process_document(self, *args, **kwargs):
        return 0, "no-op"

    def ldf(self, sql, params=()):
        if "FROM pages WHERE workspace_id" in sql:
            return pd.DataFrame(self.rows)
        return pd.DataFrame()

    def plan_mapper_page(self, workspace):
        frame = self.ldf("SELECT * FROM pages WHERE workspace_id=? ORDER BY id", (workspace["id"],))
        self.seen_ids = list(frame["id"].astype(int))
        return self.seen_ids


def test_mapper_lists_only_selected_pages_with_real_regular_files():
    with tempfile.TemporaryDirectory() as tmp:
        valid = Path(tmp) / "sheet.png"
        valid.write_bytes(b"image bytes")
        app = _MapperApp([
            {"id": 1, "workspace_id": 9, "document_id": 1, "page_no": 1, "selected": 1, "image_path": str(valid), "page_label": "A101"},
            {"id": 2, "workspace_id": 9, "document_id": 1, "page_no": 2, "selected": 1, "image_path": "", "page_label": "A102"},
            {"id": 3, "workspace_id": 9, "document_id": 1, "page_no": 3, "selected": 0, "image_path": "", "page_label": "A103"},
        ])
        mapper_guard.apply(app)
        result = app.plan_mapper_page({"id": 9})
        assert result == [1]
        assert app.seen_ids == [1]


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        if "monkeypatch" in test.__code__.co_varnames:
            continue
        test()
    print(f"{len(tests) - 1} v1.2.28 direct tests passed")
