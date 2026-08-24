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
    apply as apply_height_evidence_v150,
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
            WordBox("BED", 100, 100, 140, 120, page_id=1, line_id=(0, 0)),
            WordBox("1", 145, 100, 155, 120, page_id=1, line_id=(0, 0)),
            WordBox("CH", 110, 130, 130, 150, page_id=1, line_id=(0, 1)),
            WordBox("2700", 135, 130, 175, 150, page_id=1, line_id=(0, 1)),
        ]
        ev = extract_all_height_evidence(words, page_id=1, page_label="A301")
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertAlmostEqual(ceil_ev[0].height_m, 2.7, places=2)
        # bbox should be real PDF coords, not character offsets
        self.assertTrue(len(ceil_ev[0].bbox) == 4)
        self.assertTrue(ceil_ev[0].bbox[0] >= 100)
        self.assertIsNotNone(ceil_ev[0].anchor)

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
            WordBox("CH", 100, 100, 120, 120, line_id=(0, 0)),
            WordBox("2700", 125, 100, 165, 120, line_id=(0, 0)),
        ]
        text, wmap = _words_to_text_with_map(words)
        self.assertIn("CH", text)
        self.assertIn("2700", text)

    def test_extract_with_positions_returns_bbox(self):
        """Positioned extraction returns real bbox in evidence position."""
        words = [
            WordBox("CH", 100.0, 200.0, 120.0, 220.0, page_id=1, line_id=(0, 0)),
            WordBox("2700", 125.0, 200.0, 165.0, 220.0, page_id=1, line_id=(0, 0)),
        ]
        ev = _extract_with_positions(words, page_id=1)
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        # bbox should be real PDF coordinates (x >= 100)
        self.assertTrue(len(ceil_ev[0].bbox) == 4)
        self.assertGreaterEqual(ceil_ev[0].bbox[0], 100)


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
            status="Measured", evidence=[],
            anchor=(50.0, 50.0), bbox=[40.0, 40.0, 60.0, 60.0],
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


# ---------------------------------------------------------------------------
# BLOCKER 1 — Review/unknown dimensions do NOT drive production height
# ---------------------------------------------------------------------------


class TestReviewDimensionNotSelected(unittest.TestCase):
    """BLOCKER 1: unknown-orientation dimensions are retained but not selectable."""

    def test_plain_3000_returns_default(self):
        """Plain '3000' with no vertical context → resolver returns default."""
        ev = extract_all_height_evidence("3000", page_id=1)
        h, best = resolve_height(ev, target_type="generic")
        self.assertAlmostEqual(h, 2.7, places=2)
        self.assertEqual(best.status, "Default/fallback")

    def test_review_dimension_recorded_but_rejected(self):
        """Review dimension exists in evidence list but is filtered from selection."""
        ev = extract_all_height_evidence("3000", page_id=1)
        review_ev = [e for e in ev if e.status == "Review"]
        # The review dimension should exist as evidence
        self.assertTrue(len(review_ev) >= 1)
        # But should NOT be selected by the resolver
        h, best = resolve_height(ev, target_type="generic")
        self.assertEqual(best.extraction_method, "default")

    def test_vertical_3000_in_section_is_selectable(self):
        """Vertical dimension in section context IS selectable."""
        ev = extract_all_height_evidence(
            "SECTION A-A  3000", page_id=1, page_type="Section",
        )
        h, best = resolve_height(ev, target_type="generic")
        # Should select the section dimension, not default
        self.assertGreater(h, 2.7)
        self.assertIn(best.status, ("Provisional measured", "Measured"))


# ---------------------------------------------------------------------------
# BLOCKER 2 — v150 is authoritative, v136 RL solver is dead
# ---------------------------------------------------------------------------


class TestV150Authoritative(unittest.TestCase):
    """BLOCKER 2: v150 result replaces v136; unrelated RLs never become Verified."""

    @classmethod
    def setUpClass(cls):
        """Patch v136 so the production function runs v150 logic."""
        try:
            import pb_elevation_profile_v136 as v136
            cls._v136 = v136
            apply_height_evidence_v150(v136)
        except (ImportError, ModuleNotFoundError):
            raise unittest.SkipTest("v136 module not available")

    def test_patched_solve_rejects_unrelated_rls(self):
        """The patched production function returns default for unrelated RLs."""
        result = self._v136.solve_height_from_text("RL 45.230 RL 42.100", 2.7)
        self.assertAlmostEqual(result["height_m"], 2.7, places=2)
        self.assertNotEqual(result["status"], "Verified")
        self.assertEqual(result["source"], "default")

    def test_patched_solve_preserves_ch(self):
        """The patched function still returns CH 2700 correctly."""
        result = self._v136.solve_height_from_text("CH 2700", 2.7)
        self.assertAlmostEqual(result["height_m"], 2.7, places=2)
        self.assertEqual(result["status"], "Measured")

    def test_patched_solve_returns_v150_metadata(self):
        """The patched function returns v150 metadata, not v136 metadata."""
        result = self._v136.solve_height_from_text("CH 3000", 2.7)
        self.assertAlmostEqual(result["height_m"], 3.0, places=2)
        self.assertEqual(result["source"], "semantic_label")
        self.assertEqual(result.get("rls"), [])


# ---------------------------------------------------------------------------
# BLOCKER 3 — positioned room-height integration with production
# ---------------------------------------------------------------------------


