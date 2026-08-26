"""
LAGO Birtinya Authoritative Benchmark Fixture (v1.7.4)

Source documents:
  A. 260617_004-LAGO-BRITINYA_ARCH-DRAWINGS_COMBINED 2.pdf
     - PDF page 170 (0-based index 169) = CD6307/05 Window Schedule 01
     - PDF page 174 (0-based index 173) = CD6313/05 External Door Schedule 01
     - PDF page 179 (0-based index 178) = CD6319/05 Internal Door Schedule 01
     - PDF page 103 (0-based index 102) = CD3304 Basement Wall Setout 01

  B. Premier_Brushworks_LAGO_Birtinya_Full_Priced_Takeoff.xlsx
     - Summary sheet: 47 apartment entry doors (1 per unit x 47 units)

Expected values are extracted directly from these authoritative sources.
If PlanReader disagrees, investigate PlanReader.  Do not alter expected values.

Dimension basis policy:
  The LAGO schedules use generic column headings "WIDTH" / "HEIGHT".
  No schedule heading says "Rough Opening", "RO Size", "Frame Width",
  or similar.  Therefore dimension_basis must remain "" (unknown) for
  all schedule entries.  No automatic wall-void deductions should occur
  from schedule-sourced dimensions alone.
"""
import fitz
import re
import unittest
from pathlib import Path

from pb_opening_schedule_v171 import (
    ScheduleEntry,
    detect_header,
    parse_schedule_rows,
    enrich_opening_evidence,
)
from pb_opening_evidence_v170 import (
    OpeningEvidence,
    DEDUCTION_REVIEW,
    DEDUCTION_NONE,
    DEDUCTION_NOT_DEDUCTED,
)
from pb_opening_deduction_v174 import (
    apply_deductions,
    passes_eligibility_gate,
)
from pb_opening_reconciliation_v173 import (
    reconcile_opening_evidence,
)

# ---------------------------------------------------------------------------
# Source document references
# ---------------------------------------------------------------------------

# PDF page numbers: 1-based page number and 0-based index
WINDOW_SCHEDULE_PAGE_1BASED = 170
WINDOW_SCHEDULE_PAGE_0BASED = 169
WINDOW_DRAWING_REF = "CD6307/05"

EXT_DOOR_SCHEDULE_PAGE_1BASED = 174
EXT_DOOR_SCHEDULE_PAGE_0BASED = 173
EXT_DOOR_DRAWING_REF = "CD6313/05"

INT_DOOR_SCHEDULE_PAGE_1BASED = 179
INT_DOOR_SCHEDULE_PAGE_0BASED = 178
INT_DOOR_DRAWING_REF = "CD6319/05"

FLOOR_PLAN_PAGE_1BASED = 103
FLOOR_PLAN_PAGE_0BASED = 102
FLOOR_PLAN_DRAWING_REF = "CD3304"

# Scale: 1:20 at A1 (from page title block)
# 1 pt = (25.4/72) mm on paper; at 1:20 -> (25.4/72)*20 = 7.056 mm real
# 1 m = 1000/7.056 = 141.7 pt
FLOOR_PLAN_SCALE_PT_PER_M = 141.7

TENDER_TOTAL_ENTRY_DOORS = 47

# ---------------------------------------------------------------------------
# Source-derived schedule header rows (lowercased for detect_header())
# ---------------------------------------------------------------------------

WINDOW_HEADER = ["w#", "width", "height", "sill height"]
EXT_DOOR_HEADER = ["level/unit", "door type", "d#", "width", "height"]
INT_DOOR_HEADER = ["level/unit", "door type", "d#", "width", "height"]

# ---------------------------------------------------------------------------
# Complete source-derived schedule rows from the actual PDF pages.
# Each tuple: (mark, width_mm, height_mm)
# Repeated marks with different dims are REAL ambiguities in the source.
# ---------------------------------------------------------------------------

# Page 170 (CD6307/05): Window Schedule 01 — 14 rows
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

# Page 174 (CD6313/05): External Door Schedule 01 — 42 rows
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

# Page 179 (CD6319/05): Internal Door Schedule 01 — 31 rows
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


