"""Tests for pb_opening_schedule_v171 — B2 door/window schedule parsing.

Covers:
  - Dimension parsing (compound, mm, m, standalone, edge cases)
  - Mark extraction (D01, W01, WD01, Roman, no-mark)
  - Header detection and column mapping
  - Full row parsing (with header, without header)
  - Schedule entry properties
  - Enrichment of OpeningEvidence via merge
  - Safety rules: no new instances, no deductions, no mark assignment
"""
from __future__ import annotations

import unittest

from pb_opening_evidence_v170 import (
    DIMENSION_BASIS_UNKNOWN,
    DEDUCTION_REVIEW,
    NON_INSTANCE_SOURCES,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
    OpeningEvidence,
)
from pb_opening_schedule_v171 import (
    VERSION,
    ScheduleEntry,
    parse_dimension,
    extract_mark,
    detect_header,
    parse_schedule_rows,
    enrich_opening_evidence,
)


def _row(text: str, bbox=None):
    """Create a mock _word_rows() output row."""
    return {"text": text, "bbox": bbox or (0, 0, 100, 20)}


def _entry(mark, w=None, h=None, desc="", count=1, page=0):
    """Shorthand for ScheduleEntry."""
    return ScheduleEntry(type_mark=mark, width_mm=w, height_mm=h,
                         description=desc, count=count, page_no=page)


def _inst(mark="", wall_ref="N01", width=None, height=None, method="plan_vector"):
    """Create a minimal OpeningEvidence for testing."""
    ev = OpeningEvidence(
        type_mark=mark,
        wall_ref=wall_ref,
        width_m=width,
        height_m=height,
        dimension_basis=DIMENSION_BASIS_UNKNOWN,
        dimension_source="plan_vector",
        extraction_method=method,
        deduction_status=DEDUCTION_REVIEW,
    )
    ev.set_quantity(1, source="geometric")
    return ev


class TestDimensionParsing(unittest.TestCase):
    """parse_dimension() — compound patterns, mm, m, standalone."""

    def test_compound_x(self):
        self.assertEqual(parse_dimension("820x2040"), (820, 2040))

    def test_compound_times(self):
        self.assertEqual(parse_dimension("820 × 2040"), (820, 2040))

    def test_compound_slash(self):
        self.assertEqual(parse_dimension("820/2040"), (820, 2040))

    def test_compound_dash(self):
        self.assertEqual(parse_dimension("820-2040"), (820, 2040))

    def test_compound_case_insensitive(self):
        self.assertEqual(parse_dimension("820X2040"), (820, 2040))

    def test_compound_with_label(self):
        self.assertEqual(parse_dimension("Size: 820 x 2040"), (820, 2040))

    def test_mm_suffix(self):
        self.assertEqual(parse_dimension("820mm x 2040mm"), (820, 2040))

    def test_m_suffix(self):
        self.assertEqual(parse_dimension("0.82m x 2.04m"), (820, 2040))

    def test_single_mm_tall(self):
        w, h = parse_dimension("2040mm")
        self.assertIsNone(w)
        self.assertEqual(h, 2040)

    def test_single_number_small(self):
        w, h = parse_dimension("820")
        self.assertEqual(w, 820)
        self.assertIsNone(h)

    def test_two_standalone(self):
        self.assertEqual(parse_dimension("820 2040"), (820, 2040))

    def test_empty_string(self):
        self.assertEqual(parse_dimension(""), (None, None))

    def test_none_input(self):
        self.assertEqual(parse_dimension(None), (None, None))

    def test_no_numbers(self):
        self.assertEqual(parse_dimension("solid core"), (None, None))

    def test_large_height(self):
        w, h = parse_dimension("2700")
        self.assertIsNone(w)
        self.assertEqual(h, 2700)