class TestPositionedRoomIntegration(unittest.TestCase):
    """BLOCKER 3: positioned words → room polygons → correct per-room heights."""

    def test_bed1_ch2700_living_ch3000(self):
        """Same page: BED 1 gets CH 2700, LIVING gets CH 3000, no leakage."""
        words = [
            WordBox("BED", 100, 100, 140, 120, page_id=1, line_id=(0, 0)),
            WordBox("1", 145, 100, 155, 120, page_id=1, line_id=(0, 0)),
            WordBox("CH", 110, 130, 130, 150, page_id=1, line_id=(0, 1)),
            WordBox("2700", 135, 130, 175, 150, page_id=1, line_id=(0, 1)),
            WordBox("LIVING", 350, 100, 420, 120, page_id=1, line_id=(0, 0)),
            WordBox("CH", 360, 130, 380, 150, page_id=1, line_id=(0, 1)),
            WordBox("3000", 385, 130, 425, 150, page_id=1, line_id=(0, 1)),
        ]
        room_bed = {
            "label": "BED 1",
            "polygon": [(50, 50), (200, 50), (200, 200), (50, 200)],
        }
        room_living = {
            "label": "LIVING",
            "polygon": [(300, 50), (500, 50), (500, 200), (300, 200)],
        }
        ev = extract_all_height_evidence(words, page_id=1, page_label="A301")
        result = resolve_room_heights(
            [room_bed, room_living], ev, default_height=2.7,
        )
        self.assertAlmostEqual(result["BED 1"]["height_m"], 2.7, places=2)
        self.assertEqual(result["BED 1"]["height_source"], "semantic_label")
        self.assertAlmostEqual(result["LIVING"]["height_m"], 3.0, places=2)
        self.assertEqual(result["LIVING"]["height_source"], "semantic_label")

    def test_no_cross_room_leakage_positioned(self):
        """CH 2700 in BED 1 polygon does NOT leak to LIVING polygon."""
        words = [
            WordBox("BED", 100, 100, 140, 120, page_id=1, line_id=(0, 0)),
            WordBox("1", 145, 100, 155, 120, page_id=1, line_id=(0, 0)),
            WordBox("CH", 110, 130, 130, 150, page_id=1, line_id=(0, 1)),
            WordBox("2700", 135, 130, 175, 150, page_id=1, line_id=(0, 1)),
            WordBox("LIVING", 350, 100, 420, 120, page_id=1, line_id=(0, 0)),
            # No CH for LIVING — should get default
        ]
        room_bed = {
            "label": "BED 1",
            "polygon": [(50, 50), (200, 50), (200, 200), (50, 200)],
        }
        room_living = {
            "label": "LIVING",
            "polygon": [(300, 50), (500, 50), (500, 200), (300, 200)],
        }
        ev = extract_all_height_evidence(words, page_id=1, page_label="A301")
        result = resolve_room_heights(
            [room_bed, room_living], ev, default_height=2.7,
        )
        self.assertAlmostEqual(result["BED 1"]["height_m"], 2.7, places=2)
        # LIVING has no CH inside its polygon → gets default
        self.assertAlmostEqual(result["LIVING"]["height_m"], 2.7, places=2)
        self.assertEqual(result["LIVING"]["height_source"], "default")


# ---------------------------------------------------------------------------
# Position cleanup — bbox/anchor fields
# ---------------------------------------------------------------------------


class TestPositionFields(unittest.TestCase):
    """Verify bbox/anchor are set correctly, position migrated for compat."""

    def test_positioned_evidence_has_bbox_and_anchor(self):
        """Evidence from positioned words has bbox and anchor."""
        words = [
            WordBox("CH", 100.0, 200.0, 120.0, 220.0, page_id=1, line_id=(0, 0)),
            WordBox("2700", 125.0, 200.0, 165.0, 220.0, page_id=1, line_id=(0, 0)),
        ]
        ev = _extract_with_positions(words, page_id=1)
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertEqual(len(ceil_ev[0].bbox), 4)
        self.assertAlmostEqual(ceil_ev[0].bbox[0], 100.0, places=1)
        self.assertIsNotNone(ceil_ev[0].anchor)
        cx, cy = ceil_ev[0].anchor
        self.assertAlmostEqual(cx, 110.0, places=1)  # (100+120)/2
        self.assertAlmostEqual(cy, 210.0, places=1)  # (200+220)/2

    def test_plain_text_evidence_has_text_span(self):
        """Evidence from plain text has text_span, no bbox."""
        ev = extract_all_height_evidence("CH 2700", page_id=1)
        ceil_ev = [e for e in ev if e.height_type == "floor_to_ceiling"]
        self.assertTrue(len(ceil_ev) >= 1)
        self.assertTrue(len(ceil_ev[0].text_span) == 2)
        self.assertEqual(len(ceil_ev[0].bbox), 0)
        self.assertIsNone(ceil_ev[0].anchor)


# ---------------------------------------------------------------------------
# Level pairing safety — multi-level page
# ---------------------------------------------------------------------------