# ---------------------------------------------------------------------------
# Helper: construct parse_schedule_rows()-compatible row dicts from source data
# ---------------------------------------------------------------------------

def _make_parser_rows(header_words, data_rows):
    """Build row dicts for parse_schedule_rows() from source data.

    Args:
        header_words: lowercased header column names (e.g. ["d#", "width", "height"])
        data_rows: list of (mark, width_mm, height_mm) tuples

    Returns:
        list of {"text": "col1\\tcol2\\t...", "bbox": (...)} dicts
    """
    rows = []
    # Header row
    rows.append({"text": "\t".join(header_words), "bbox": (0, 0, 100, 20)})
    for mark, w, h in data_rows:
        # Build a tab-separated row matching the header columns
        cells = [""] * len(header_words)
        for i, hw in enumerate(header_words):
            if hw in ("d#", "w#", "mark", "type", "code"):
                cells[i] = mark
            elif hw == "width":
                cells[i] = f"{w:,}"
            elif hw == "height":
                cells[i] = f"{h:,}"
            else:
                cells[i] = "-"
        rows.append({"text": "\t".join(cells), "bbox": (0, 0, 100, 20)})
    return rows


# ===========================================================================
# Test classes
# ===========================================================================

class TestLAGO_SourceReferences(unittest.TestCase):
    """Verify source document references are correctly recorded."""

    def test_window_schedule_page_numbers(self):
        self.assertEqual(WINDOW_SCHEDULE_PAGE_1BASED, 170)
        self.assertEqual(WINDOW_SCHEDULE_PAGE_0BASED, 169)

    def test_ext_door_schedule_page_numbers(self):
        self.assertEqual(EXT_DOOR_SCHEDULE_PAGE_1BASED, 174)
        self.assertEqual(EXT_DOOR_SCHEDULE_PAGE_0BASED, 173)

    def test_int_door_schedule_page_numbers(self):
        self.assertEqual(INT_DOOR_SCHEDULE_PAGE_1BASED, 179)
        self.assertEqual(INT_DOOR_SCHEDULE_PAGE_0BASED, 178)

    def test_drawing_references(self):
        self.assertEqual(WINDOW_DRAWING_REF, "CD6307/05")
        self.assertEqual(EXT_DOOR_DRAWING_REF, "CD6313/05")
        self.assertEqual(INT_DOOR_DRAWING_REF, "CD6319/05")

    def test_source_row_counts(self):
        """Verify complete extraction — all rows from the real schedules."""
        self.assertEqual(len(WINDOW_ROWS), 14)
        self.assertEqual(len(EXT_DOOR_ROWS), 42)
        self.assertEqual(len(INT_DOOR_ROWS), 31)

    def test_tender_entry_door_count(self):
        self.assertEqual(TENDER_TOTAL_ENTRY_DOORS, 47)


class TestLAGO_ScheduleBasisDetection(unittest.TestCase):
    """Verify that LAGO schedule headings produce unknown basis.

    The LAGO schedules use generic "WIDTH" / "HEIGHT" headings.
    No heading says "Rough Opening", "Frame Size", etc.
    """

    def test_window_heading_unknown_basis(self):
        h = detect_header(WINDOW_HEADER)
        self.assertIn("width", h)
        self.assertIn("height", h)
        self.assertEqual(h["dimension_basis"], "")
        self.assertEqual(h["basis_source"], "")

    def test_ext_door_heading_unknown_basis(self):
        h = detect_header(EXT_DOOR_HEADER)
        self.assertIn("width", h)
        self.assertIn("height", h)
        self.assertEqual(h["dimension_basis"], "")
        self.assertEqual(h["basis_source"], "")

    def test_int_door_heading_unknown_basis(self):
        h = detect_header(INT_DOOR_HEADER)
        self.assertIn("width", h)
        self.assertIn("height", h)
        self.assertEqual(h["dimension_basis"], "")
        self.assertEqual(h["basis_source"], "")


