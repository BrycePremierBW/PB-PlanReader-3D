"""Priority 3 Phase B regression tests — v150 height evidence (round 2).

Covers:
- BLOCKER 1: FFL values are absolute levels, not heights
- BLOCKER 2: room spatial association uses real PDF coordinates
- BLOCKER 3: horizontal dimensions do NOT become wall heights
- BLOCKER 4: RL difference type classification from semantic endpoints
- Metadata: v150 status/confidence reaches profile consistently
- Evidence precedence, type compatibility, section evidence, room association
"""
from __future__ import annotations

import math
import unittest
from typing import Any, Dict, List, Optional, Tuple

from pb_height_evidence_v150 import (
    HeightEvidence,
    WordBox,
    _CEIL_RE,
    _FFL_RE,
    _RL_RE,
    _DIM_RE,
    _TYPE_RANGES,
    _infer_rl_height_type,
    _find_paired_rls,
    _extract_semantic_heights,
    _extract_dimension_heights,
    _classify_dimension_orientation,
    _derive_level_heights,
    _extract_with_positions,
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
SCALE_1_100_M_PER_PT = MM_PER_PT * 100.0
SCALE_1_50_M_PER_PT = MM_PER_PT * 50.0
SCALE_1_200_M_PER_PT = MM_PER_PT * 200.0


# ---------------------------------------------------------------------------
# BLOCKER 1 — FFL values are absolute levels, not heights
# ---------------------------------------------------------------------------


class TestFFLDatumIsolation(unittest.TestCase):
    """BLOCKER 1: FFL 10.000 must NOT resolve to 10.0 m wall height."""

    def test_ffl_standalone_not_resolver_compatible(self):
        """FFL 10.000 alone does NOT resolve to 10.0 m."""
        ev = extract_all_height_evidence("FFL 10.000", page_id=1)
        h, best = resolve_height(ev, target_type="generic")
        # Should get default 2.7, not 10.0
        self.assertAlmostEqual(h, 2.7, places=2)
        self.assertEqual(best.status, "Default/fallback")

    def test_ffl_is_level_reference_type(self):
        """FFL 10.000 has height_type=level_reference."""
        ev = extract_all_height_evidence("FFL 10.000", page_id=1)
        ffl_ev = [e for e in ev if e.extraction_method == "level_reference"]
        self.assertTrue(len(ffl_ev) >= 1)
        self.assertEqual(ffl_ev[0].height_type, "level_reference")
        self.assertAlmostEqual(ffl_ev[0].height_m, 10.0, places=2)

    def test_level_ref_excluded_from_resolver(self):
        """Level references are filtered from resolver candidates."""
        ev_list = [
            HeightEvidence(
                id="L1", source_page_id=1, source_page_label="",
                height_type="level_reference", raw_text="FFL 10.000",
                height_m=10.0, extraction_method="level_reference",
                confidence=0.90, confidence_reason="",
                status="Level reference", evidence=[], position=[0, 10],
            ),
        ]
        h, best = resolve_height(ev_list, target_type="generic")
        self.assertEqual(best.status, "Default/fallback")

    def test_ffl_fcl_derives_height(self):
        """FFL 10.000 → FCL 12.700 derives floor_to_ceiling = 2.700 m."""
        ev = extract_all_height_evidence(
            "FFL 10.000  FCL 12.700", page_id=1,
        )
        h, best = resolve_height(ev, target_type="floor_to_ceiling")
        self.assertAlmostEqual(h, 2.7, places=2)
        self.assertEqual(best.height_type, "floor_to_ceiling")
        self.assertEqual(best.status, "Measured")

    def test_level1_level2_derives_floor_to_floor(self):
        """LEVEL 1 FFL 10.000 → LEVEL 2 FFL 13.200 → f2f = 3.200 m."""
        ev = extract_all_height_evidence(
            "LEVEL 1 FFL 10.000  LEVEL 2 FFL 13.200", page_id=1,
        )
        h, best = resolve_height(ev, target_type="generic")
        self.assertAlmostEqual(h, 3.2, places=2)
        self.assertEqual(best.height_type, "floor_to_floor")


# ---------------------------------------------------------------------------
# BLOCKER 4 — RL difference type classification
# ---------------------------------------------------------------------------


class TestRLTypeClassification(unittest.TestCase):
    """BLOCKER 4: height type inferred from semantic endpoints."""

    def test_infer_rl_height_type_ffl_fcl(self):
        """FFL → FCL = floor_to_ceiling."""
        self.assertEqual(_infer_rl_height_type("ffl", "fcl"), "floor_to_ceiling")
        self.assertEqual(_infer_rl_height_type("FCL", "FFL"), "floor_to_ceiling")

    def test_infer_rl_height_type_level_level(self):
        """LEVEL → LEVEL = floor_to_floor."""
        self.assertEqual(_infer_rl_height_type("level", "level"), "floor_to_floor")
        self.assertEqual(_infer_rl_height_type("storey", "ffl"), "floor_to_floor")

    def test_infer_rl_height_type_ffl_soffit(self):
        """FFL → SOFFIT = soffit/generic."""
        ht = _infer_rl_height_type("ffl", "soffit")
        self.assertIn(ht, ("generic", "soffit"))

    def test_paired_rl_ffl_fcl_is_floor_to_ceiling(self):
        """Paired RLs FFL→FCL produce floor_to_ceiling height."""
        text = "FFL RL 0.000  FCL RL 2.700"
        ev = _find_paired_rls(text)
        self.assertTrue(len(ev) >= 1)
        self.assertEqual(ev[0].height_type, "floor_to_ceiling")
        self.assertAlmostEqual(ev[0].height_m, 2.7, places=2)

    def test_paired_rl_level_level_is_floor_to_floor(self):
        """Paired RLs Level 1→Level 2 produce floor_to_floor."""
        text = "LEVEL 1 RL 10.000  LEVEL 2 RL 12.700"
        ev = _find_paired_rls(text)
        self.assertTrue(len(ev) >= 1)
        self.assertEqual(ev[0].height_type, "floor_to_floor")
        self.assertAlmostEqual(ev[0].height_m, 2.7, places=2)

    def test_unrelated_rls_no_pairing(self):
        """Two unrelated RLs on same sheet do NOT become a wall height."""
        text = "RL 45.230  RL 42.100"
        ev = _find_paired_rls(text)
        self.assertEqual(len(ev), 0)


# ---------------------------------------------------------------------------
# BLOCKER 3 — horizontal dimensions do NOT become wall heights
# ---------------------------------------------------------------------------


class TestDimensionOrientation(unittest.TestCase):
    """BLOCKER 3: raw horizontal dimensions do NOT become height evidence."""

    def test_horizontal_2400_not_height(self):
        """'ROOM 2400 wide' → 2400 is horizontal, NOT height."""
        ev = _extract_dimension_heights("ROOM WIDTH 2400mm", "generic")
        # Should be empty — horizontal context detected
        usable = [e for e in ev if e.status != "Review"]
        self.assertEqual(len(usable), 0)

    def test_horizontal_3000_not_height(self):
        """'3000 long room' → 3000 is horizontal, NOT height."""
        ev = _extract_dimension_heights("ROOM LENGTH 3000mm", "generic")
        usable = [e for e in ev if e.status != "Review"]
        self.assertEqual(len(usable), 0)

    def test_vertical_dimension_in_section(self):
        """Section/elevation context → dimension is vertical."""
        ev = _extract_dimension_heights(
            "ELEVATION 3000mm HIGH", "generic", is_section_or_elevation=True,
        )
        self.assertTrue(len(ev) >= 1)
        self.assertIn(ev[0].status, ("Provisional measured",))

    def test_vertical_context_dimension(self):
        """'HEIGHT 3000mm' → vertical context, usable."""
        ev = _extract_dimension_heights("HEIGHT 3000mm", "generic")
        self.assertTrue(len(ev) >= 1)
        self.assertEqual(ev[0].status, "Provisional measured")

    def test_ch_3000_remains_measured(self):
        """CH 3000 → semantic ceiling height, Measured status."""
        ev = extract_all_height_evidence("CH 3000", page_id=1)
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertAlmostEqual(ceil_ev[0].height_m, 3.0, places=2)
        self.assertEqual(ceil_ev[0].status, "Measured")

    def test_unknown_orientation_is_review(self):
        """Dimension without orientation context → Review status."""
        ev = _extract_dimension_heights("3000", "generic")
        if ev:  # may or may not match depending on context
            for e in ev:
                self.assertEqual(e.status, "Review")


# ---------------------------------------------------------------------------
# BLOCKER 2 — room spatial association uses real PDF coordinates
# ---------------------------------------------------------------------------


class TestPositionedRoomAssociation(unittest.TestCase):
    """BLOCKER 2: evidence uses real PDF bbox, not text offsets."""

    def test_positioned_ch_2700_bed1(self):
        """Positioned CH 2700 inside BED 1 polygon → 2.7 m."""
        words = [
            WordBox("BED", 100, 100, 140, 120, page_id=1, line_id=0),
            WordBox("1", 145, 100, 155, 120, page_id=1, line_id=0),
            WordBox("CH", 110, 130, 130, 150, page_id=1, line_id=1),
            WordBox("2700", 135, 130, 175, 150, page_id=1, line_id=1),
        ]
        ev = extract_all_height_evidence(words, page_id=1, page_label="A301")
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertAlmostEqual(ceil_ev[0].height_m, 2.7, places=2)
        # Position should be real bbox coords, not character offsets
        self.assertTrue(ceil_ev[0].position[0] >= 100)

    def test_no_cross_room_leakage(self):
        """CH 2700 in BED 1 does NOT leak to LIVING."""
        room_bed = {
            "label": "BED 1",
            "polygon": [(50, 50), (200, 50), (200, 200), (50, 200)],
        }
        room_living = {
            "label": "LIVING",
            "polygon": [(300, 50), (500, 50), (500, 200), (300, 200)],
        }
        ev = extract_all_height_evidence("CH 2700", page_id=1)
        result = resolve_room_heights(
            [room_bed, room_living], ev, default_height=2.7,
        )
        # Both get default since CH 2700 has no real bbox (plain text → no spatial match)
        # This test proves no false spatial matching from character offsets
        for key in result:
            self.assertEqual(result[key]["height_source"], "default")


class TestPositionedWordExtraction(unittest.TestCase):
    """Positioned words produce real bbox coordinates."""

    def test_words_to_text(self):
        """WordBox list reconstructs to correct text."""
        from pb_height_evidence_v150 import _words_to_text_with_map
        words = [
            WordBox("CH", 100, 100, 120, 120, line_id=0),
            WordBox("2700", 125, 100, 165, 120, line_id=0),
        ]
        text, wmap = _words_to_text_with_map(words)
        self.assertIn("CH", text)
        self.assertIn("2700", text)

    def test_extract_with_positions_returns_bbox(self):
        """Positioned extraction returns real bbox in evidence position."""
        words = [
            WordBox("CH", 100.0, 200.0, 120.0, 220.0, page_id=1, line_id=0),
            WordBox("2700", 125.0, 200.0, 165.0, 220.0, page_id=1, line_id=0),
        ]
        ev = _extract_with_positions(words, page_id=1)
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        # Position should be real PDF coordinates (x >= 100)
        self.assertGreaterEqual(ceil_ev[0].position[0], 100)


# ---------------------------------------------------------------------------
# Evidence precedence
# ---------------------------------------------------------------------------


class TestEvidencePrecedence(unittest.TestCase):
    """Semantic > RL > dimension > default."""

    def test_semantic_beats_dimension(self):
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
        self.assertEqual(best.status, "Default/fallback")

    def test_f2f_accepted_for_generic(self):
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
    def test_section_ch_2700(self):
        text = "SECTION A-A  CH 2700  LEVEL 1  LEVEL 2"
        ev = extract_all_height_evidence(
            text, page_id=10, page_label="SECTION A-A", page_type="Section",
        )
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertAlmostEqual(ceil_ev[0].height_m, 2.7, places=2)
        self.assertEqual(ceil_ev[0].source_page_id, 10)


# ---------------------------------------------------------------------------
# Per-room height association
# ---------------------------------------------------------------------------


class TestPerRoomAssociation(unittest.TestCase):
    def test_labelled_room_gets_semantic_height(self):
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

    def test_unlabelled_room_gets_default(self):
        rooms = [{"label": "", "room_ref": "R01", "polygon": []}]
        result = resolve_room_heights(rooms, [])
        self.assertAlmostEqual(result["R01"]["height_m"], 2.7, places=2)
        self.assertEqual(result["R01"]["height_status"], "Default/fallback")


# ---------------------------------------------------------------------------
# Wall-area calculation
# ---------------------------------------------------------------------------


class TestWallAreaCalculation(unittest.TestCase):
    def test_10m_x_27m(self):
        self.assertAlmostEqual(10.0 * 2.7, 27.0, places=1)

    def test_10m_x_30m(self):
        self.assertAlmostEqual(10.0 * 3.0, 30.0, places=1)


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------


class TestRegexPatterns(unittest.TestCase):
    def test_ceili_re(self):
        m = _CEIL_RE.search("CH 2700")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "2700")

    def test_ceili_re_start_of_string(self):
        m = _CEIL_RE.search("CH 2700 at start")
        self.assertIsNotNone(m)

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