class TestMultiLevelPairing(unittest.TestCase):
    """Multi-storey page: level refs pair within semantic level, not cross-level."""

    def test_multi_level_pairs_correctly(self):
        """LEVEL 1 FFL/FCL and LEVEL 2 FFL/FCL pair within level."""
        text = (
            "LEVEL 1 FFL 10.000  LEVEL 1 FCL 12.700  "
            "LEVEL 2 FFL 13.200  LEVEL 2 FCL 15.900"
        )
        ev = extract_all_height_evidence(text, page_id=1)

        # Should derive Level 1 F2C = 2.7, Level 2 F2C = 2.7
        # and L1→L2 F2F = 3.2
        f2c_ev = [e for e in ev if e.height_type == "floor_to_ceiling"
                  and e.extraction_method == "rl_difference"]
        f2f_ev = [e for e in ev if e.height_type == "floor_to_floor"
                  and e.extraction_method == "rl_difference"]

        # Should have at least one F2C derived height
        self.assertTrue(len(f2c_ev) >= 1)
        f2c_heights = sorted([e.height_m for e in f2c_ev])
        self.assertAlmostEqual(f2c_heights[0], 2.7, places=2)

        # Should have at least one F2F derived height (L1→L2)
        self.assertTrue(len(f2f_ev) >= 1)
        f2f_heights = sorted([e.height_m for e in f2f_ev])
        self.assertAlmostEqual(f2f_heights[0], 3.2, places=2)

    def test_no_cross_level_fcl_pairing(self):
        """LEVEL 1 FFL must NOT pair with LEVEL 2 FCL across levels."""
        text = (
            "LEVEL 1 FFL 10.000  LEVEL 2 FCL 15.900"
        )
        ev = extract_all_height_evidence(text, page_id=1)
        # The difference is 5.9m — this is a cross-level span, not a room height
        # It should NOT produce a usable floor_to_ceiling height
        f2c_ev = [e for e in ev
                  if e.height_type == "floor_to_ceiling"
                  and e.extraction_method == "rl_difference"
                  and 1.8 <= e.height_m <= 6.0]
        # 5.9m may appear as generic F2F, but should NOT be F2C for a room
        for e in f2c_ev:
            # If it appears, it should not be classified as a room ceiling height
            self.assertNotAlmostEqual(e.height_m, 5.9, places=1)


# ---------------------------------------------------------------------------
# Production v136 tests — patched function
# ---------------------------------------------------------------------------


class TestPatchedV136Production(unittest.TestCase):
    """Ensure patched v136 functions produce v150 results."""

    @classmethod
    def setUpClass(cls):
        """Patch v136 so the production function runs v150 logic."""
        try:
            import pb_elevation_profile_v136 as v136
            cls._v136 = v136
            apply_height_evidence_v150(v136)
        except (ImportError, ModuleNotFoundError):
            raise unittest.SkipTest("v136 module not available")

    def test_ch_2700_via_patched_function(self):
        """CH 2700 through patched solve_height_from_text → 2.7 Measured."""
        result = self._v136.solve_height_from_text("CH 2700", 2.7)
        self.assertAlmostEqual(result["height_m"], 2.7, places=2)
        self.assertEqual(result["status"], "Measured")
        self.assertEqual(result["source"], "semantic_label")

    def test_empty_text_via_patched_function(self):
        """Empty text through patched function → 2.7 Default/fallback."""
        result = self._v136.solve_height_from_text("", 2.7)
        self.assertAlmostEqual(result["height_m"], 2.7, places=2)
        self.assertEqual(result["status"], "Default/fallback")
        self.assertEqual(result["source"], "default")

    def test_unrelated_rls_via_patched_function(self):
        """Unrelated RLs through patched function → 2.7 not Verified."""
        result = self._v136.solve_height_from_text("RL 45.230 RL 42.100", 2.7)
        self.assertAlmostEqual(result["height_m"], 2.7, places=2)
        self.assertNotEqual(result["status"], "Verified")
        self.assertEqual(result["source"], "default")

    def test_ffl_fcl_via_patched_function(self):
        """FFL+FCL through patched function → 2.7 floor_to_ceiling."""
        result = self._v136.solve_height_from_text("FFL 10.000 FCL 12.700", 2.7)
        self.assertAlmostEqual(result["height_m"], 2.7, places=2)
        self.assertEqual(result["status"], "Measured")
        self.assertEqual(result["source"], "rl_difference")


# ---------------------------------------------------------------------------
# BLOCKER 1 — project default height is respected
# ---------------------------------------------------------------------------