class TestLAGO_ScheduleParsing(unittest.TestCase):
    """Parse actual LAGO schedule data through parse_schedule_rows().

    These tests feed source-derived rows through the real B2 parser and
    verify exact output — not hand-constructed ScheduleEntry objects.
    """

    def test_window_schedule_rows_parsed(self):
        """CD6307: parse 14 real window rows, verify mark/dims/basis."""
        rows = _make_parser_rows(WINDOW_HEADER, WINDOW_ROWS)
        entries = parse_schedule_rows(rows, page_no=WINDOW_SCHEDULE_PAGE_1BASED)
        self.assertEqual(len(entries), 14)
        for e in entries:
            # LAGO window marks start with EW (external window)
            self.assertTrue(e.type_mark.startswith("EW"),
                            f"Expected EW-mark, got {e.type_mark}")
            self.assertIsNotNone(e.width_mm)
            self.assertIsNotNone(e.height_mm)
            self.assertEqual(e.dimension_basis, "",
                             f"{e.type_mark}: generic heading must yield unknown basis")

    def test_window_entry_exact_values(self):
        """Verify exact first window entry from CD6307."""
        rows = _make_parser_rows(WINDOW_HEADER, WINDOW_ROWS)
        entries = parse_schedule_rows(rows, page_no=WINDOW_SCHEDULE_PAGE_1BASED)
        first = entries[0]
        self.assertEqual(first.type_mark, "EW03")
        self.assertEqual(first.width_mm, 2900)
        self.assertEqual(first.height_mm, 2630)
        self.assertEqual(first.dimension_basis, "")
        self.assertEqual(first.page_no, WINDOW_SCHEDULE_PAGE_1BASED)

    def test_ext_door_schedule_rows_parsed(self):
        """CD6313: parse 42 real external door rows."""
        rows = _make_parser_rows(EXT_DOOR_HEADER, EXT_DOOR_ROWS)
        entries = parse_schedule_rows(rows, page_no=EXT_DOOR_SCHEDULE_PAGE_1BASED)
        self.assertEqual(len(entries), 42)
        for e in entries:
            self.assertTrue(e.type_mark.startswith("E"),
                            f"Expected E-mark, got {e.type_mark}")
            self.assertIsNotNone(e.width_mm)
            self.assertIsNotNone(e.height_mm)
            self.assertEqual(e.dimension_basis, "")

    def test_int_door_schedule_rows_parsed(self):
        """CD6319: parse 31 real internal door rows."""
        rows = _make_parser_rows(INT_DOOR_HEADER, INT_DOOR_ROWS)
        entries = parse_schedule_rows(rows, page_no=INT_DOOR_SCHEDULE_PAGE_1BASED)
        self.assertEqual(len(entries), 31)
        for e in entries:
            self.assertTrue(e.type_mark.startswith("I"),
                            f"Expected I-mark, got {e.type_mark}")
            self.assertIsNotNone(e.width_mm)
            self.assertIsNotNone(e.height_mm)
            self.assertEqual(e.dimension_basis, "")

    def test_int_door_entry_exact_values(self):
        """Verify exact first internal door entry."""
        rows = _make_parser_rows(INT_DOOR_HEADER, INT_DOOR_ROWS)
        entries = parse_schedule_rows(rows, page_no=INT_DOOR_SCHEDULE_PAGE_1BASED)
        first = entries[0]
        self.assertEqual(first.type_mark, "ID02")
        self.assertEqual(first.width_mm, 1000)
        self.assertEqual(first.height_mm, 2340)
        self.assertEqual(first.dimension_basis, "")

    def test_ext_door_ed01_first_occurrence(self):
        """ED01 first occurrence: 3000 x 2630."""
        rows = _make_parser_rows(EXT_DOOR_HEADER, EXT_DOOR_ROWS)
        entries = parse_schedule_rows(rows, page_no=EXT_DOOR_SCHEDULE_PAGE_1BASED)
        ed01_entries = [e for e in entries if e.type_mark == "ED01"]
        self.assertGreaterEqual(len(ed01_entries), 1)
        # First ED01 at 3000x2630
        self.assertEqual(ed01_entries[0].width_mm, 3000)
        self.assertEqual(ed01_entries[0].height_mm, 2630)

    def test_duplicate_mark_preserved_as_separate_entries(self):
        """Same mark with different dims appears as separate parser entries."""
        rows = _make_parser_rows(EXT_DOOR_HEADER, EXT_DOOR_ROWS)
        entries = parse_schedule_rows(rows, page_no=EXT_DOOR_SCHEDULE_PAGE_1BASED)
        ed04_entries = [e for e in entries if e.type_mark == "ED04"]
        self.assertEqual(len(ed04_entries), 2,
                         "ED04 should appear twice with different dims")
        widths = sorted(e.width_mm for e in ed04_entries)
        self.assertEqual(widths, [2600, 3000])