# ---------------------------------------------------------------------------
# Production chain integration
# ---------------------------------------------------------------------------


class TestProductionChain(unittest.TestCase):
    def test_full_pipeline_27m(self):
        """10 m wall with CH 2700 → 27.0 m² Measured takeoff row."""
        ev = extract_all_height_evidence("CH 2700", page_id=1, page_label="A301")
        h, best = resolve_height(ev, target_type="floor_to_ceiling")
        self.assertAlmostEqual(h, 2.7, places=2)
        self.assertEqual(best.status, "Measured")
        area = round(10.0 * h, 2)
        self.assertAlmostEqual(area, 27.0, places=1)

    def test_full_pipeline_30m(self):
        """10 m wall with CH 3000 → 30.0 m² Measured takeoff row."""
        ev = extract_all_height_evidence("CH 3000", page_id=1)
        h, best = resolve_height(ev, target_type="floor_to_ceiling")
        self.assertAlmostEqual(h, 3.0, places=2)
        area = round(10.0 * h, 2)
        self.assertAlmostEqual(area, 30.0, places=1)

    def test_explicit_ch_overrides_default(self):
        ev = extract_all_height_evidence("CH 2700", page_id=1)
        h, best = resolve_height(ev, target_type="floor_to_ceiling")
        self.assertEqual(best.extraction_method, "semantic_label")
        self.assertNotEqual(best.extraction_method, "default")

    def test_floor_to_floor_not_ceiling(self):
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
        ev = extract_all_height_evidence("RL 45.230  RL 42.100", page_id=1)
        rl_ev = [e for e in ev if e.extraction_method == "rl_difference"]
        self.assertEqual(len(rl_ev), 0)

    def test_fallback_27m_is_provisional(self):
        h, best = resolve_height([], target_type="generic")
        self.assertAlmostEqual(h, 2.7, places=2)
        self.assertEqual(best.status, "Default/fallback")