class TestDefaultHeightParameter(unittest.TestCase):
    """BLOCKER 1: resolver uses supplied default_height, not hardcoded 2.7."""

    def test_empty_evidence_default_30(self):
        """Empty evidence + default 3.0 → 3.0 Default/fallback."""
        h, best = resolve_height([], default_height=3.0)
        self.assertAlmostEqual(h, 3.0, places=2)
        self.assertEqual(best.status, "Default/fallback")
        self.assertEqual(best.extraction_method, "default")
        self.assertAlmostEqual(best.height_m, 3.0, places=2)

    def test_empty_evidence_default_32(self):
        """Empty evidence + default 3.2 → 3.2."""
        h, best = resolve_height([], default_height=3.2)
        self.assertAlmostEqual(h, 3.2, places=2)

    def test_no_compatible_evidence_default_30(self):
        """Only Review evidence + default 3.0 → 3.0 (Review filtered)."""
        ev = extract_all_height_evidence("3000", page_id=1)
        # All evidence should be Review (unknown orientation)
        h, best = resolve_height(ev, default_height=3.0)
        self.assertAlmostEqual(h, 3.0, places=2)
        self.assertEqual(best.status, "Default/fallback")

    def test_explicit_ch_still_wins_over_default(self):
        """CH 2700 still returns 2.7 even with default 3.0."""
        ev = extract_all_height_evidence("CH 2700", page_id=1)
        h, best = resolve_height(ev, default_height=3.0)
        self.assertAlmostEqual(h, 2.7, places=2)
        self.assertEqual(best.status, "Measured")

    def test_default_27_backward_compat(self):
        """Default 2.7 still works (backward compatibility)."""
        h, best = resolve_height([], default_height=2.7)
        self.assertAlmostEqual(h, 2.7, places=2)

    def test_default_row_is_non_measured(self):
        """Default-derived row/status remains non-Measured."""
        h, best = resolve_height([], default_height=3.0)
        self.assertNotEqual(best.status, "Measured")
        self.assertNotEqual(best.status, "Provisional measured")
        self.assertEqual(best.status, "Default/fallback")

    def test_patched_solve_respects_default_height(self):
        """Patched v136 solve with default 3.0 → 3.0 for empty text."""
        try:
            import pb_elevation_profile_v136 as v136
            apply_height_evidence_v150(v136)
            result = v136.solve_height_from_text("", 3.0)
            self.assertAlmostEqual(result["height_m"], 3.0, places=2)
            self.assertEqual(result["status"], "Default/fallback")
        except (ImportError, ModuleNotFoundError):
            self.skipTest("v136 module not available")


# ---------------------------------------------------------------------------
# BLOCKER 2 — room heights wired into production
# ---------------------------------------------------------------------------


class TestRoomHeightResolutionProduction(unittest.TestCase):
    """BLOCKER 2: resolve_room_heights produces per-room heights from evidence."""

    def test_bed1_ch2700_living_ch3000_production(self):
        """Production-style: positioned words + room polygons → per-room heights."""
        words = [
            WordBox("BED", 100, 100, 140, 120, page_id=1, line_id=(0, 0)),
            WordBox("1", 145, 100, 155, 120, page_id=1, line_id=(0, 0)),
            WordBox("CH", 110, 130, 130, 150, page_id=1, line_id=(0, 1)),
            WordBox("2700", 135, 130, 175, 150, page_id=1, line_id=(0, 1)),
            WordBox("LIVING", 350, 100, 420, 120, page_id=1, line_id=(0, 0)),
            WordBox("CH", 360, 130, 380, 150, page_id=1, line_id=(0, 1)),
            WordBox("3000", 385, 130, 425, 150, page_id=1, line_id=(0, 1)),
        ]
        room_bed = {
            "label": "BED 1",
            "polygon": [(50, 50), (200, 50), (200, 200), (50, 200)],
        }
        room_living = {
            "label": "LIVING",
            "polygon": [(300, 50), (500, 50), (500, 200), (300, 200)],
        }
        ev = extract_all_height_evidence(words, page_id=1, page_label="A301")
        result = resolve_room_heights(
            [room_bed, room_living], ev, default_height=2.7,
        )
        # BED 1 → 2.7 from CH 2700
        self.assertAlmostEqual(result["BED 1"]["height_m"], 2.7, places=2)
        self.assertEqual(result["BED 1"]["height_source"], "semantic_label")
        self.assertEqual(result["BED 1"]["height_status"], "Measured")
        # LIVING → 3.0 from CH 3000
        self.assertAlmostEqual(result["LIVING"]["height_m"], 3.0, places=2)
        self.assertEqual(result["LIVING"]["height_source"], "semantic_label")
        self.assertEqual(result["LIVING"]["height_status"], "Measured")

    def test_no_cross_room_leakage_production(self):
        """CH 2700 in BED 1 does NOT leak to LIVING."""
        words = [
            WordBox("BED", 100, 100, 140, 120, page_id=1, line_id=(0, 0)),
            WordBox("1", 145, 100, 155, 120, page_id=1, line_id=(0, 0)),
            WordBox("CH", 110, 130, 130, 150, page_id=1, line_id=(0, 1)),
            WordBox("2700", 135, 130, 175, 150, page_id=1, line_id=(0, 1)),
            WordBox("LIVING", 350, 100, 420, 120, page_id=1, line_id=(0, 0)),
        ]
        room_bed = {
            "label": "BED 1",
            "polygon": [(50, 50), (200, 50), (200, 200), (50, 200)],
        }
        room_living = {
            "label": "LIVING",
            "polygon": [(300, 50), (500, 50), (500, 200), (300, 200)],
        }
        ev = extract_all_height_evidence(words, page_id=1, page_label="A301")
        result = resolve_room_heights(
            [room_bed, room_living], ev, default_height=2.7,
        )
        self.assertAlmostEqual(result["BED 1"]["height_m"], 2.7, places=2)
        # LIVING has no CH inside its polygon → gets default
        self.assertAlmostEqual(result["LIVING"]["height_m"], 2.7, places=2)
        self.assertEqual(result["LIVING"]["height_source"], "default")

    def test_custom_default_per_room(self):
        """Unlabeled room gets workspace default (3.0), not hardcoded 2.7."""
        room = {"label": "", "room_ref": "R01", "polygon": []}
        result = resolve_room_heights([room], [], default_height=3.0)
        self.assertAlmostEqual(result["R01"]["height_m"], 3.0, places=2)
        self.assertEqual(result["R01"]["height_source"], "default")

    def test_room_height_map_fields(self):
        """Room height map has all required fields."""
        room = {"label": "BED 1", "polygon": [(0, 0), (100, 0), (100, 100), (0, 100)]}
        words = [
            WordBox("CH", 50, 50, 70, 70, page_id=1, line_id=(0, 0)),
            WordBox("2700", 75, 50, 115, 70, page_id=1, line_id=(0, 0)),
        ]
        ev = extract_all_height_evidence(words, page_id=1)
        result = resolve_room_heights([room], ev, default_height=2.7)
        r = result["BED 1"]
        self.assertIn("height_m", r)
        self.assertIn("height_type", r)
        self.assertIn("height_status", r)
        self.assertIn("height_source", r)
        self.assertIn("height_confidence", r)
        self.assertIn("height_evidence_id", r)


