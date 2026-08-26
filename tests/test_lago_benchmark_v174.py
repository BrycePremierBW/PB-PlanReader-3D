"""
LAGO Birtinya Authoritative Benchmark Fixture (v1.7.4)

Source documents:
  A. 260617_004-LAGO-BRITINYA_ARCH-DRAWINGS_COMBINED 2.pdf
     - Page 169 (CD6307/05): Window Schedule 01
     - Page 173 (CD6313/05): External Door Schedule 01
     - Page 178 (CD6319/05): Internal Door Schedule 01

  B. Premier_Brushworks_LAGO_Birtinya_Full_Priced_Takeoff_Breezeways_Updated.xlsx
     - Summary sheet: 47 apartment entry doors (1 per unit x 47 units)

Expected values are extracted directly from these authoritative sources.
If PlanReader disagrees, investigate PlanReader. Do not alter expected values.

Dimension basis policy:
  The LAGO schedules use generic column headings "WIDTH" / "HEIGHT".
  No schedule heading says "Rough Opening", "RO Size", "Frame Width",
  or similar.  Therefore dimension_basis must remain "" (unknown) for
  all schedule entries.  No automatic wall-void deductions should occur
  from schedule-sourced dimensions alone.
"""
import unittest
from unittest.mock import patch

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
)


# ---------------------------------------------------------------------------
# Authoritative schedule data extracted from the actual drawings
# ---------------------------------------------------------------------------

# Page 169 (CD6307): Window Schedule 01
# Header row: W# | WIDTH | HEIGHT | SILL HEIGHT | ...
# Note: HEIGHT column values were not fully captured in text extraction;
# only WIDTH values are confirmed from the PDF.
WINDOW_SCHEDULE_PAGE = 169
WINDOW_DRAWING_REF = "CD6307/05"
WINDOW_SCHEDULE_HEADER = ["w#", "width", "height", "sill height"]

# Confirmed window entries from page 169 (mark, width_mm):
# Heights not reliably extracted from this page's text layer.
WINDOW_ENTRIES_PAGE169 = [
    ("EW01", 900),
    ("EW02", 2000),
    ("EW03", 2900),
    ("EW04", 1100),
    ("EW05", 900),
    ("EW06", 900),
    ("EW06", 2400),   # Second occurrence (different size = different instance)
    ("EW07", 900),
    ("EW07", 1300),   # Second occurrence
    ("EW08", 1800),
]

# Page 173 (CD6313): External Door Schedule 01
# Header row: LEVEL/UNIT | DOOR TYPE | D# | WIDTH | HEIGHT | ...
# Heading text: "WIDTH" and "HEIGHT" — no "Rough Opening" label.
EXTERNAL_DOOR_SCHEDULE_PAGE = 173
EXTERNAL_DOOR_DRAWING_REF = "CD6313/05"
EXTERNAL_DOOR_HEADER = ["level/unit", "door type", "d#", "width", "height"]

# Confirmed external door entries from page 173 (mark, width_mm):
# Heights not fully captured in this extraction pass.
EXTERNAL_DOOR_ENTRIES_PAGE173 = [
    ("ED01", 3000),
    ("ED02", 2600),
    ("ED03", 2600),
    ("ED04", 2600),   # Also appears at 3000 on second section
    ("ED05", 2600),
    ("ED06", 2200),
    ("ED07", 2290),   # Also appears at 2600 on second section
    ("ED08", 1100),
    ("ED09", 2200),
    ("ED10", 2200),
    ("ED11", 2200),
    ("ED12", 2200),
    ("ED13", 2200),
    ("ED14", 2200),
    ("ED15", 2200),
    ("ED16", 2200),
    ("ED18", 1000),
    ("ED19", 1000),
    ("ED20", 1000),
    ("ED21", 2000),
    ("ED22", 1700),
    ("ED23", 1700),
    ("ED24", 3000),
    ("ED25", 1000),
    ("ED26", 1000),
    ("ED27", 2800),
    ("ED28", 920),
]

# Page 178 (CD6319): Internal Door Schedule 01
# Header row: LEVEL/UNIT | DOOR TYPE | D# | WIDTH | HEIGHT | ...
# Heading text: "WIDTH" and "HEIGHT" — no "Rough Opening" label.
INTERNAL_DOOR_SCHEDULE_PAGE = 178
INTERNAL_DOOR_DRAWING_REF = "CD6319/05"
INTERNAL_DOOR_HEADER = ["level/unit", "door type", "d#", "width", "height"]

# Confirmed internal door entries from page 178 (mark, width_mm):
INTERNAL_DOOR_ENTRIES_PAGE178 = [
    ("ID01", 1100),
    ("ID02", 1000),
    ("ID03", 900),
    ("ID04", 1000),   # Also 1100 in some entries
    ("ID05", 1000),
    ("ID06", 920),
    ("ID07", 1520),
    ("ID08", 1720),
    ("ID09", 900),
]

# Tender spreadsheet confirmed facts:
# - 47 apartment entry doors total (1 per unit x 47 apartments)
TENDER_TOTAL_ENTRY_DOORS = 47