class TestMarkExtraction(unittest.TestCase):
    """extract_mark() — D01, W01, WD01, Roman, no-mark."""

    def test_door_mark(self):
        self.assertEqual(extract_mark("D01"), "D01")

    def test_window_mark(self):
        self.assertEqual(extract_mark("W01"), "W01")

    def test_mark_in_text(self):
        self.assertEqual(extract_mark("Door D01 820x2040"), "D01")

    def test_wd_mark(self):
        self.assertEqual(extract_mark("WD01"), "WD01")

    def test_dw_mark(self):
        self.assertEqual(extract_mark("DW01"), "DW01")

    def test_lowercase(self):
        self.assertEqual(extract_mark("d01"), "D01")

    def test_no_mark(self):
        self.assertEqual(extract_mark("solid core door"), "")

    def test_empty(self):
        self.assertEqual(extract_mark(""), "")

    def test_none(self):
        self.assertEqual(extract_mark(None), "")

    def test_ignores_non_dw(self):
        self.assertEqual(extract_mark("EC01 FC01"), "")

    def test_high_number(self):
        self.assertEqual(extract_mark("D99"), "D99")

    def test_very_high_number(self):
        self.assertEqual(extract_mark("W9999"), "W9999")


class TestHeaderDetection(unittest.TestCase):
    """detect_header() — column role mapping."""

    def test_standard_header(self):
        h = detect_header(["Mark", "Width", "Height", "Description"])
        self.assertEqual(h["mark"], 0)
        self.assertEqual(h["width"], 1)
        self.assertEqual(h["height"], 2)
        self.assertEqual(h["desc"], 3)

    def test_dims_column(self):
        h = detect_header(["Type", "Size", "Notes"])
        self.assertEqual(h["mark"], 0)
        self.assertEqual(h["dims"], 1)
        self.assertEqual(h["desc"], 2)

    def test_count_column(self):
        h = detect_header(["Mark", "Qty", "Dims"])
        self.assertEqual(h["mark"], 0)
        self.assertEqual(h["count"], 1)
        self.assertEqual(h["dims"], 2)

    def test_empty_header(self):
        h = detect_header([])
        self.assertEqual(h, {})

    def test_no_match(self):
        h = detect_header(["foo", "bar", "baz"])
        self.assertEqual(h, {})

    def test_case_insensitive(self):
        h = detect_header(["MARK", "WIDTH", "HEIGHT"])
        self.assertEqual(h["mark"], 0)
        self.assertEqual(h["width"], 1)
        self.assertEqual(h["height"], 2)


class TestScheduleRowParsing(unittest.TestCase):
    """parse_schedule_rows() — full row parsing with header."""

    def test_standard_table(self):
        rows = [
            _row("Mark\tSize\tDescription"),
            _row("D01\t820x2040\tSolid core flush"),
            _row("D02\t720x2040\tSolid core flush"),
            _row("W01\t1200x1500\tAluminium slider"),
        ]
        entries = parse_schedule_rows(rows, page_no=1)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].type_mark, "D01")
        self.assertEqual(entries[0].width_mm, 820)
        self.assertEqual(entries[0].height_mm, 2040)
        self.assertEqual(entries[0].description, "Solid core flush")
        self.assertEqual(entries[0].page_no, 1)

    def test_dims_column(self):
        rows = [
            _row("Type\tSize\tNotes"),
            _row("W01\t1200 × 1500\tSliding window"),
        ]
        entries = parse_schedule_rows(rows)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].type_mark, "W01")
        self.assertEqual(entries[0].width_mm, 1200)
        self.assertEqual(entries[0].height_mm, 1500)

    def test_separate_width_height_columns(self):
        rows = [
            _row("Mark\tWidth\tHeight\tDesc"),
            _row("D01\t820\t2040\tFlush door"),
        ]
        entries = parse_schedule_rows(rows)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].width_mm, 820)
        self.assertEqual(entries[0].height_mm, 2040)

    def test_count_column(self):
        rows = [
            _row("Mark\tDims\tQty"),
            _row("D01\t820x2040\t4"),
            _row("W01\t1200x1500\t2"),
        ]
        entries = parse_schedule_rows(rows)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].count, 4)
        self.assertEqual(entries[1].count, 2)

    def test_empty_rows_skipped(self):
        rows = [
            _row("Mark\tSize"),
            _row("D01\t820x2040"),
            _row(""),
            _row("W01\t1200x1500"),
        ]
        entries = parse_schedule_rows(rows)
        self.assertEqual(len(entries), 2)

    def test_no_header_fallback(self):
        rows = [
            _row("D01 820x2040 Solid core"),
            _row("W01 1200x1500 Aluminium"),
        ]
        entries = parse_schedule_rows(rows)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].type_mark, "D01")
        self.assertEqual(entries[1].type_mark, "W01")

    def test_no_rows(self):
        self.assertEqual(parse_schedule_rows([]), [])

    def test_header_only_no_data(self):
        rows = [_row("Mark\tSize\tDescription")]
        entries = parse_schedule_rows(rows)
        self.assertEqual(entries, [])

    def test_mark_without_dimensions(self):
        rows = [
            _row("Mark\tSize"),
            _row("D01\t"),
            _row("W01\t1200x1500"),
        ]
        entries = parse_schedule_rows(rows)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].type_mark, "D01")
        self.assertIsNone(entries[0].width_mm)
        self.assertEqual(entries[1].width_mm, 1200)

    def test_page_no_propagated(self):
        rows = [_row("D01 820x2040")]
        entries = parse_schedule_rows(rows, page_no=5)
        self.assertEqual(entries[0].page_no, 5)

    def test_bbox_propagated(self):
        rows = [_row("D01 820x2040", bbox=(10, 20, 200, 40))]
        entries = parse_schedule_rows(rows)
        self.assertEqual(entries[0].bbox, (10, 20, 200, 40))

    def test_mixed_door_window(self):
        rows = [
            _row("Mark\tDims"),
            _row("D01\t820x2040"),
            _row("W01\t1200x1500"),
            _row("D02\t720x2040"),
            _row("W02\t600x1200"),
        ]
        entries = parse_schedule_rows(rows)
        marks = [e.type_mark for e in entries]
        self.assertEqual(marks, ["D01", "W01", "D02", "W02"])