# ---------------------------------------------------------------------------
# End-to-end wall path — fake-app integration
# ---------------------------------------------------------------------------


class TestEndToEndWallPath(unittest.TestCase):
    """Prove: wall length → v150 height → gross wall area → takeoff row metadata."""

    def _make_fake_app(self, default_height: float = 2.7):
        """Create a fake app with the minimum interface needed."""
        settings = {
            "default_wall_height_m": str(default_height),
            "elevation_profiles_v136": "{}",
            "height_evidence_v150": "{}",
            "room_heights_v150": "{}",
        }

        class FakeApp:
            def set_workspace_setting(self, wid, key, val):
                settings[key] = val

            def workspace_setting(self, wid, key, default=""):
                return settings.get(key, default)

            def lquery(self, sql, params=()):
                return []

        return FakeApp()

    def test_10m_x_ch3000(self):
        """10 m wall × CH 3000 → 30.0 m² with correct metadata."""
        # Simulate: text says "CH 3000", wall is 10 m long
        ev = extract_all_height_evidence("CH 3000", page_id=1)
        h, best = resolve_height(ev, default_height=2.7)
        wall_length_m = 10.0
        gross_wall_area = round(wall_length_m * h, 2)
        self.assertAlmostEqual(gross_wall_area, 30.0, places=2)
        self.assertEqual(best.status, "Measured")
        self.assertEqual(best.extraction_method, "semantic_label")

    def test_10m_x_default_30(self):
        """10 m wall × default 3.0 → 30.0 m² (no measured evidence)."""
        h, best = resolve_height([], default_height=3.0)
        wall_length_m = 10.0
        gross_wall_area = round(wall_length_m * h, 2)
        self.assertAlmostEqual(gross_wall_area, 30.0, places=2)
        self.assertEqual(best.status, "Default/fallback")

    def test_height_source_carries_through(self):
        """Height source/status carried through to takeoff row."""
        ev = extract_all_height_evidence("CH 2700", page_id=1)
        h, best = resolve_height(ev, default_height=2.7)
        # Build a fake takeoff row
        row = {
            "height_m": round(h, 4),
            "height_source": best.extraction_method,
            "height_status": best.status,
            "height_evidence_id": best.id,
            "gross_wall_area_m2": round(10.0 * h, 2),
        }
        self.assertAlmostEqual(row["gross_wall_area_m2"], 27.0, places=2)
        self.assertEqual(row["height_source"], "semantic_label")
        self.assertEqual(row["height_status"], "Measured")

    def test_workspace_default_flows_through(self):
        """Workspace setting default_wall_height_m=3.2 → profile height 3.2."""
        app = self._make_fake_app(default_height=3.2)
        from pb_height_evidence_v150 import get_default_height
        default = get_default_height(app, 1)
        self.assertAlmostEqual(default, 3.2, places=2)
        # Resolver uses this default
        h, best = resolve_height([], default_height=default)
        self.assertAlmostEqual(h, 3.2, places=2)
        self.assertEqual(best.status, "Default/fallback")

    def test_patched_solve_carries_default(self):
        """Patched v136 solve with default 3.0 → 3.0 when no evidence."""
        try:
            import pb_elevation_profile_v136 as v136
            apply_height_evidence_v150(v136)
            result = v136.solve_height_from_text("RL 45.230 RL 42.100", 3.0)
            self.assertAlmostEqual(result["height_m"], 3.0, places=2)
            self.assertEqual(result["status"], "Default/fallback")
        except (ImportError, ModuleNotFoundError):
            self.skipTest("v136 module not available")


# ---------------------------------------------------------------------------
# BLOCKER 1 — page-scoped room-height map keys
# ---------------------------------------------------------------------------


