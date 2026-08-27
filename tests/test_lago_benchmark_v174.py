"""LAGO Birtinya authoritative opening-evidence benchmark (v1.7.4).

Authoritative sources:
  - PDF page 170 / index 169: CD6307/05 Window Schedule 01
  - PDF page 174 / index 173: CD6313/05 External Door Schedule 01
  - PDF page 179 / index 178: CD6319/05 Internal Door Schedule 01
  - PDF page 23 / index 22: CD1161/06 GA - Level 08
  - Premier Brushworks tender reference: 47 apartment entry doors

Safety authority:
  LAGO schedule WIDTH/HEIGHT headings are generic.  They do not prove a
  rough-opening basis, so schedule dimensions alone must never create an
  automatic wall-void deduction.

The GA fixture is a source-derived vector crop committed under tests/fixtures.
It intentionally does not force the offset ED04 callout onto geometry when B1
cannot prove the association.  A conservative miss/review is acceptable; a
confident false subtraction is not.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from pb_opening_schedule_v171 import (
    ScheduleEntry,
    detect_header,
    enrich_opening_evidence,
    extract_mark,
    parse_schedule_rows,
)
from pb_opening_evidence_v170 import (
    DEDUCTION_NOT_DEDUCTED,
    DEDUCTION_REVIEW,
    OpeningEvidence,
)
from pb_opening_deduction_v174 import apply_deductions
from pb_opening_reconciliation_v173 import reconcile_opening_evidence
from pb_plan_opening_detection_v171 import (
    Segment,
    TextWord,
    _classify_tag,
    plan_opening_candidates,
)

WINDOW_SCHEDULE_PAGE_1BASED = 170
WINDOW_SCHEDULE_PAGE_0BASED = 169
WINDOW_DRAWING_REF = "CD6307/05"

EXT_DOOR_SCHEDULE_PAGE_1BASED = 174
EXT_DOOR_SCHEDULE_PAGE_0BASED = 173
EXT_DOOR_DRAWING_REF = "CD6313/05"

INT_DOOR_SCHEDULE_PAGE_1BASED = 179
INT_DOOR_SCHEDULE_PAGE_0BASED = 178
INT_DOOR_DRAWING_REF = "CD6319/05"

GA08_PAGE_1BASED = 23
GA08_PAGE_0BASED = 22
GA08_DRAWING_REF = "CD1161/06"
GA08_SCALE_PT_PER_M = 28.346456693  # 1:100 @ A1

TENDER_TOTAL_ENTRY_DOORS = 47

WINDOW_HEADER = ["w#", "width", "height", "sill height"]
EXT_DOOR_HEADER = ["level/unit", "door type", "d#", "width", "height"]
INT_DOOR_HEADER = ["level/unit", "door type", "d#", "width", "height"]

# Complete source-derived rows from CD6307/05, page 170.
WINDOW_ROWS = [
    ("EW03", 2900, 2630),
    ("EW04", 1100, 2630),
    ("EW08", 1800, 1450),
    ("EW01", 900, 1665),
    ("EW02", 2000, 1000),
    ("EW05", 900, 1630),
    ("EW06", 900, 1630),
    ("EW07", 900, 1630),
    ("EW06", 2400, 1665),
    ("EW07", 1300, 1665),
    ("EW01", 900, 1665),
    ("EW02", 900, 1665),
    ("EW03", 1800, 1665),
    ("EW04", 2400, 1665),
]

# Complete source-derived rows from CD6313/05, page 174.
# Repeated marks belong to different LEVEL/UNIT rows.  Different dimensions
# therefore mean mark-only association is insufficient, not that the architect
# necessarily made a drafting error.
EXT_DOOR_ROWS = [
    ("ED18", 1000, 2340),
    ("ED19", 1000, 2340),
    ("ED26", 1000, 2040),
    ("ED28", 920, 2040),
    ("ED07", 2290, 2300),
    ("ED27", 2800, 2300),
    ("ED08", 1100, 2340),
    ("ED20", 1000, 2340),
    ("ED25", 1000, 2340),
    ("ED01", 3000, 2630),
    ("ED02", 2600, 2630),
    ("ED03", 2600, 2630),
    ("ED04", 2600, 2630),
    ("ED05", 2600, 2630),
    ("ED06", 2200, 2630),
    ("ED09", 2200, 2630),
    ("ED10", 2200, 2630),
    ("ED11", 2200, 2630),
    ("ED12", 2200, 2630),
    ("ED13", 2200, 2630),
    ("ED14", 2200, 2630),
    ("ED15", 2200, 2630),
    ("ED16", 2200, 2630),
    ("ED24", 3000, 2630),
    ("ED21", 2000, 2630),
    ("ED22", 1700, 2630),
    ("ED23", 1700, 2630),
    ("ED18", 1000, 2040),
    ("ED01", 3000, 2665),
    ("ED02", 2600, 2665),
    ("ED03", 2600, 2665),
    ("ED04", 3000, 2665),
    ("ED05", 2600, 2665),
    ("ED06", 2600, 2665),
    ("ED07", 2600, 2665),
    ("ED08", 2600, 2665),
    ("ED09", 2600, 2665),
    ("ED10", 2600, 2665),
    ("ED12", 3000, 2665),
    ("ED14", 3000, 2665),
    ("ED11", 1700, 2665),
    ("ED13", 2200, 2665),
]

# Complete source-derived rows from CD6319/05, page 179.
INT_DOOR_ROWS = [
    ("ID02", 1000, 2340),
    ("ID03", 1000, 2340),
    ("ID05", 1000, 2040),
    ("ID06", 1108, 2100),
    ("ID07", 1000, 2040),
    ("ID08", 1000, 2040),
    ("ID09", 1000, 2040),
    ("ID02", 1100, 2300),
    ("ID04", 3700, 2300),
    ("ID04", 1000, 2340),
    ("ID02", 1100, 2300),
    ("ID01", 1100, 2340),
    ("ID03", 1100, 2040),
    ("ID05", 1000, 2040),
    ("ID06", 920, 2040),
    ("ID07", 1520, 2040),
    ("ID08", 1720, 2040),
    ("ID03", 900, 2040),
    ("ID04", 900, 2040),
    ("ID05", 900, 2040),
    ("ID02", 1500, 2040),
    ("ID01", 1100, 2040),
    ("ID04", 900, 2040),
    ("ID05", 900, 2040),
    ("ID06", 900, 2040),
    ("ID02", 900, 2040),
    ("ID03", 900, 2040),
    ("ID01", 1100, 2040),
    ("ID02", 900, 2040),
    ("ID03", 900, 2040),
    ("ID04", 900, 2040),
]


def _make_parser_rows(header_words, data_rows):
    rows = [{"text": "\t".join(header_words), "bbox": (0, 0, 100, 20)}]
    for mark, width, height in data_rows:
        cells = [""] * len(header_words)
        for i, heading in enumerate(header_words):
            if heading in ("d#", "w#", "mark", "type", "code"):
                cells[i] = mark
            elif heading == "width":
                cells[i] = f"{width:,}"
            elif heading == "height":
                cells[i] = f"{height:,}"
            else:
                cells[i] = "-"
        rows.append({"text": "\t".join(cells), "bbox": (0, 0, 100, 20)})
    return rows


def _parsed(header, rows, page_no):
    return parse_schedule_rows(_make_parser_rows(header, rows), page_no=page_no)


def _plan_instance(mark):
    return OpeningEvidence(
        type_mark=mark,
        width_m=None,
        height_m=None,
        dimension_basis="",
        dimension_source="plan_detection",
        extraction_method="plan_detection",
        page_no=0,
    )


def _fixture_path():
    return Path(__file__).resolve().parent / "fixtures" / "lago_b1_ga08_ed04_cluster.json"


def _load_b1_fixture():
    with _fixture_path().open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _fixture_inputs():
    fixture = _load_b1_fixture()
    segments = [
        Segment(
            x1=row["x1"], y1=row["y1"],
            x2=row["x2"], y2=row["y2"],
            drawing_index=row.get("drawing_index", 0),
        )
        for row in fixture["segments"]
    ]
    words = [
        TextWord(
            text=row["text"], x0=row["x0"], y0=row["y0"],
            x1=row["x1"], y1=row["y1"], page_no=GA08_PAGE_1BASED,
        )
        for row in fixture["words"]
    ]
    return fixture, segments, words


class TestLAGO_SourceReferences(unittest.TestCase):
    def test_schedule_page_numbers(self):
        self.assertEqual((WINDOW_SCHEDULE_PAGE_1BASED, WINDOW_SCHEDULE_PAGE_0BASED), (170, 169))
        self.assertEqual((EXT_DOOR_SCHEDULE_PAGE_1BASED, EXT_DOOR_SCHEDULE_PAGE_0BASED), (174, 173))
        self.assertEqual((INT_DOOR_SCHEDULE_PAGE_1BASED, INT_DOOR_SCHEDULE_PAGE_0BASED), (179, 178))

    def test_ga08_page_numbers(self):
        self.assertEqual((GA08_PAGE_1BASED, GA08_PAGE_0BASED), (23, 22))
        self.assertEqual(GA08_DRAWING_REF, "CD1161/06")

    def test_schedule_drawing_references(self):
        self.assertEqual(WINDOW_DRAWING_REF, "CD6307/05")
        self.assertEqual(EXT_DOOR_DRAWING_REF, "CD6313/05")
        self.assertEqual(INT_DOOR_DRAWING_REF, "CD6319/05")

    def test_source_row_counts(self):
        self.assertEqual((len(WINDOW_ROWS), len(EXT_DOOR_ROWS), len(INT_DOOR_ROWS)), (14, 42, 31))

    def test_tender_entry_door_count(self):
        self.assertEqual(TENDER_TOTAL_ENTRY_DOORS, 47)


class TestLAGO_MarkRecognition(unittest.TestCase):
    def test_b2_real_lago_marks_are_structural_opening_marks(self):
        self.assertEqual(extract_mark("EW03"), "EW03")
        self.assertEqual(extract_mark("L8.00 ED04"), "ED04")
        self.assertEqual(extract_mark("BA.00 ID02"), "ID02")
        self.assertEqual(extract_mark("D01"), "D01")
        self.assertEqual(extract_mark("W01"), "W01")

    def test_b2_roman_numerals_are_not_opening_marks(self):
        for value in ("I", "II", "III", "IV", "IX", "X"):
            self.assertEqual(extract_mark(value), "", value)

    def test_b1_real_lago_tag_classes(self):
        self.assertEqual(_classify_tag("ED04"), "door")
        self.assertEqual(_classify_tag("ID02"), "door")
        self.assertEqual(_classify_tag("EW03"), "window")
        self.assertEqual(_classify_tag("D01"), "door")
        self.assertEqual(_classify_tag("W01"), "window")

    def test_b1_roman_numerals_are_not_tags(self):
        for value in ("I", "II", "III", "IV", "IX", "X"):
            self.assertEqual(_classify_tag(value), "", value)


class TestLAGO_ScheduleBasisDetection(unittest.TestCase):
    def test_window_heading_unknown_basis(self):
        header = detect_header(WINDOW_HEADER)
        self.assertIn("width", header)
        self.assertIn("height", header)
        self.assertEqual(header["dimension_basis"], "")

    def test_external_door_heading_unknown_basis(self):
        header = detect_header(EXT_DOOR_HEADER)
        self.assertIn("width", header)
        self.assertIn("height", header)
        self.assertEqual(header["dimension_basis"], "")

    def test_internal_door_heading_unknown_basis(self):
        header = detect_header(INT_DOOR_HEADER)
        self.assertIn("width", header)
        self.assertIn("height", header)
        self.assertEqual(header["dimension_basis"], "")


class TestLAGO_ScheduleParsing(unittest.TestCase):
    def test_window_schedule_all_rows(self):
        entries = _parsed(WINDOW_HEADER, WINDOW_ROWS, WINDOW_SCHEDULE_PAGE_1BASED)
        self.assertEqual(len(entries), 14)
        self.assertEqual([(e.type_mark, e.width_mm, e.height_mm) for e in entries], WINDOW_ROWS)
        self.assertTrue(all(e.dimension_basis == "" for e in entries))

    def test_external_door_schedule_all_rows(self):
        entries = _parsed(EXT_DOOR_HEADER, EXT_DOOR_ROWS, EXT_DOOR_SCHEDULE_PAGE_1BASED)
        self.assertEqual(len(entries), 42)
        self.assertEqual([(e.type_mark, e.width_mm, e.height_mm) for e in entries], EXT_DOOR_ROWS)
        self.assertTrue(all(e.dimension_basis == "" for e in entries))

    def test_internal_door_schedule_all_rows(self):
        entries = _parsed(INT_DOOR_HEADER, INT_DOOR_ROWS, INT_DOOR_SCHEDULE_PAGE_1BASED)
        self.assertEqual(len(entries), 31)
        self.assertEqual([(e.type_mark, e.width_mm, e.height_mm) for e in entries], INT_DOOR_ROWS)
        self.assertTrue(all(e.dimension_basis == "" for e in entries))

    def test_exact_window_source_value(self):
        entries = _parsed(WINDOW_HEADER, WINDOW_ROWS, WINDOW_SCHEDULE_PAGE_1BASED)
        self.assertEqual((entries[0].type_mark, entries[0].width_mm, entries[0].height_mm),
                         ("EW03", 2900, 2630))

    def test_exact_external_door_source_value(self):
        entries = _parsed(EXT_DOOR_HEADER, EXT_DOOR_ROWS, EXT_DOOR_SCHEDULE_PAGE_1BASED)
        ed04 = [(e.width_mm, e.height_mm) for e in entries if e.type_mark == "ED04"]
        self.assertEqual(ed04, [(2600, 2630), (3000, 2665)])

    def test_duplicate_marks_are_preserved(self):
        entries = _parsed(WINDOW_HEADER, WINDOW_ROWS, WINDOW_SCHEDULE_PAGE_1BASED)
        self.assertEqual(len([e for e in entries if e.type_mark == "EW02"]), 2)
        self.assertEqual(len([e for e in entries if e.type_mark == "EW03"]), 2)
        self.assertEqual(len([e for e in entries if e.type_mark == "EW04"]), 2)


class TestLAGO_MarkOnlyAssociationAmbiguity(unittest.TestCase):
    def _assert_ambiguous(self, mark, schedule_entries):
        result = enrich_opening_evidence([_plan_instance(mark)], schedule_entries)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].width_m)
        ambiguous = [o for o in result[0].source_observations if o.get("status") == "ambiguous"]
        self.assertEqual(len(ambiguous), 1)
        return ambiguous[0]

    def test_ew02_mark_only_association_is_ambiguous(self):
        entries = _parsed(WINDOW_HEADER, WINDOW_ROWS, WINDOW_SCHEDULE_PAGE_1BASED)
        self._assert_ambiguous("EW02", [e for e in entries if e.type_mark == "EW02"])

    def test_ew03_mark_only_association_is_ambiguous(self):
        entries = _parsed(WINDOW_HEADER, WINDOW_ROWS, WINDOW_SCHEDULE_PAGE_1BASED)
        self._assert_ambiguous("EW03", [e for e in entries if e.type_mark == "EW03"])

    def test_ew04_mark_only_association_is_ambiguous(self):
        entries = _parsed(WINDOW_HEADER, WINDOW_ROWS, WINDOW_SCHEDULE_PAGE_1BASED)
        self._assert_ambiguous("EW04", [e for e in entries if e.type_mark == "EW04"])

    def test_ed04_mark_only_association_is_ambiguous(self):
        entries = _parsed(EXT_DOOR_HEADER, EXT_DOOR_ROWS, EXT_DOOR_SCHEDULE_PAGE_1BASED)
        obs = self._assert_ambiguous("ED04", [e for e in entries if e.type_mark == "ED04"])
        alternatives = {(a["width_mm"], a["height_mm"]) for a in obs["alternatives"]}
        self.assertEqual(alternatives, {(2600, 2630), (3000, 2665)})

    def test_ew01_identical_rows_can_enrich(self):
        entries = _parsed(WINDOW_HEADER, WINDOW_ROWS, WINDOW_SCHEDULE_PAGE_1BASED)
        ew01 = [e for e in entries if e.type_mark == "EW01"]
        self.assertEqual([(e.width_mm, e.height_mm) for e in ew01], [(900, 1665), (900, 1665)])
        result = enrich_opening_evidence([_plan_instance("EW01")], ew01)
        self.assertAlmostEqual(result[0].width_m, 0.9, places=3)
        self.assertAlmostEqual(result[0].height_m, 1.665, places=3)
        self.assertEqual(result[0].dimension_basis, "unknown")


class TestLAGO_EndToEndScheduleSafety(unittest.TestCase):
    def test_generic_internal_schedule_never_enables_deduction(self):
        entries = _parsed(INT_DOOR_HEADER, [("ID02", 1000, 2340)], INT_DOOR_SCHEDULE_PAGE_1BASED)
        inst = OpeningEvidence(
            type_mark="ID02",
            width_m=None,
            height_m=None,
            dimension_basis="",
            dimension_source="plan_detection",
            extraction_method="plan_detection",
            page_no=14,
            geometry_confidence=0.85,
            dimension_confidence=0.0,
            association_confidence=0.80,
            wall_ref="W-ID02",
        )
        enriched = enrich_opening_evidence([inst], entries)
        self.assertEqual(enriched[0].dimension_basis, "unknown")
        reconciled, _ = reconcile_opening_evidence(enriched)
        reconciled[0].compute_deduction_status()
        self.assertEqual(reconciled[0].deduction_status, DEDUCTION_REVIEW)
        result = apply_deductions(reconciled)
        self.assertFalse(result[0].deduct)
        self.assertEqual(result[0].deduction_decision, DEDUCTION_NOT_DEDUCTED)

    def test_generic_window_schedule_never_enables_deduction(self):
        entries = _parsed(WINDOW_HEADER, [("EW01", 900, 1665)], WINDOW_SCHEDULE_PAGE_1BASED)
        inst = OpeningEvidence(
            type_mark="EW01",
            width_m=None,
            height_m=None,
            dimension_basis="",
            dimension_source="plan_detection",
            extraction_method="plan_detection",
            page_no=23,
            geometry_confidence=0.80,
            dimension_confidence=0.0,
            association_confidence=0.75,
            wall_ref="W-EW01",
        )
        enriched = enrich_opening_evidence([inst], entries)
        reconciled, _ = reconcile_opening_evidence(enriched)
        reconciled[0].compute_deduction_status()
        self.assertEqual(reconciled[0].deduction_status, DEDUCTION_REVIEW)
        self.assertFalse(apply_deductions(reconciled)[0].deduct)


class TestLAGO_B1_GA08Fixture(unittest.TestCase):
    def test_fixture_is_self_contained_and_source_traced(self):
        fixture = _load_b1_fixture()
        source = fixture["source"]
        self.assertEqual(source["pdf_page_1based"], 23)
        self.assertEqual(source["pdf_page_0based"], 22)
        self.assertEqual(source["drawing_ref"], GA08_DRAWING_REF)
        self.assertEqual(source["scale_a1"], "1:100")
        self.assertEqual(fixture["expected"]["source_tags"], ["ED04"])

    def test_fixture_contains_real_ed04_word(self):
        fixture = _load_b1_fixture()
        tags = [w for w in fixture["words"] if w["text"] == "ED04"]
        self.assertEqual(len(tags), 1)
        self.assertEqual(
            [tags[0]["x0"], tags[0]["y0"], tags[0]["x1"], tags[0]["y1"]],
            fixture["expected"]["tag_bboxes"]["ED04"],
        )

    def test_fixture_contains_verified_door_leaf_vectors(self):
        fixture = _load_b1_fixture()
        actual = {
            tuple(round(row[key], 3) for key in ("x1", "y1", "x2", "y2"))
            for row in fixture["segments"]
        }
        for expected in fixture["expected"]["verified_door_leaf_segments"]:
            self.assertIn(tuple(round(v, 3) for v in expected), actual)

    def test_unmocked_b1_runs_on_checked_in_real_vectors(self):
        fixture, segments, words = _fixture_inputs()
        result = plan_opening_candidates(
            segments=segments,
            words=words,
            scale_px_per_m=fixture["source"]["scale_pt_per_m"],
            page_no=GA08_PAGE_1BASED,
            min_wall_length_pt=200.0,
        )
        self.assertGreater(result.wall_lines_found, 0)
        self.assertGreater(len(result.candidates), 0)
        for candidate in result.candidates:
            self.assertEqual(candidate.page_no, GA08_PAGE_1BASED)
            self.assertEqual(candidate.dimension_basis, "unknown")
            self.assertFalse(candidate.deduct)

    def test_real_vector_tag_is_never_retyped_as_window(self):
        fixture, segments, words = _fixture_inputs()
        result = plan_opening_candidates(
            segments=segments,
            words=words,
            scale_px_per_m=fixture["source"]["scale_pt_per_m"],
            page_no=GA08_PAGE_1BASED,
            min_wall_length_pt=200.0,
        )
        for candidate in result.candidates:
            if candidate.type_mark == "ED04":
                self.assertEqual(candidate.opening_type, "door")

    def test_real_vector_pipeline_remains_non_deducting(self):
        fixture, segments, words = _fixture_inputs()
        b1 = plan_opening_candidates(
            segments=segments,
            words=words,
            scale_px_per_m=fixture["source"]["scale_pt_per_m"],
            page_no=GA08_PAGE_1BASED,
            min_wall_length_pt=200.0,
        )
        schedule = _parsed(EXT_DOOR_HEADER, EXT_DOOR_ROWS, EXT_DOOR_SCHEDULE_PAGE_1BASED)
        ed04_schedule = [entry for entry in schedule if entry.type_mark == "ED04"]
        enriched = enrich_opening_evidence(b1.candidates, ed04_schedule)
        reconciled, _ = reconcile_opening_evidence(enriched)
        for candidate in reconciled:
            candidate.compute_deduction_status()
        deducted = apply_deductions(reconciled)
        self.assertTrue(deducted)
        self.assertFalse(any(candidate.deduct for candidate in deducted))


if __name__ == "__main__":
    unittest.main()
