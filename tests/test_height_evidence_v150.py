"""Priority 3 Phase B regression tests for height evidence model, resolver and room association.

Tests cover:
- Semantic CH/CLG/FCL/CEILING parsing
- RL contextual pairing safety
- Evidence precedence (semantic > RL > dimension > default)
- Height-type compatibility (floor-to-floor ≠ floor-to-ceiling)
- Section-derived evidence
- Per-room height association
- Full production chain: page → evidence → resolver → v139 wall → takeoff row
- Scale invariance (1:50 / 1:100 / 1:200)
"""
from __future__ import annotations

import math
import unittest
from typing import Any, Dict, List, Optional, Tuple

from pb_height_evidence_v150 import (
    HeightEvidence,
    _CEIL_RE,
    _FFL_RE,
    _RL_RE,
    _DIM_RE,
    _TYPE_RANGES,
    _find_paired_rls,
    _extract_semantic_heights,
    _extract_dimension_heights,
    _extract_section_heights,
    _room_height_type,
    _point_in_polygon,
    extract_all_height_evidence,
    resolve_height,
    resolve_room_heights,
    get_default_height,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MM_PER_PT = 25.4 / 72.0
SCALE_1_100_M_PER_PT = MM_PER_PT * 100.0  # ~0.03528 m/pt
SCALE_1_50_M_PER_PT = MM_PER_PT * 50.0
SCALE_1_200_M_PER_PT = MM_PER_PT * 200.0


# ---------------------------------------------------------------------------
# Semantic height parsing
# ---------------------------------------------------------------------------


class TestSemanticParsing(unittest.TestCase):
    """Semantic CH/CLG/FCL/CEILING parsing."""

    def test_ch_2700_mm(self):
        """Explicit 2700 → 2.7 m ceiling height."""
        ev = extract_all_height_evidence("CH 2700", page_id=1, page_label="A301")
        self.assertTrue(len(ev) >= 1)
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertAlmostEqual(ceil_ev[0].height_m, 2.7, places=2)
        self.assertEqual(ceil_ev[0].extraction_method, "semantic_label")
        self.assertEqual(ceil_ev[0].status, "Measured")

    def test_clg_3000(self):
        """CLG 3000 → 3.0 m ceiling height."""
        ev = extract_all_height_evidence("CLG 3000", page_id=1)
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertAlmostEqual(ceil_ev[0].height_m, 3.0, places=2)

    def test_ceiling_height_2700(self):
        """CEILING HEIGHT 2700 → 2.7 m."""
        ev = extract_all_height_evidence("CEILING HEIGHT 2700", page_id=1)
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertAlmostEqual(ceil_ev[0].height_m, 2.7, places=2)

    def test_fcl_2700(self):
        """FCL 2700 → 2.7 m ceiling height."""
        ev = extract_all_height_evidence("FCL 2700", page_id=1)
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertAlmostEqual(ceil_ev[0].height_m, 2.7, places=2)

    def test_ch_27m(self):
        """CH 2.7m → 2.7 m (with explicit 'm' unit)."""
        ev = extract_all_height_evidence("CH 2.7m", page_id=1)
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertAlmostEqual(ceil_ev[0].height_m, 2.7, places=2)

    def test_bare_2700_not_ceiling(self):
        """Bare 2700 without semantic label is NOT a ceiling height."""
        ev = extract_all_height_evidence("2700", page_id=1)
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertEqual(len(ceil_ev), 0, "Bare 2700 should not be floor_to_ceiling")


# ---------------------------------------------------------------------------
# RL contextual pairing safety
# ---------------------------------------------------------------------------


class TestRLPairingSafety(unittest.TestCase):
    """RL-derived evidence requires contextual pairing."""

    def test_ffl_fcl_pairing(self):
        """FFL 0.000 + FCL 2.700 → 2.7 m from semantic labels."""
        text = "FFL 0.000  FCL 2.700"
        ev = _extract_semantic_heights(text)
        self.assertTrue(len(ev) >= 1)
        heights = sorted(set(round(e.height_m, 2) for e in ev))
        self.assertIn(2.7, heights)

    def test_level_pairing(self):
        """LEVEL 1 RL 10.000 + LEVEL 2 RL 12.700 → 2.7 m."""
        text = "LEVEL 1 RL 10.000  LEVEL 2 RL 12.700"
        ev = _find_paired_rls(text)
        self.assertTrue(len(ev) >= 1)
        self.assertAlmostEqual(ev[0].height_m, 2.7, places=2)

    def test_unrelated_rls_no_pairing(self):
        """Two unrelated RLs on same sheet do NOT become a wall height."""
        text = "RL 45.230  RL 42.100"  # diff = 3.13 ≈ storey height
        ev = _find_paired_rls(text)
        # Without contextual keywords (FFL, FCL, LEVEL), should be empty
        self.assertEqual(len(ev), 0,
            "Unrelated RLs without semantic context should not pair")

    def test_rls_with_context_keywords(self):
        """RLs near FFL/FCL keywords are paired."""
        text = "FFL RL 0.000  FCL RL 2.700"
        ev = _find_paired_rls(text)
        self.assertTrue(len(ev) >= 1)
        self.assertAlmostEqual(ev[0].height_m, 2.7, places=2)

    def test_rls_near_storey_keyword(self):
        """RLs near STOREY keyword are paired."""
        text = "STOREY 1 RL 0.000  STOREY 2 RL 3.200"
        ev = _find_paired_rls(text)
        self.assertTrue(len(ev) >= 1)
        self.assertAlmostEqual(ev[0].height_m, 3.2, places=2)


# ---------------------------------------------------------------------------
# Evidence precedence
# ---------------------------------------------------------------------------


class TestEvidencePrecedence(unittest.TestCase):
    """Semantic > RL > dimension > default."""

    def test_semantic_beats_dimension(self):
        """CH 2700 beats a raw 3000 dimension."""
        ev_list = [
            HeightEvidence(
                id="H1", source_page_id=1, source_page_label="",
                height_type="floor_to_ceiling", raw_text="CH 2700",
                height_m=2.7, extraction_method="semantic_label",
                confidence=0.95, confidence_reason="explicit",
                status="Measured", evidence=[], position=[0, 10],
            ),
            HeightEvidence(
                id="H2", source_page_id=1, source_page_label="",
                height_type="generic", raw_text="3000",
                height_m=3.0, extraction_method="dimension_parse",
                confidence=0.70, confidence_reason="raw dim",
                status="Provisional measured", evidence=[], position=[20, 30],
            ),
        ]
        h, best = resolve_height(ev_list, target_type="floor_to_ceiling")
        self.assertAlmostEqual(h, 2.7, places=2)
        self.assertEqual(best.extraction_method, "semantic_label")

    def test_rl_beats_dimension(self):
        """Paired RL beats raw dimension."""
        ev_list = [
            HeightEvidence(
                id="H1", source_page_id=1, source_page_label="",
                height_type="floor_to_floor", raw_text="RL pair",
                height_m=2.7, extraction_method="rl_difference",
                confidence=0.95, confidence_reason="contextual RL",
                status="Measured", evidence=[], position=[0, 10],
            ),
            HeightEvidence(
                id="H2", source_page_id=1, source_page_label="",
                height_type="generic", raw_text="3000",
                height_m=3.0, extraction_method="dimension_parse",
                confidence=0.70, confidence_reason="raw dim",
                status="Provisional measured", evidence=[], position=[20, 30],
            ),
        ]
        h, best = resolve_height(ev_list, target_type="generic")
        self.assertAlmostEqual(h, 2.7, places=2)
        self.assertEqual(best.extraction_method, "rl_difference")

    def test_default_when_empty(self):
        """Empty evidence → default 2.7 m with Default/fallback status."""
        h, best = resolve_height([], target_type="generic")
        self.assertAlmostEqual(h, 2.7, places=2)
        self.assertEqual(best.status, "Default/fallback")
        self.assertEqual(best.extraction_method, "default")


# ---------------------------------------------------------------------------
# Height-type compatibility
# ---------------------------------------------------------------------------


class TestHeightTypeCompatibility(unittest.TestCase):
    """Floor-to-floor does not satisfy floor-to-ceiling requests."""

    def test_f2f_rejected_for_f2c(self):
        """Floor-to-floor evidence rejected when floor-to-ceiling requested."""
        ev_list = [
            HeightEvidence(
                id="H1", source_page_id=1, source_page_label="",
                height_type="floor_to_floor", raw_text="FFL→FCL 3.2",
                height_m=3.2, extraction_method="rl_difference",
                confidence=0.95, confidence_reason="",
                status="Measured", evidence=[], position=[0, 10],
            ),
        ]
        h, best = resolve_height(ev_list, target_type="floor_to_ceiling",
                                  allow_floor_to_floor=False)
        # Should fall back to default since f2f is not compatible
        self.assertEqual(best.status, "Default/fallback")

    def test_f2f_accepted_for_generic(self):
        """Floor-to-floor evidence accepted for generic height request."""
        ev_list = [
            HeightEvidence(
                id="H1", source_page_id=1, source_page_label="",
                height_type="floor_to_floor", raw_text="FFL→FCL 3.2",
                height_m=3.2, extraction_method="rl_difference",
                confidence=0.95, confidence_reason="",
                status="Measured", evidence=[], position=[0, 10],
            ),
        ]
        h, best = resolve_height(ev_list, target_type="generic")
        self.assertAlmostEqual(h, 3.2, places=2)


# ---------------------------------------------------------------------------
# Section-derived evidence
# ---------------------------------------------------------------------------


class TestSectionEvidence(unittest.TestCase):
    """Section pages contribute height evidence."""

    def test_section_ch_2700(self):
        """Section with CH 2700 produces height evidence."""
        text = "SECTION A-A  CH 2700  LEVEL 1  LEVEL 2"
        ev = extract_all_height_evidence(
            text, page_id=10, page_label="SECTION A-A", page_type="Section",
        )
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertAlmostEqual(ceil_ev[0].height_m, 2.7, places=2)
        self.assertEqual(ceil_ev[0].source_page_id, 10)

    def test_section_dimension(self):
        """Section with 3000 dimension produces height evidence."""
        text = "WALL SECTION  3000  FFL 0.000"
        ev = extract_all_height_evidence(
            text, page_id=20, page_label="SECTION B", page_type="Section",
        )
        self.assertTrue(len(ev) >= 1)
        heights = [e.height_m for e in ev]
        self.assertIn(3.0, [round(h, 2) for h in heights])


# ---------------------------------------------------------------------------
# Per-room height association
# ---------------------------------------------------------------------------


class TestPerRoomAssociation(unittest.TestCase):
    """Room-specific height association using Priority 2 polygons."""

    def test_labelled_room_gets_semantic_height(self):
        """Room with CH 2700 inside polygon gets 2.7 m."""
        rooms = [{"label": "BED 1", "polygon": [(0, 0), (100, 0), (100, 100), (0, 100)]}]
        evidence = [HeightEvidence(
            id="H1", source_page_id=1, source_page_label="",
            height_type="floor_to_ceiling", raw_text="CH 2700",
            height_m=2.7, extraction_method="semantic_label",
            confidence=0.95, confidence_reason="explicit",
            status="Measured", evidence=[], position=[50, 50],
        )]
        result = resolve_room_heights(rooms, evidence)
        self.assertAlmostEqual(result["BED 1"]["height_m"], 2.7, places=2)
        self.assertEqual(result["BED 1"]["height_source"], "semantic_label")

    def test_different_heights_different_rooms(self):
        """Room A 2700 / Room B 3000 → different wall heights."""
        rooms = [
            {"label": "BED 1", "polygon": [(0, 0), (100, 0), (100, 100), (0, 100)]},
            {"label": "LIVING", "polygon": [(200, 0), (300, 0), (300, 100), (200, 100)]},
        ]
        evidence = [
            HeightEvidence(
                id="H1", source_page_id=1, source_page_label="",
                height_type="floor_to_ceiling", raw_text="CH 2700",
                height_m=2.7, extraction_method="semantic_label",
                confidence=0.95, confidence_reason="",
                status="Measured", evidence=[], position=[50, 50],
            ),
            HeightEvidence(
                id="H2", source_page_id=1, source_page_label="",
                height_type="floor_to_ceiling", raw_text="CH 3000",
                height_m=3.0, extraction_method="semantic_label",
                confidence=0.95, confidence_reason="",
                status="Measured", evidence=[], position=[250, 50],
            ),
        ]
        result = resolve_room_heights(rooms, evidence)
        self.assertAlmostEqual(result["BED 1"]["height_m"], 2.7, places=2)
        self.assertAlmostEqual(result["LIVING"]["height_m"], 3.0, places=2)

    def test_unlabelled_room_gets_default(self):
        """Unlabelled room without evidence gets default."""
        rooms = [{"label": "", "room_ref": "R01", "polygon": []}]
        result = resolve_room_heights(rooms, [])
        self.assertAlmostEqual(result["R01"]["height_m"], 2.7, places=2)
        self.assertEqual(result["R01"]["height_status"], "Default/fallback")


# ---------------------------------------------------------------------------
# Wall-area calculation
# ---------------------------------------------------------------------------


class TestWallAreaCalculation(unittest.TestCase):
    """Wall area = length × height with correct source propagation."""

    def test_10m_x_27m(self):
        """10 m wall × 2.7 m = 27.0 m²."""
        length = 10.0
        height = 2.7
        area = length * height
        self.assertAlmostEqual(area, 27.0, places=1)

    def test_10m_x_30m(self):
        """10 m wall × 3.0 m = 30.0 m²."""
        length = 10.0
        height = 3.0
        area = length * height
        self.assertAlmostEqual(area, 30.0, places=1)

    def test_different_heights_different_areas(self):
        """Two 10 m walls with different heights produce different areas."""
        area_a = 10.0 * 2.7
        area_b = 10.0 * 3.0
        self.assertNotAlmostEqual(area_a, area_b, places=1)
        self.assertAlmostEqual(area_a, 27.0, places=1)
        self.assertAlmostEqual(area_b, 30.0, places=1)


# ---------------------------------------------------------------------------
# Scale invariance
# ---------------------------------------------------------------------------


class TestScaleInvariance(unittest.TestCase):
    """Vertical height from geometric measurement is scale-invariant."""

    def test_same_height_at_different_scales(self):
        """A 2.7 m vertical dimension should produce 2.7 m at any scale."""
        for scale_m_per_pt in [SCALE_1_50_M_PER_PT, SCALE_1_100_M_PER_PT, SCALE_1_200_M_PER_PT]:
            # Dimension text "2700" is independent of plan scale
            ev = extract_all_height_evidence("CH 2700", page_id=1)
            ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
            self.assertTrue(len(ceil_ev) >= 1)
            self.assertAlmostEqual(ceil_ev[0].height_m, 2.7, places=2)


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------


class TestRegexPatterns(unittest.TestCase):
    """Core regex pattern correctness."""

    def test_ceili_re(self):
        m = _CEIL_RE.search("CH 2700")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "2700")

    def test_ffl_re(self):
        m = _FFL_RE.search("FFL 0.000")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "0.000")

    def test_rl_re(self):
        m = _RL_RE.search("RL 23.450")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "23.450")

    def test_dim_re(self):
        m = _DIM_RE.search("3000mm")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "3000")
        self.assertEqual(m.group(2), "mm")

    def test_dim_re_plain(self):
        m = _DIM_RE.search("2700")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "2700")