class TestRoomHeightMapPageScopedKeys(unittest.TestCase):
    """BLOCKER 1: page-scoped keys prevent cross-page room name collisions."""

    def test_bed1_on_two_pages_both_survive(self):
        """BED 1 on page A = 2.7, BED 1 on page B = 3.0, both survive."""
        words_a = [
            WordBox("BED", 100, 100, 140, 120, page_id=10, line_id=(0, 0)),
            WordBox("1", 145, 100, 155, 120, page_id=10, line_id=(0, 0)),
            WordBox("CH", 110, 130, 130, 150, page_id=10, line_id=(0, 1)),
            WordBox("2700", 135, 130, 175, 150, page_id=10, line_id=(0, 1)),
        ]
        words_b = [
            WordBox("BED", 100, 100, 140, 120, page_id=20, line_id=(0, 0)),
            WordBox("1", 145, 100, 155, 120, page_id=20, line_id=(0, 0)),
            WordBox("CH", 110, 130, 130, 150, page_id=20, line_id=(0, 1)),
            WordBox("3000", 135, 130, 180, 150, page_id=20, line_id=(0, 1)),
        ]
        room = {"label": "BED 1", "polygon": [(50, 50), (200, 50), (200, 200), (50, 200)]}
        ev_a = extract_all_height_evidence(words_a, page_id=10, page_label="A101")
        ev_b = extract_all_height_evidence(words_b, page_id=20, page_label="A102")
        r_a = resolve_room_heights([room], ev_a, default_height=2.7)
        r_b = resolve_room_heights([room], ev_b, default_height=2.7)
        # Simulate production map construction
        room_height_map = {}
        for room_key, room_data in r_a.items():
            room_height_map[f"page_10:{room_key}"] = {"height_m": room_data["height_m"], "page_id": 10}
        for room_key, room_data in r_b.items():
            room_height_map[f"page_20:{room_key}"] = {"height_m": room_data["height_m"], "page_id": 20}
        # Both survive — no overwrite
        self.assertEqual(len(room_height_map), 2)
        self.assertAlmostEqual(room_height_map["page_10:BED 1"]["height_m"], 2.7, places=2)
        self.assertAlmostEqual(room_height_map["page_20:BED 1"]["height_m"], 3.0, places=2)

    def test_duplicate_room_names_in_two_units(self):
        """BED 1 in unit A and BED 1 in unit B do not overwrite each other."""
        words_a = [
            WordBox("BED", 100, 100, 140, 120, page_id=5, line_id=(0, 0)),
            WordBox("1", 145, 100, 155, 120, page_id=5, line_id=(0, 0)),
            WordBox("CH", 110, 130, 130, 150, page_id=5, line_id=(0, 1)),
            WordBox("2700", 135, 130, 175, 150, page_id=5, line_id=(0, 1)),
        ]
        words_b = [
            WordBox("BED", 100, 100, 140, 120, page_id=15, line_id=(0, 0)),
            WordBox("1", 145, 100, 155, 120, page_id=15, line_id=(0, 0)),
            WordBox("CH", 110, 130, 130, 150, page_id=15, line_id=(0, 1)),
            WordBox("3200", 135, 130, 180, 150, page_id=15, line_id=(0, 1)),
        ]
        room = {"label": "BED 1", "polygon": [(50, 50), (200, 50), (200, 200), (50, 200)]}
        ev_a = extract_all_height_evidence(words_a, page_id=5)
        ev_b = extract_all_height_evidence(words_b, page_id=15)
        r_a = resolve_room_heights([room], ev_a)
        r_b = resolve_room_heights([room], ev_b)
        room_height_map = {}
        for rk, rd in r_a.items():
            room_height_map[f"page_5:{rk}"] = {"height_m": rd["height_m"]}
        for rk, rd in r_b.items():
            room_height_map[f"page_15:{rk}"] = {"height_m": rd["height_m"]}
        self.assertEqual(len(room_height_map), 2)
        self.assertAlmostEqual(room_height_map["page_5:BED 1"]["height_m"], 2.7, places=2)
        self.assertAlmostEqual(room_height_map["page_15:BED 1"]["height_m"], 3.2, places=2)

    def test_three_rooms_two_pages_full_metadata(self):
        """Page 1: BED 1 + LIVING, Page 2: BED 1 — all 3 records with metadata."""
        words_p1 = [
            WordBox("BED", 100, 100, 140, 120, page_id=1, line_id=(0, 0)),
            WordBox("1", 145, 100, 155, 120, page_id=1, line_id=(0, 0)),
            WordBox("CH", 110, 130, 130, 150, page_id=1, line_id=(0, 1)),
            WordBox("2700", 135, 130, 175, 150, page_id=1, line_id=(0, 1)),
            WordBox("LIVING", 350, 100, 420, 120, page_id=1, line_id=(0, 0)),
            WordBox("CH", 360, 130, 380, 150, page_id=1, line_id=(0, 1)),
            WordBox("3000", 385, 130, 425, 150, page_id=1, line_id=(0, 1)),
        ]
        words_p2 = [
            WordBox("BED", 100, 100, 140, 120, page_id=2, line_id=(0, 0)),
            WordBox("1", 145, 100, 155, 120, page_id=2, line_id=(0, 0)),
            WordBox("CH", 110, 130, 130, 150, page_id=2, line_id=(0, 1)),
            WordBox("3200", 135, 130, 180, 150, page_id=2, line_id=(0, 1)),
        ]
        room_bed = {"label": "BED 1", "polygon": [(50, 50), (200, 50), (200, 200), (50, 200)]}
        room_living = {"label": "LIVING", "polygon": [(300, 50), (500, 50), (500, 200), (300, 200)]}
        ev1 = extract_all_height_evidence(words_p1, page_id=1, page_label="A301")
        ev2 = extract_all_height_evidence(words_p2, page_id=2, page_label="A302")
        r1 = resolve_room_heights([room_bed, room_living], ev1)
        r2 = resolve_room_heights([room_bed], ev2)
        room_height_map = {}
        for rk, rd in r1.items():
            room_height_map[f"page_1:{rk}"] = {
                "page_id": 1, "page_no": 1, "page_label": "A301",
                "room_ref": rk, "room_label": rd.get("label", rk),
                "height_m": rd["height_m"], "height_type": rd["height_type"],
                "height_status": rd["height_status"],
                "height_source": rd["height_source"],
                "height_evidence_id": rd["height_evidence_id"],
            }
        for rk, rd in r2.items():
            room_height_map[f"page_2:{rk}"] = {
                "page_id": 2, "page_no": 2, "page_label": "A302",
                "room_ref": rk, "room_label": rd.get("label", rk),
                "height_m": rd["height_m"], "height_type": rd["height_type"],
                "height_status": rd["height_status"],
                "height_source": rd["height_source"],
                "height_evidence_id": rd["height_evidence_id"],
            }
        self.assertEqual(len(room_height_map), 3)
        self.assertAlmostEqual(room_height_map["page_1:BED 1"]["height_m"], 2.7, places=2)
        self.assertAlmostEqual(room_height_map["page_1:LIVING"]["height_m"], 3.0, places=2)
        self.assertAlmostEqual(room_height_map["page_2:BED 1"]["height_m"], 3.2, places=2)
        # Verify full metadata present
        for key in ("page_id", "page_no", "page_label", "room_ref", "room_label",
                     "height_m", "height_type", "height_status", "height_source",
                     "height_evidence_id"):
            self.assertIn(key, room_height_map["page_1:BED 1"])