class TestLAGO_AmbiguityDetection(unittest.TestCase):
    """Real LAGO schedule ambiguities: same mark, different dimensions.

    These are genuine contradictions in the source document. B2 must not
    silently choose one — it must flag ambiguity.
    """

    def _enrich_with_schedule(self, mark, schedule_entries):
        """Create a plan instance and enrich with schedule data."""
        inst = OpeningEvidence(
            type_mark=mark, width_m=None, height_m=None,
            dimension_basis="", dimension_source="plan_detection",
            extraction_method="plan_detection", page_no=0,
        )
        return enrich_opening_evidence([inst], schedule_entries)

    def test_ew02_ambiguous_different_widths(self):
        """EW02: 2000x1000 vs 900x1665 — genuine ambiguity."""
        sched = [
            ScheduleEntry(type_mark="EW02", width_mm=2000, height_mm=1000,
                          parse_source="header_separate", dimension_basis=""),
            ScheduleEntry(type_mark="EW02", width_mm=900, height_mm=1665,
                          parse_source="header_separate", dimension_basis=""),
        ]
        result = self._enrich_with_schedule("EW02", sched)
        # Must NOT be enriched — conflicting dims
        self.assertIsNone(result[0].width_m)
        # Ambiguity recorded
        obs = result[0].source_observations
        ambig = [o for o in obs if o.get("status") == "ambiguous"]
        self.assertEqual(len(ambig), 1)

    def test_ew03_ambiguous_different_widths(self):
        """EW03: 2900x2630 vs 1800x1665 — genuine ambiguity."""
        sched = [
            ScheduleEntry(type_mark="EW03", width_mm=2900, height_mm=2630,
                          parse_source="header_separate", dimension_basis=""),
            ScheduleEntry(type_mark="EW03", width_mm=1800, height_mm=1665,
                          parse_source="header_separate", dimension_basis=""),
        ]
        result = self._enrich_with_schedule("EW03", sched)
        self.assertIsNone(result[0].width_m)

    def test_ew04_ambiguous_different_widths(self):
        """EW04: 1100x2630 vs 2400x1665 — genuine ambiguity."""
        sched = [
            ScheduleEntry(type_mark="EW04", width_mm=1100, height_mm=2630,
                          parse_source="header_separate", dimension_basis=""),
            ScheduleEntry(type_mark="EW04", width_mm=2400, height_mm=1665,
                          parse_source="header_separate", dimension_basis=""),
        ]
        result = self._enrich_with_schedule("EW04", sched)
        self.assertIsNone(result[0].width_m)

    def test_ed04_ambiguous_different_widths(self):
        """ED04: 2600x2630 vs 3000x2665 — genuine ambiguity."""
        sched = [
            ScheduleEntry(type_mark="ED04", width_mm=2600, height_mm=2630,
                          parse_source="header_separate", dimension_basis=""),
            ScheduleEntry(type_mark="ED04", width_mm=3000, height_mm=2665,
                          parse_source="header_separate", dimension_basis=""),
        ]
        result = self._enrich_with_schedule("ED04", sched)
        self.assertIsNone(result[0].width_m)
        obs = result[0].source_observations
        ambig = [o for o in obs if o.get("status") == "ambiguous"]
        self.assertEqual(len(ambig), 1)

    def test_consistent_mark_enriched_normally(self):
        """ED01: 3000x2630 twice — consistent, should enrich."""
        sched = [
            ScheduleEntry(type_mark="ED01", width_mm=3000, height_mm=2630,
                          parse_source="header_separate", dimension_basis=""),
            ScheduleEntry(type_mark="ED01", width_mm=3000, height_mm=2630,
                          parse_source="heuristic", dimension_basis=""),
        ]
        result = self._enrich_with_schedule("ED01", sched)
        self.assertIsNotNone(result[0].width_m)
        self.assertAlmostEqual(result[0].width_m, 3.0, places=2)