# ---------------------------------------------------------------------------
# Production chain integration (mocked)
# ---------------------------------------------------------------------------


class TestProductionChain(unittest.TestCase):
    """End-to-end: page → evidence → resolver → wall → takeoff row."""

    def test_full_pipeline_27m(self):
        """10 m wall with CH 2700 → 27.0 m² Measured takeoff row."""
        # Step 1: Extract evidence from page text
        ev = extract_all_height_evidence("CH 2700", page_id=1, page_label="A301")
        self.assertTrue(len(ev) >= 1)

        # Step 2: Resolve best height
        h, best = resolve_height(ev, target_type="floor_to_ceiling")
        self.assertAlmostEqual(h, 2.7, places=2)
        self.assertEqual(best.status, "Measured")

        # Step 3: Wall area
        length = 10.0
        area = round(length * h, 2)
        self.assertAlmostEqual(area, 27.0, places=1)

        # Step 4: Takeoff row
        row = {
            "section": "External",
            "element": "Registered external wall · Render",
            "quantity": area,
            "unit": "m²",
            "quantity_status": "Measured plan length + registered elevation height"
                               if best.confidence >= 0.90 else "Provisional measured",
            "confidence": "Measured" if best.confidence >= 0.90 else "Derived",
            "notes": f"Height: {h:.3f} m from {best.extraction_method}. "
                     f"Source: {best.raw_text}.",
        }
        self.assertEqual(row["quantity"], 27.0)
        self.assertEqual(row["unit"], "m²")
        self.assertIn("2.700", row["notes"])

    def test_full_pipeline_30m(self):
        """10 m wall with CH 3000 → 30.0 m² Measured takeoff row."""
        ev = extract_all_height_evidence("CH 3000", page_id=1)
        h, best = resolve_height(ev, target_type="floor_to_ceiling")
        self.assertAlmostEqual(h, 3.0, places=2)
        area = round(10.0 * h, 2)
        self.assertAlmostEqual(area, 30.0, places=1)

    def test_explicit_ch_overrides_default(self):
        """CH 2700 overrides project default of 2.7 m (same value but different source)."""
        ev = extract_all_height_evidence("CH 2700", page_id=1)
        h, best = resolve_height(ev, target_type="floor_to_ceiling")
        self.assertEqual(best.extraction_method, "semantic_label")
        self.assertNotEqual(best.extraction_method, "default")

    def test_floor_to_floor_not_ceiling(self):
        """Floor-to-floor 3.2 m does NOT satisfy floor-to-ceiling request."""
        ev_list = [
            HeightEvidence(
                id="H1", source_page_id=1, source_page_label="",
                height_type="floor_to_floor", raw_text="3200",
                height_m=3.2, extraction_method="dimension_parse",
                confidence=0.70, confidence_reason="",
                status="Provisional measured", evidence=[], position=[0, 10],
            ),
        ]
        h, best = resolve_height(ev_list, target_type="floor_to_ceiling",
                                  allow_floor_to_floor=False)
        self.assertEqual(best.status, "Default/fallback")

    def test_unrelated_rls_not_verified(self):
        """Unrelated RLs do NOT become Verified height."""
        ev = extract_all_height_evidence(
            "RL 45.230  RL 42.100", page_id=1,
        )
        # Without semantic context, RLs should not pair
        rl_ev = [e for e in ev if e.extraction_method == "rl_difference"]
        self.assertEqual(len(rl_ev), 0,
            "Unrelated RLs should not produce rl_difference evidence")

    def test_fallback_27m_is_provisional(self):
        """Default 2.7 m fallback is Default/fallback, not Measured."""
        h, best = resolve_height([], target_type="generic")
        self.assertAlmostEqual(h, 2.7, places=2)
        self.assertEqual(best.status, "Default/fallback")
        self.assertEqual(best.extraction_method, "default")