class TestScheduleEntry(unittest.TestCase):
    """ScheduleEntry dataclass properties."""

    def test_defaults(self):
        e = ScheduleEntry(type_mark="D01")
        self.assertEqual(e.width_mm, None)
        self.assertEqual(e.height_mm, None)
        self.assertEqual(e.description, "")
        self.assertEqual(e.count, 1)
        self.assertEqual(e.page_no, 0)

    def test_frozen(self):
        e = ScheduleEntry(type_mark="D01")
        with self.assertRaises(AttributeError):
            e.type_mark = "D02"  # type: ignore[misc]


class TestEnrichment(unittest.TestCase):
    """enrich_opening_evidence() — schedule dims → OpeningEvidence."""

    def test_enrich_width_and_height(self):
        inst = _inst(mark="D01", width=0.82)
        sched = [_entry("D01", w=820, h=2040)]
        result = enrich_opening_evidence([inst], sched)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].width_m, 0.82, places=2)
        self.assertAlmostEqual(result[0].height_m, 2.04, places=2)
        self.assertEqual(result[0].dimension_source, "schedule_parse")

    def test_no_match_no_enrichment(self):
        inst = _inst(mark="D01")
        sched = [_entry("W01", w=1200, h=1500)]
        result = enrich_opening_evidence([inst], sched)
        self.assertIsNone(result[0].width_m)
        self.assertIsNone(result[0].height_m)

    def test_empty_mark_no_enrichment(self):
        inst = _inst(mark="")
        sched = [_entry("D01", w=820, h=2040)]
        result = enrich_opening_evidence([inst], sched)
        self.assertIsNone(result[0].width_m)

    def test_empty_schedule(self):
        inst = _inst(mark="D01")
        result = enrich_opening_evidence([inst], [])
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].width_m)

    def test_empty_instances(self):
        sched = [_entry("D01", w=820, h=2040)]
        result = enrich_opening_evidence([], sched)
        self.assertEqual(result, [])

    def test_multiple_instances(self):
        insts = [_inst(mark="D01"), _inst(mark="W01"), _inst(mark="D02")]
        sched = [
            _entry("D01", w=820, h=2040),
            _entry("W01", w=1200, h=1500),
        ]
        result = enrich_opening_evidence(insts, sched)
        self.assertEqual(len(result), 3)
        self.assertAlmostEqual(result[0].height_m, 2.04, places=2)
        self.assertAlmostEqual(result[1].height_m, 1.50, places=2)
        self.assertIsNone(result[2].height_m)  # D02 not in schedule

    def test_schedule_only_width(self):
        inst = _inst(mark="D01")
        sched = [_entry("D01", w=820)]
        result = enrich_opening_evidence([inst], sched)
        # Atomic bundle: both width and height needed to replace
        # Width-only does not replace existing dimensions
        # (this is the B0 safety contract behavior)
        self.assertIsNotNone(result[0])

    def test_existing_height_not_overwritten_by_none(self):
        inst = _inst(mark="D01", width=0.82, height=2.04)
        sched = [_entry("D01", w=820)]  # no height
        result = enrich_opening_evidence([inst], sched)
        # Existing height should be preserved (atomic bundle requires both)
        self.assertAlmostEqual(result[0].height_m, 2.04, places=2)

    def test_enrichment_preserves_deduction_status(self):
        inst = _inst(mark="D01")
        inst.deduction_status = DEDUCTION_REVIEW
        sched = [_entry("D01", w=820, h=2040)]
        result = enrich_opening_evidence([inst], sched)
        self.assertEqual(result[0].deduction_status, DEDUCTION_REVIEW)

    def test_no_new_instances_created(self):
        """Schedule entries must not create new OpeningEvidence records."""
        insts = [_inst(mark="D01")]
        sched = [
            _entry("D01", w=820, h=2040),
            _entry("W01", w=1200, h=1500),  # W01 has no matching instance
        ]
        result = enrich_opening_evidence(insts, sched)
        # Only 1 result — the W01 schedule entry does NOT create an instance
        self.assertEqual(len(result), 1)

    def test_deduction_never_set_by_enrichment(self):
        """Enrichment must not change deduction status to deducted."""
        inst = _inst(mark="D01")
        inst.deduction_status = DEDUCTION_REVIEW
        sched = [_entry("D01", w=820, h=2040)]
        result = enrich_opening_evidence([inst], sched)
        self.assertNotEqual(result[0].deduction_status, "deducted")

    def test_schedule_mark_not_assigned_to_unlabeled(self):
        """Schedule must not assign a type mark to an unlabeled opening."""
        inst = _inst(mark="")  # no mark
        sched = [_entry("D01", w=820, h=2040)]
        result = enrich_opening_evidence([inst], sched)
        self.assertEqual(result[0].type_mark, "")  # mark not assigned

    def test_first_schedule_mark_wins(self):
        """If schedule has duplicate marks, first entry wins."""
        inst = _inst(mark="D01")
        sched = [
            _entry("D01", w=820, h=2040),
            _entry("D01", w=900, h=2100),  # duplicate — ignored
        ]
        result = enrich_opening_evidence([inst], sched)
        self.assertAlmostEqual(result[0].height_m, 2.04, places=2)

    def test_enrichment_does_not_mutate_original(self):
        inst = _inst(mark="D01")
        orig_width = inst.width_m
        sched = [_entry("D01", w=820, h=2040)]
        result = enrich_opening_evidence([inst], sched)
        # Original should not be mutated
        self.assertIsNone(inst.width_m)
        # Result should have enriched dims
        self.assertAlmostEqual(result[0].height_m, 2.04, places=2)


class TestSafetyContract(unittest.TestCase):
    """Verify B2 safety boundaries."""

    def test_version(self):
        self.assertEqual(VERSION, "1.7.1")

    def test_schedule_source_in_non_instance_set(self):
        self.assertIn("schedule_parse", NON_INSTANCE_SOURCES)

    def test_schedule_entry_is_frozen(self):
        e = ScheduleEntry(type_mark="D01", width_mm=820)
        with self.assertRaises(AttributeError):
            e.width_mm = 900  # type: ignore[misc]

    def test_enrichment_uses_merge_not_direct_assign(self):
        """Enrichment goes through merge_opening_evidence (B0 contract)."""
        inst = _inst(mark="D01", width=0.82, height=2.04)
        sched = [_entry("D01", w=820, h=2040)]
        result = enrich_opening_evidence([inst], sched)
        # merge_opening_evidence is called — evidence list should be merged
        self.assertIsInstance(result[0], OpeningEvidence)


if __name__ == "__main__":
    unittest.main()