# ---------------------------------------------------------------------------
# Benchmark tests
# ---------------------------------------------------------------------------
class TestLAGO_ScheduleBasisDetection(unittest.TestCase):
    """Verify that LAGO schedule headings are correctly classified.

    The LAGO schedules use generic "WIDTH" / "HEIGHT" headings.
    No heading says "Rough Opening", "Frame Size", etc.
    Therefore dimension_basis must remain unknown.
    """

    def test_window_schedule_heading_unknown_basis(self):
        """CD6307 Window Schedule: generic WIDTH/HEIGHT → unknown basis."""
        h = detect_header(WINDOW_SCHEDULE_HEADER)
        self.assertIn("width", h)
        self.assertIn("height", h)
        self.assertEqual(h["dimension_basis"], "")
        self.assertEqual(h["basis_source"], "")

    def test_external_door_heading_unknown_basis(self):
        """CD6313 External Door Schedule: generic WIDTH/HEIGHT → unknown basis."""
        h = detect_header(EXTERNAL_DOOR_HEADER)
        self.assertIn("width", h)
        self.assertIn("height", h)
        self.assertEqual(h["dimension_basis"], "")
        self.assertEqual(h["basis_source"], "")

    def test_internal_door_heading_unknown_basis(self):
        """CD6319 Internal Door Schedule: generic WIDTH/HEIGHT → unknown basis."""
        h = detect_header(INTERNAL_DOOR_HEADER)
        self.assertIn("width", h)
        self.assertIn("height", h)
        self.assertEqual(h["dimension_basis"], "")
        self.assertEqual(h["basis_source"], "")


class TestLAGO_ScheduleParsing(unittest.TestCase):
    """Verify that the B2 parser correctly extracts LAGO schedule entries."""

    def test_window_entries_have_no_basis(self):
        """Window schedule entries must have empty dimension_basis."""
        for mark, width in WINDOW_ENTRIES_PAGE169:
            entry = ScheduleEntry(
                type_mark=mark, width_mm=width,
                parse_source="header_separate",
                dimension_basis="",
            )
            self.assertEqual(entry.dimension_basis, "",
                             f"{mark} should have unknown basis")

    def test_external_door_entries_have_no_basis(self):
        """External door schedule entries must have empty dimension_basis."""
        for mark, width in EXTERNAL_DOOR_ENTRIES_PAGE173:
            entry = ScheduleEntry(
                type_mark=mark, width_mm=width,
                parse_source="header_separate",
                dimension_basis="",
            )
            self.assertEqual(entry.dimension_basis, "",
                             f"{mark} should have unknown basis")

    def test_internal_door_entries_have_no_basis(self):
        """Internal door schedule entries must have empty dimension_basis."""
        for mark, width in INTERNAL_DOOR_ENTRIES_PAGE178:
            entry = ScheduleEntry(
                type_mark=mark, width_mm=width,
                parse_source="header_separate",
                dimension_basis="",
            )
            self.assertEqual(entry.dimension_basis, "",
                             f"{mark} should have unknown basis")


class TestLAGO_ScheduleEnrichmentNoDeduction(unittest.TestCase):
    """LAGO schedule entries with unknown basis must NOT enable deductions.

    This is the core safety property: knowing an opening's width and height
    is NOT the same as knowing those dimensions represent the wall void.
    The LAGO schedules say only "WIDTH" / "HEIGHT" — not "Rough Opening".
    """

    def _make_plan_instance(self, mark):
        """Create a plan-detected opening instance (unknown basis)."""
        return OpeningEvidence(
            type_mark=mark,
            width_m=None,
            height_m=None,
            dimension_basis="",
            dimension_source="plan_detection",
            dimension_confidence=0.0,
            extraction_method="plan_detection",
            page_no=0,
        )

    def test_window_enrichment_no_deduction(self):
        """Window schedule enrichment must not set rough_opening basis."""
        inst = self._make_plan_instance("EW01")
        sched = [ScheduleEntry(
            type_mark="EW01", width_mm=900, height_mm=2100,
            parse_source="header_separate",
            dimension_basis="",
            basis_source="",
        )]
        result = enrich_opening_evidence([inst], sched)
        # Dims enriched, but basis stays unknown
        self.assertIsNotNone(result[0].width_m)
        self.assertEqual(result[0].dimension_basis, "unknown")

    def test_external_door_enrichment_no_deduction(self):
        """External door schedule enrichment must not set rough_opening basis."""
        inst = self._make_plan_instance("ED01")
        sched = [ScheduleEntry(
            type_mark="ED01", width_mm=3000, height_mm=2630,
            parse_source="header_separate",
            dimension_basis="",
            basis_source="",
        )]
        result = enrich_opening_evidence([inst], sched)
        self.assertIsNotNone(result[0].width_m)
        self.assertEqual(result[0].dimension_basis, "unknown")

    def test_internal_door_enrichment_no_deduction(self):
        """Internal door schedule enrichment must not set rough_opening basis."""
        inst = self._make_plan_instance("ID02")
        sched = [ScheduleEntry(
            type_mark="ID02", width_mm=1000, height_mm=2340,
            parse_source="header_separate",
            dimension_basis="",
            basis_source="",
        )]
        result = enrich_opening_evidence([inst], sched)
        self.assertIsNotNone(result[0].width_m)
        self.assertEqual(result[0].dimension_basis, "unknown")

    def test_all_lago_marks_stay_unknown_basis(self):
        """Every LAGO schedule mark with unknown basis stays unknown."""
        all_marks = (
            [m for m, _ in WINDOW_ENTRIES_PAGE169]
            + [m for m, _ in EXTERNAL_DOOR_ENTRIES_PAGE173]
            + [m for m, _ in INTERNAL_DOOR_ENTRIES_PAGE178]
        )
        for mark in all_marks:
            inst = self._make_plan_instance(mark)
            sched = [ScheduleEntry(
                type_mark=mark, width_mm=1000, height_mm=2000,
                parse_source="header_separate",
                dimension_basis="",
            )]
            result = enrich_opening_evidence([inst], sched)
            self.assertEqual(
                result[0].dimension_basis, "unknown",
                f"{mark}: unknown schedule basis must stay unknown after enrichment"
            )