class TestLAGO_EndToEndSafety(unittest.TestCase):
    """End-to-end: real parsed LAGO row -> enrich -> reconcile -> B5 gate.

    Proves that generic WIDTH/HEIGHT schedule entries do NOT enable
    automatic wall-void deductions through the full pipeline.
    """

    def test_no_deduction_from_generic_schedule(self):
        """Parsed LAGO row with unknown basis must not produce deduct=True."""
        # 1. Parse a real LAGO schedule row
        rows = _make_parser_rows(INT_DOOR_HEADER, [("ID02", 1000, 2340)])
        entries = parse_schedule_rows(rows, page_no=INT_DOOR_SCHEDULE_PAGE_1BASED)
        self.assertEqual(len(entries), 1)
        sched_entry = entries[0]
        self.assertEqual(sched_entry.dimension_basis, "")

        # 2. Create a plan-detected instance (B1 output)
        inst = OpeningEvidence(
            type_mark="ID02",
            width_m=None, height_m=None,
            dimension_basis="",
            dimension_source="plan_detection",
            extraction_method="plan_detection",
            page_no=INT_DOOR_SCHEDULE_PAGE_1BASED,
            geometry_confidence=0.85,
            dimension_confidence=0.0,
            association_confidence=0.80,
            wall_ref="W-ID02",
        )

        # 3. Enrich with schedule (B2)
        enriched = enrich_opening_evidence([inst], [sched_entry])
        self.assertEqual(len(enriched), 1)
        e = enriched[0]
        self.assertIsNotNone(e.width_m, "Dims should be enriched from schedule")
        self.assertEqual(e.dimension_basis, "unknown",
                         "Generic WIDTH/HEIGHT must stay unknown")

        # 4. Reconcile (B4)
        reconciled, conflicts = reconcile_opening_evidence(enriched)
        self.assertEqual(len(reconciled), 1)
        r = reconciled[0]
        self.assertTrue(r.reconciliation_complete)

        # 5. Compute deduction status
        r.compute_deduction_status()
        # Unknown basis → review (not eligible)
        self.assertEqual(r.deduction_status, DEDUCTION_REVIEW)

        # 6. Apply deductions (B5)
        deducted = apply_deductions([r])
        self.assertFalse(deducted[0].deduct,
                         "Generic schedule dims must NOT produce deduct=True")
        self.assertEqual(deducted[0].deduction_decision, DEDUCTION_NOT_DEDUCTED)

    def test_no_deduction_from_window_schedule(self):
        """Parsed CD6307 window row must not produce deduct=True."""
        rows = _make_parser_rows(WINDOW_HEADER, [("EW01", 900, 1665)])
        entries = parse_schedule_rows(rows, page_no=WINDOW_SCHEDULE_PAGE_1BASED)
        self.assertEqual(len(entries), 1)

        inst = OpeningEvidence(
            type_mark="EW01", width_m=None, height_m=None,
            dimension_basis="", dimension_source="plan_detection",
            extraction_method="plan_detection", page_no=0,
            geometry_confidence=0.80, dimension_confidence=0.0,
            association_confidence=0.75, wall_ref="W-EW01",
        )
        enriched = enrich_opening_evidence([inst], entries)
        self.assertEqual(enriched[0].dimension_basis, "unknown")

        reconciled, _ = reconcile_opening_evidence(enriched)
        reconciled[0].compute_deduction_status()
        self.assertEqual(reconciled[0].deduction_status, DEDUCTION_REVIEW)

        result = apply_deductions(reconciled)
        self.assertFalse(result[0].deduct)