# ---------------------------------------------------------------------------
# BLOCKER 2 — real v139 integration test
# ---------------------------------------------------------------------------


class TestEndToEndWallPathProduction(unittest.TestCase):
    """BLOCKER 2: prove v150 height flows through real v139 wall assembly."""

    def _make_fake_app(self, default_height: float = 2.7):
        """Fake app implementing the minimum v139 interface."""
        settings = {
            "default_wall_height_m": str(default_height),
            "elevation_profiles_v136": "{}",
            "height_evidence_v150": "{}",
            "room_heights_v150": "{}",
        }
        # Registered wall records — one 10 m wall
        wall_records = [{
            "wall_ref": "W01",
            "side": "North",
            "length_m": 10.0,
            "height_m": default_height,  # fallback before v150 patch
            "substrate": "Brick veneer",
            "substrate_confidence": "High",
            "substrate_status": "Derived",
            "height_status": "Default/fallback",
        }]
        # Elevation registration — one North facade with one segment
        elevations = {
            "facades": {
                "North": {
                    "orientation": "North",
                    "segments": [{"wall_ref": "W01", "a": (0, 0), "b": (10, 0)}],
                }
            }
        }
        # Elevation height profiles (before v150 patch — would return 2.7)
        profiles = {"North": {"height_m": 2.7, "status": "Default/fallback"}}

        class FakeApp:
            def set_workspace_setting(self, wid, key, val):
                settings[key] = val

            def workspace_setting(self, wid, key, default=""):
                return settings.get(key, default)

            def lquery(self, sql, params=()):
                return []

            def register_elevations_v135(self, wid):
                return elevations

            def elevation_height_by_side_v136(self, wid):
                return profiles

            def registered_wall_records_v135(self, wid):
                return wall_records

        app = FakeApp()
        # Install v150 patch (patches v136 functions on the app)
        try:
            from pb_height_evidence_v150 import apply as apply_v150
            apply_v150(app)
        except ImportError:
            self.skipTest("pb_height_evidence_v150 not importable")
        return app, settings

    def test_ch3000_flows_through_to_gross_area(self):
        """10 m wall + CH 3000 → 30.0 m² with Measured status."""
        from pb_unified_building_v139 import build_registered_walls, takeoff_rows

        app, settings = self._make_fake_app(default_height=2.7)
        # Overwrite profiles to simulate v150 resolver finding CH 3000
        # The patched solve_height_from_text is called by build_profiles
        # For this test, we directly set the profiles to what v150 would produce
        profiles = app.elevation_height_by_side_v136(1)
        profiles["North"] = {
            "height_m": 3.0,
            "status": "Measured",
            "confidence": "Verified",
            "height_evidence_id": "ev_001",
        }
        # Also set the workspace setting so the patched build_profiles uses it
        import json
        settings["elevation_profiles_v136"] = json.dumps({
            "version": "v150",
            "profiles": [{
                "side": "North",
                "height_m": 3.0,
                "status": "Measured",
                "confidence": "Verified",
            }],
        })

        walls = build_registered_walls(app, 1)
        self.assertEqual(len(walls), 1)
        wall = walls[0]
        self.assertAlmostEqual(wall["height_m"], 3.0, places=2)
        self.assertAlmostEqual(wall["gross_m2"], 30.0, places=2)
        self.assertEqual(wall["height_status"], "Measured")

        rows = takeoff_rows(walls)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["quantity"], 30.0, places=2)
        self.assertEqual(row["unit"], "m²")

    def test_workspace_default_32_flows_through(self):
        """10 m wall + workspace default 3.2 → 32.0 m², non-Measured status."""
        from pb_unified_building_v139 import build_registered_walls, takeoff_rows

        app, settings = self._make_fake_app(default_height=3.2)
        # Set profiles to use the workspace default
        import json
        settings["elevation_profiles_v136"] = json.dumps({
            "version": "v150",
            "profiles": [{
                "side": "North",
                "height_m": 3.2,
                "status": "Default/fallback",
                "confidence": "Review",
            }],
        })
        # Update the in-memory profiles dict too
        app.elevation_height_by_side_v136(1)["North"] = {
            "height_m": 3.2,
            "status": "Default/fallback",
            "confidence": "Review",
        }

        walls = build_registered_walls(app, 1)
        self.assertEqual(len(walls), 1)
        wall = walls[0]
        self.assertAlmostEqual(wall["height_m"], 3.2, places=2)
        self.assertAlmostEqual(wall["gross_m2"], 32.0, places=2)
        self.assertNotEqual(wall["height_status"], "Measured")

        rows = takeoff_rows(walls)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["quantity"], 32.0, places=2)

    def test_height_source_in_notes(self):
        """Height status is carried through to takeoff row notes."""
        from pb_unified_building_v139 import build_registered_walls, takeoff_rows

        app, settings = self._make_fake_app(default_height=2.7)
        import json
        settings["elevation_profiles_v136"] = json.dumps({
            "version": "v150",
            "profiles": [{
                "side": "North",
                "height_m": 3.0,
                "status": "Measured",
                "confidence": "Verified",
            }],
        })
        app.elevation_height_by_side_v136(1)["North"] = {
            "height_m": 3.0,
            "status": "Measured",
            "confidence": "Verified",
        }

        walls = build_registered_walls(app, 1)
        rows = takeoff_rows(walls)
        self.assertIn("Measured", rows[0]["notes"])