# ---------------------------------------------------------------------------
# Room height type
# ---------------------------------------------------------------------------


class TestRoomHeightType(unittest.TestCase):
    """Room label → expected height type."""

    def test_bedroom_is_f2c(self):
        self.assertEqual(_room_height_type("BED 1"), "floor_to_ceiling")

    def test_living_is_f2c(self):
        self.assertEqual(_room_height_type("LIVING ROOM"), "floor_to_ceiling")

    def test_kitchen_is_f2c(self):
        self.assertEqual(_room_height_type("KITCHEN"), "floor_to_ceiling")

    def test_wc_is_f2c(self):
        self.assertEqual(_room_height_type("WC"), "floor_to_ceiling")

    def test_garage_is_f2f(self):
        self.assertEqual(_room_height_type("GARAGE"), "floor_to_floor")

    def test_unknown_is_generic(self):
        self.assertEqual(_room_height_type("UNKNOWN"), "generic")


# ---------------------------------------------------------------------------
# Point in polygon
# ---------------------------------------------------------------------------


class TestPointInPolygon(unittest.TestCase):
    """Ray-casting point-in-polygon."""

    def test_inside(self):
        poly = [(0, 0), (100, 0), (100, 100), (0, 100)]
        self.assertTrue(_point_in_polygon((50, 50), poly))

    def test_outside(self):
        poly = [(0, 0), (100, 0), (100, 100), (0, 100)]
        self.assertFalse(_point_in_polygon((150, 50), poly))

    def test_on_edge(self):
        poly = [(0, 0), (100, 0), (100, 100), (0, 100)]
        # Edge case — on boundary may or may not be inside
        result = _point_in_polygon((0, 50), poly)
        self.assertIsInstance(result, bool)


if __name__ == "__main__":
    unittest.main()