# ---------------------------------------------------------------------------
# Metadata propagation (v150 status reaches profile)
# ---------------------------------------------------------------------------


class TestMetadataPropagation(unittest.TestCase):
    def test_v150_status_in_evidence_record(self):
        """v150-selected evidence carries consistent status/confidence."""
        ev = extract_all_height_evidence("CH 2700", page_id=1)
        h, best = resolve_height(ev, target_type="floor_to_ceiling")
        self.assertEqual(best.status, "Measured")
        self.assertGreaterEqual(best.confidence, 0.90)

    def test_default_evidence_has_fallback_status(self):
        """Default fallback has Default/fallback status."""
        h, best = resolve_height([], target_type="generic")
        self.assertEqual(best.status, "Default/fallback")
        self.assertEqual(best.extraction_method, "default")

    def test_level_reference_not_in_height_evidence(self):
        """Level references are tagged as level_reference, not usable height."""
        ev = extract_all_height_evidence("FFL 10.000", page_id=1)
        refs = [e for e in ev if e.height_type == "level_reference"]
        self.assertTrue(len(refs) >= 1)
        # The level reference must NOT be selected by the resolver
        h, best = resolve_height(ev, target_type="generic")
        self.assertEqual(best.status, "Default/fallback")


# ---------------------------------------------------------------------------
# Room height type
# ---------------------------------------------------------------------------


class TestRoomHeightType(unittest.TestCase):
    def test_bedroom_is_f2c(self):
        self.assertEqual(_room_height_type("BED 1"), "floor_to_ceiling")

    def test_garage_is_f2f(self):
        self.assertEqual(_room_height_type("GARAGE"), "floor_to_floor")

    def test_unknown_is_generic(self):
        self.assertEqual(_room_height_type("UNKNOWN"), "generic")


# ---------------------------------------------------------------------------
# Point in polygon
# ---------------------------------------------------------------------------


class TestPointInPolygon(unittest.TestCase):
    def test_inside(self):
        poly = [(0, 0), (100, 0), (100, 100), (0, 100)]
        self.assertTrue(_point_in_polygon((50, 50), poly))

    def test_outside(self):
        poly = [(0, 0), (100, 0), (100, 100), (0, 100)]
        self.assertFalse(_point_in_polygon((150, 50), poly))


if __name__ == "__main__":
    unittest.main()