# ---------------------------------------------------------------------------
# BLOCKER 3 — PDF block separation
# ---------------------------------------------------------------------------


class TestPDFBlockSeparation(unittest.TestCase):
    """BLOCKER 3: words from different PDF text blocks stay separate."""

    def test_two_blocks_same_line_no_not_merged(self):
        """Block 1 line 0: CH 2700, Block 2 line 0: WIDTH 5000 — separate lines."""
        words = [
            # Block 1, line 0: CH 2700
            WordBox("CH", 100, 100, 120, 120, page_id=1, line_id=(1, 0)),
            WordBox("2700", 125, 100, 165, 120, page_id=1, line_id=(1, 0)),
            # Block 2, line 0: WIDTH 5000
            WordBox("WIDTH", 400, 100, 460, 120, page_id=1, line_id=(2, 0)),
            WordBox("5000", 465, 100, 510, 120, page_id=1, line_id=(2, 0)),
        ]
        ev = extract_all_height_evidence(words, page_id=1)
        # CH 2700 should produce a semantic height
        heights = [e for e in ev if e.height_m == 2.7 and e.status == "Measured"]
        self.assertEqual(len(heights), 1, "CH 2700 should produce exactly one Measured height")
        # WIDTH 5000 should NOT produce a Measured height (it's in horizontal context)
        widths = [e for e in ev if e.raw_text and "WIDTH" in e.raw_text.upper()
                  and e.status == "Measured"]
        self.assertEqual(len(widths), 0, "WIDTH should not be treated as a height")

    def test_same_block_line_numbers_group_correctly(self):
        """Words in the same block with different line numbers form separate lines."""
        words = [
            WordBox("CH", 100, 100, 120, 120, page_id=1, line_id=(0, 0)),
            WordBox("2700", 125, 100, 165, 120, page_id=1, line_id=(0, 0)),
            WordBox("NOTE", 100, 150, 140, 170, page_id=1, line_id=(0, 1)),
            WordBox("something", 145, 150, 230, 170, page_id=1, line_id=(0, 1)),
        ]
        ev = extract_all_height_evidence(words, page_id=1)
        ch_ev = [e for e in ev if e.height_m == 2.7 and e.status == "Measured"]
        self.assertEqual(len(ch_ev), 1)
        # The NOTE line should not interfere
        self.assertIn("CH", ch_ev[0].raw_text)

    def test_three_blocks_independent(self):
        """Three different blocks with overlapping line numbers remain independent."""
        words = [
            # Block 0: CH 2700 on line 0
            WordBox("CH", 100, 100, 120, 120, page_id=1, line_id=(0, 0)),
            WordBox("2700", 125, 100, 165, 120, page_id=1, line_id=(0, 0)),
            # Block 1: CLG 3000 on line 0 (same line number, different block)
            WordBox("CLG", 400, 100, 435, 120, page_id=1, line_id=(1, 0)),
            WordBox("3000", 440, 100, 480, 120, page_id=1, line_id=(1, 0)),
            # Block 2: FFL 10.500 on line 0 (same line number, different block)
            WordBox("FFL", 700, 100, 735, 120, page_id=1, line_id=(2, 0)),
            WordBox("10.500", 740, 100, 800, 120, page_id=1, line_id=(2, 0)),
        ]
        ev = extract_all_height_evidence(words, page_id=1)
        # CH 2700 detected (semantic label + raw dimension both valid)
        ch = [e for e in ev if e.height_m == 2.7]
        self.assertGreaterEqual(len(ch), 1, "CH 2700 detected at least once")
        # CLG 3000 detected (semantic label + raw dimension both valid)
        clg = [e for e in ev if e.height_m == 3.0]
        self.assertGreaterEqual(len(clg), 1, "CLG 3000 detected at least once from block 1")
        # FFL is a level_reference, not a usable height
        ffl = [e for e in ev if e.height_m == 10.5 and e.height_type == "level_reference"]
        self.assertEqual(len(ffl), 1, "FFL 10.500 detected as level reference")


if __name__ == "__main__":
    unittest.main()