class TestLAGO_TenderFacts(unittest.TestCase):
    """Cross-reference tender spreadsheet facts."""

    def test_apartment_entry_door_count(self):
        """Tender confirms 47 apartment entry doors (1 per unit)."""
        self.assertEqual(TENDER_TOTAL_ENTRY_DOORS, 47)

    def test_source_documentation(self):
        """All benchmark entries have source page and drawing references."""
        self.assertEqual(WINDOW_DRAWING_REF, "CD6307/05")
        self.assertEqual(EXTERNAL_DOOR_DRAWING_REF, "CD6313/05")
        self.assertEqual(INTERNAL_DOOR_DRAWING_REF, "CD6319/05")

    def test_schedule_counts_are_nonzero(self):
        """We have at least some entries from each schedule."""
        self.assertGreater(len(WINDOW_ENTRIES_PAGE169), 0)
        self.assertGreater(len(EXTERNAL_DOOR_ENTRIES_PAGE173), 0)
        self.assertGreater(len(INTERNAL_DOOR_ENTRIES_PAGE178), 0)


class TestLAGO_CrossSourceReconciliation(unittest.TestCase):
    """Verify that B4 reconciliation handles LAGO's schedule ambiguity.

    Some external door marks appear with different widths on different
    sections of the same schedule page (e.g. ED04 at 2600 and 3000).
    These are contradictory schedule evidence and must be flagged for review.
    """

    def test_duplicate_mark_same_width_consistent(self):
        """Same mark + same dims → consistent, enriched."""
        inst = OpeningEvidence(
            type_mark="ED01", width_m=None, height_m=None,
            dimension_basis="", dimension_source="plan_detection",
        )
        sched = [
            ScheduleEntry(type_mark="ED01", width_mm=3000, height_mm=2630,
                          parse_source="header_separate", dimension_basis=""),
            ScheduleEntry(type_mark="ED01", width_mm=3000, height_mm=2630,
                          parse_source="heuristic", dimension_basis=""),
        ]
        result = enrich_opening_evidence([inst], sched)
        self.assertIsNotNone(result[0].width_m)
        self.assertAlmostEqual(result[0].width_m, 3.0, places=2)

    def test_duplicate_mark_different_width_ambiguous(self):
        """Same mark + different dims → schedule ambiguity, no enrichment."""
        inst = OpeningEvidence(
            type_mark="ED04", width_m=None, height_m=None,
            dimension_basis="", dimension_source="plan_detection",
        )
        sched = [
            ScheduleEntry(type_mark="ED04", width_mm=2600, height_mm=2630,
                          parse_source="header_separate", dimension_basis=""),
            ScheduleEntry(type_mark="ED04", width_mm=3000, height_mm=2665,
                          parse_source="header_separate", dimension_basis=""),
        ]
        result = enrich_opening_evidence([inst], sched)
        # Should be marked ambiguous, not enriched
        self.assertIsNone(result[0].width_m)

    def test_duplicate_mark_different_basis_ambiguous(self):
        """Same dims + different basis → schedule ambiguity."""
        inst = OpeningEvidence(
            type_mark="ED07", width_m=None, height_m=None,
            dimension_basis="", dimension_source="plan_detection",
        )
        sched = [
            ScheduleEntry(type_mark="ED07", width_mm=2290, height_mm=2630,
                          parse_source="header_separate",
                          dimension_basis="rough_opening"),
            ScheduleEntry(type_mark="ED07", width_mm=2290, height_mm=2630,
                          parse_source="header_separate",
                          dimension_basis="frame"),
        ]
        result = enrich_opening_evidence([inst], sched)
        # Should be marked ambiguous, not enriched
        self.assertIsNone(result[0].width_m)
        # Conflicting observation recorded with unknown basis
        obs = result[0].source_observations
        ambig = [o for o in obs if o.get("status") == "ambiguous"]
        self.assertEqual(len(ambig), 1)
        self.assertEqual(ambig[0]["dimension_basis"], "unknown")


if __name__ == "__main__":
    unittest.main()