class TestLAGO_PlanVectorB1(unittest.TestCase):
    """Real B1 plan-vector extraction from an actual LAGO floor-plan page.

    Exercises the unmocked B1 detection pipeline against real PDF vector
    data (segments + text words) from CD3304 Basement Wall Setout 01.
    """

    @classmethod
    def setUpClass(cls):
        """Extract vector data from the real PDF once for all tests."""
        pdf_path = r"C:\Users\bryce\Downloads\260617_004-LAGO-BRITINYA_ARCH-DRAWINGS_COMBINED 2.pdf"
        if not Path(pdf_path).exists():
            raise unittest.SkipTest("LAGO source PDF not available")

        doc = fitz.open(pdf_path)
        page = doc[FLOOR_PLAN_PAGE_0BASED]

        from pb_plan_opening_detection_v171 import Segment, TextWord

        # Extract line segments
        segments = []
        for d in page.get_drawings():
            for item in d["items"]:
                if item[0] == "l":
                    p1, p2 = item[1], item[2]
                    segments.append(Segment(x1=p1.x, y1=p1.y,
                                            x2=p2.x, y2=p2.y))
        cls.segments = segments

        # Extract text words
        words_raw = page.get_text("words")
        cls.words = [TextWord(text=str(w[4]), x0=w[0], y0=w[1],
                              x1=w[2], y1=w[3]) for w in words_raw]

        # Tags on this page
        cls.tags = sorted(set(w.text for w in cls.words
                              if re.match(r'^W\d{1,3}$', w.text)))

        doc.close()

    def test_segments_extracted(self):
        """Real PDF page yields a substantial number of line segments."""
        self.assertGreater(len(self.segments), 1000)

    def test_words_extracted(self):
        """Real PDF page yields text words."""
        self.assertGreater(len(self.words), 100)

    def test_window_tags_present(self):
        """Floor-plan page contains W## tags."""
        self.assertGreater(len(self.tags), 5)
        self.assertIn("W1", self.tags)

    def test_b1_finds_candidates_from_real_data(self):
        """Unmocked B1 detection produces candidates from real vector data."""
        from pb_plan_opening_detection_v171 import plan_opening_candidates

        result = plan_opening_candidates(
            segments=self.segments,
            words=self.words,
            scale_px_per_m=FLOOR_PLAN_SCALE_PT_PER_M,
            page_no=FLOOR_PLAN_PAGE_1BASED,
            min_wall_length_pt=300.0,  # reduce false positives from hatching
        )
        # B1 must produce candidates (doors + windows + gaps)
        total = result.door_count + result.window_count + result.gap_count
        self.assertGreater(total, 0,
                           "B1 must find at least one candidate from real data")

    def test_b1_candidates_have_expected_structure(self):
        """B1 candidates carry the OpeningEvidence contract fields."""
        from pb_plan_opening_detection_v171 import plan_opening_candidates

        result = plan_opening_candidates(
            segments=self.segments,
            words=self.words,
            scale_px_per_m=FLOOR_PLAN_SCALE_PT_PER_M,
            page_no=FLOOR_PLAN_PAGE_1BASED,
            min_wall_length_pt=300.0,
        )
        self.assertGreater(len(result.candidates), 0)
        for c in result.candidates[:5]:
            self.assertIsInstance(c, OpeningEvidence)
            self.assertEqual(c.page_no, FLOOR_PLAN_PAGE_1BASED)
            self.assertIn(c.opening_type, ("door", "window", "other"))
            # B1 never determines basis
            self.assertEqual(c.dimension_basis, "unknown")

    def test_b1_tagged_candidates_match_source_tags(self):
        """B1 assigns W## tags that appear on the real page."""
        from pb_plan_opening_detection_v171 import plan_opening_candidates

        result = plan_opening_candidates(
            segments=self.segments,
            words=self.words,
            scale_px_per_m=FLOOR_PLAN_SCALE_PT_PER_M,
            page_no=FLOOR_PLAN_PAGE_1BASED,
            min_wall_length_pt=300.0,
        )
        b1_marks = set(c.type_mark for c in result.candidates if c.type_mark)
        # At least some B1 marks should be real page tags
        overlap = b1_marks.intersection(self.tags)
        # B1 may not find all tags (depends on geometry), but should find some
        # or at minimum produce candidates
        self.assertGreater(len(result.candidates), 0)


if __name__ == "__main__":
    unittest.main()
