"""
tests/test_workstream_b5_opening_deduction_authority.py — Workstream P7

Comprehensive audit and regression verification for B5 opening deduction authority:
1. Exact intended boolean/evidence state matrix:
   (True, False, 1, 0, "true", "false", "yes", "no", None, NaN, list/object values)
   for reconciliation_complete, deduct, and manual_override_confirmed.
2. Workspace identity and binding validation:
   Positive integer workspace IDs, rejection of non-positive, non-finite, sentinel,
   and mismatched workspaces.
3. Page reference validation:
   Positive integer page numbers and page IDs, rejection of non-positive, non-finite,
   and sentinel values.
4. Host wall validation:
   Legitimate wall references vs empty, unassigned, unknown, and sentinel wall refs
   ("nan", "null", "none", "0", "-", "n/a", booleans).
5. Opening physical identity validation:
   Opening instance ID validation, rejection of empty, whitespace, sentinel identities.
6. Dimension and basis safety:
   Strict rough-opening basis, positive finite width/height/area, finite confidence
   channels in [0.0, 1.0], rejection of NaN/inf confidence poisoning.
7. Evidence source validation:
   Valid dimension and extraction sources, rejection of sentinel/empty sources.
8. End-to-end deduction and wall area calculation safety:
   Strict boolean deduct filtering, prevention of NaN area poisoning, and refusal
   to merge unproven/excluded B5 rows into wall area deductions.
"""
from __future__ import annotations

import math
from typing import Any, Dict
import unittest

from pb_opening_evidence_v170 import (
    DEDUCTION_AUTO_ELIGIBLE,
    DEDUCTION_REVIEW,
    DIMENSION_BASIS_ROUGH_OPENING,
    OPENING_TYPE_DOOR,
    OpeningEvidence,
    deducted_area_m2,
)
from pb_opening_deduction_v174 import (
    passes_eligibility_gate,
    net_wall_area_after_deductions,
)
from pb_opening_production_v175 import (
    _is_authorised_b5_automatic,
    is_authorised_deduction,
    _assigned_wall,
    _row_identity_agrees,
    merge_b5_authoritative,
)


def _valid_inst(**kwargs) -> OpeningEvidence:
    """Construct a pristine, fully-eligible OpeningEvidence instance."""
    base_kwargs = dict(
        opening_instance_id="inst_b5_001",
        type_mark="D01",
        workspace_id=7,
        page_id=5,
        page_no=1,
        wall_ref="W01",
        opening_type=OPENING_TYPE_DOOR,
        width_m=0.82,
        height_m=2.10,
        dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
        dimension_source="schedule_parse",
        extraction_method="plan_vector",
        geometry_confidence=0.85,
        dimension_confidence=0.90,
        association_confidence=0.88,
        deduction_status=DEDUCTION_AUTO_ELIGIBLE,
        reconciliation_complete=True,
    )
    base_kwargs.update(kwargs)
    inst = OpeningEvidence(**base_kwargs)
    inst.compute_area()
    return inst


def _valid_row(**kwargs) -> Dict[str, Any]:
    """Construct a pristine, fully-eligible B5 persisted row dictionary."""
    base = {
        "opening_instance_id": "inst_b5_001",
        "type_mark": "D01",
        "workspace_id": 7,
        "page_id": 5,
        "page_no": 1,
        "wall_ref": "W01",
        "resolved_wall_ref": "W01",
        "opening_type": "door",
        "width_m": 0.82,
        "height_m": 2.10,
        "area_m2": round(0.82 * 2.10, 4),
        "quantity": 1,
        "dimension_basis": DIMENSION_BASIS_ROUGH_OPENING,
        "dimension_source": "schedule_parse",
        "extraction_method": "plan_vector",
        "geometry_confidence": 0.85,
        "dimension_confidence": 0.90,
        "association_confidence": 0.88,
        "deduction_status": DEDUCTION_AUTO_ELIGIBLE,
        "deduction_decision": "deducted",
        "reconciliation_complete": True,
        "deduct": True,
    }
    base.update(kwargs)
    return base


# Non-strict-boolean variants that must fail closed:
INVALID_BOOL_VARIANTS = [
    False,
    1,
    0,
    "true",
    "True",
    "TRUE",
    "false",
    "False",
    "FALSE",
    "yes",
    "Yes",
    "no",
    "No",
    None,
    float("nan"),
    [True],
    [1],
    {"valid": True},
    object(),
]


class TestB5BooleanEvidenceStateMatrix(unittest.TestCase):
    """Verifies that only strict Python True grants deduction authority."""

    def test_reconciliation_complete_boolean_matrix_in_gate(self):
        """passes_eligibility_gate requires strict True for reconciliation_complete."""
        inst_valid = _valid_inst(reconciliation_complete=True)
        self.assertTrue(passes_eligibility_gate(inst_valid))

        for variant in INVALID_BOOL_VARIANTS:
            inst = _valid_inst(reconciliation_complete=variant)
            self.assertFalse(
                passes_eligibility_gate(inst),
                f"reconciliation_complete={variant!r} ({type(variant)}) should fail eligibility gate",
            )

    def test_reconciliation_complete_boolean_matrix_in_b5_automatic(self):
        """_is_authorised_b5_automatic requires strict True for reconciliation_complete."""
        row_valid = _valid_row(reconciliation_complete=True)
        self.assertTrue(_is_authorised_b5_automatic(row_valid))

        for variant in INVALID_BOOL_VARIANTS:
            row = _valid_row(reconciliation_complete=variant)
            self.assertFalse(
                _is_authorised_b5_automatic(row),
                f"reconciliation_complete={variant!r} ({type(variant)}) should fail _is_authorised_b5_automatic",
            )

    def test_deduct_flag_matrix_in_b5_automatic(self):
        """_is_authorised_b5_automatic must fail closed if deduct is present and not True."""
        row_valid = _valid_row(deduct=True)
        self.assertTrue(_is_authorised_b5_automatic(row_valid))

        for variant in INVALID_BOOL_VARIANTS:
            row = _valid_row(deduct=variant)
            self.assertFalse(
                _is_authorised_b5_automatic(row),
                f"deduct={variant!r} ({type(variant)}) should fail _is_authorised_b5_automatic",
            )

    def test_deduct_flag_matrix_in_is_authorised_deduction(self):
        """is_authorised_deduction requires strict True for deduct flag."""
        row_valid = _valid_row(deduct=True)
        self.assertTrue(is_authorised_deduction(row_valid))

        for variant in INVALID_BOOL_VARIANTS:
            row = _valid_row(deduct=variant)
            self.assertFalse(
                is_authorised_deduction(row),
                f"deduct={variant!r} ({type(variant)}) should fail is_authorised_deduction",
            )

    def test_manual_override_confirmed_matrix(self):
        """manual_override_confirmed requires strict True to grant manual authority."""
        row_manual_valid = _valid_row(
            reconciliation_complete=False,
            deduction_status=DEDUCTION_REVIEW,
            deduct=True,
            manual_override_confirmed=True,
        )
        self.assertTrue(is_authorised_deduction(row_manual_valid))

        for variant in INVALID_BOOL_VARIANTS:
            row = _valid_row(
                reconciliation_complete=False,
                deduction_status=DEDUCTION_REVIEW,
                deduct=True,
                manual_override_confirmed=variant,
            )
            self.assertFalse(
                is_authorised_deduction(row),
                f"manual_override_confirmed={variant!r} ({type(variant)}) should not grant manual authority",
            )


class TestB5WorkspaceAndPageValidation(unittest.TestCase):
    """Verifies workspace and page boundaries on opening deduction authority."""

    def test_workspace_id_validation_matrix(self):
        """Workspace IDs must be positive integers, rejecting non-positives and sentinels."""
        for good_ws in (1, 7, 42, "7", "100"):
            row = _valid_row(workspace_id=good_ws)
            self.assertTrue(_is_authorised_b5_automatic(row))

        for bad_ws in (0, -1, -42, "0", "-1", "nan", "NaN", "none", "null", "undefined", float("nan"), float("inf"), True, False, [1]):
            row = _valid_row(workspace_id=bad_ws)
            self.assertFalse(
                _is_authorised_b5_automatic(row),
                f"workspace_id={bad_ws!r} should fail closed",
            )

    def test_page_number_and_id_validation_matrix(self):
        """Page numbers and IDs must be positive integers."""
        for good_page in (1, 5, 20, "1", "5"):
            row = _valid_row(page_no=good_page, page_id=good_page)
            self.assertTrue(_is_authorised_b5_automatic(row))

        for bad_page in (0, -1, -5, "0", "-1", "nan", "NaN", "none", "null", float("nan"), float("inf"), True, False):
            row_no = _valid_row(page_no=bad_page)
            self.assertFalse(
                _is_authorised_b5_automatic(row_no),
                f"page_no={bad_page!r} should fail closed",
            )
            row_id = _valid_row(page_id=bad_page)
            self.assertFalse(
                _is_authorised_b5_automatic(row_id),
                f"page_id={bad_page!r} should fail closed",
            )

    def test_passes_eligibility_gate_page_and_workspace(self):
        """OpeningEvidence with invalid page or workspace fails eligibility gate."""
        for bad_p in (0, -1, float("nan")):
            inst_p = _valid_inst(page_no=bad_p)
            self.assertFalse(passes_eligibility_gate(inst_p))

        for bad_ws in (-1, float("nan")):
            inst_w = _valid_inst(workspace_id=bad_ws)
            self.assertFalse(passes_eligibility_gate(inst_w))

    def test_row_identity_agrees_rejects_sentinels_and_mismatches(self):
        """_row_identity_agrees enforces positive integer match and rejects sentinels."""
        self.assertTrue(_row_identity_agrees({"workspace_id": 7, "page_id": 5}, 7, 5))
        self.assertFalse(_row_identity_agrees({"workspace_id": 7, "page_id": 6}, 7, 5))
        self.assertFalse(_row_identity_agrees({"workspace_id": 8, "page_id": 5}, 7, 5))
        self.assertFalse(_row_identity_agrees({"workspace_id": "nan", "page_id": 5}, 7, 5))
        self.assertFalse(_row_identity_agrees({"workspace_id": 7, "page_id": "none"}, 7, 5))


class TestB5HostWallValidation(unittest.TestCase):
    """Verifies that only legitimate, assigned host walls authorize deductions."""

    def test_assigned_wall_helper_sentinels(self):
        """_assigned_wall rejects sentinels and empty strings."""
        for good in ("W01", "W-02", "EXT_WALL_NORTH", "Int_Partition"):
            self.assertEqual(_assigned_wall({"wall_ref": good}), good)

        for bad in (
            "", "   ", None, "unassigned", "Unassigned", "UNASSIGNED",
            "unassigned wall", "unknown", "none", "None", "NONE",
            "nan", "NaN", "null", "NULL", "undefined", "0", "-", "- ",
            "n/a", "na", False, True, float("nan"),
        ):
            self.assertEqual(
                _assigned_wall({"wall_ref": bad}),
                "",
                f"wall_ref={bad!r} must resolve to empty string",
            )

    def test_passes_eligibility_gate_rejects_sentinel_host_walls(self):
        """passes_eligibility_gate fails closed on sentinel host walls."""
        for bad in ("", "   ", "unassigned", "unknown", "none", "nan", "null", "undefined", "0", "-"):
            inst = _valid_inst(wall_ref=bad)
            self.assertFalse(
                passes_eligibility_gate(inst),
                f"wall_ref={bad!r} should fail eligibility gate",
            )

    def test_is_authorised_deduction_rejects_sentinel_host_walls(self):
        """is_authorised_deduction fails closed on sentinel host walls."""
        for bad in ("", "unassigned", "unknown", "none", "nan", "null", "undefined", "0", "-"):
            row = _valid_row(wall_ref=bad, resolved_wall_ref=bad)
            self.assertFalse(
                is_authorised_deduction(row),
                f"wall_ref={bad!r} should fail is_authorised_deduction",
            )


class TestB5OpeningIdentityValidation(unittest.TestCase):
    """Verifies physical opening instance identity requirements."""

    def test_opening_instance_id_in_gate(self):
        """OpeningEvidence must carry a non-empty, non-sentinel opening_instance_id."""
        inst_valid = _valid_inst(opening_instance_id="inst_12345")
        self.assertTrue(passes_eligibility_gate(inst_valid))

        for bad_id in ("", "   ", None, "nan", "none", "null", "undefined", True, False):
            inst = _valid_inst(opening_instance_id=bad_id)
            self.assertFalse(
                passes_eligibility_gate(inst),
                f"opening_instance_id={bad_id!r} should fail eligibility gate",
            )

    def test_opening_instance_id_in_b5_automatic(self):
        """B5 automatic row must carry a valid opening_instance_id if supplied."""
        row_valid = _valid_row(opening_instance_id="inst_12345")
        self.assertTrue(_is_authorised_b5_automatic(row_valid))

        for bad_id in ("", "   ", "nan", "none", "null", "undefined", True, False):
            row = _valid_row(opening_instance_id=bad_id)
            self.assertFalse(
                _is_authorised_b5_automatic(row),
                f"opening_instance_id={bad_id!r} should fail _is_authorised_b5_automatic",
            )


class TestB5DimensionsAndBasisValidation(unittest.TestCase):
    """Verifies dimensional safety, rough-opening basis, and confidence validity."""

    def test_non_finite_dimensions_in_gate(self):
        """passes_eligibility_gate rejects non-finite widths, heights, and areas."""
        for bad_dim in (float("nan"), float("inf"), -float("inf")):
            inst_w = _valid_inst(width_m=bad_dim)
            self.assertFalse(passes_eligibility_gate(inst_w))

            inst_h = _valid_inst(height_m=bad_dim)
            self.assertFalse(passes_eligibility_gate(inst_h))

            inst_a = _valid_inst()
            inst_a.area_m2 = bad_dim
            self.assertFalse(passes_eligibility_gate(inst_a))

    def test_confidence_nan_poisoning_prevented_in_gate(self):
        """NaN in any confidence channel must not bypass min() or grant authority."""
        for bad_conf in (float("nan"), float("inf"), -0.1, 1.5):
            inst_geom = _valid_inst(geometry_confidence=bad_conf)
            self.assertFalse(passes_eligibility_gate(inst_geom))

            inst_dim = _valid_inst(dimension_confidence=bad_conf)
            self.assertFalse(passes_eligibility_gate(inst_dim))

            inst_assoc = _valid_inst(association_confidence=bad_conf)
            self.assertFalse(passes_eligibility_gate(inst_assoc))

    def test_dimension_basis_strict_rough_opening(self):
        """Only rough_opening dimension basis qualifies for automatic deduction."""
        for bad_basis in ("frame", "leaf", "finish", "unknown", "", "nan", None, 123):
            inst = _valid_inst(dimension_basis=bad_basis)
            self.assertFalse(passes_eligibility_gate(inst))

            row = _valid_row(dimension_basis=bad_basis)
            self.assertFalse(_is_authorised_b5_automatic(row))


class TestB5SourceValidation(unittest.TestCase):
    """Verifies evidence source requirements."""

    def test_dimension_source_validation_in_gate(self):
        """OpeningEvidence must carry a non-sentinel dimension_source."""
        for good_source in ("schedule_parse", "plan_vector", "elevation_rect", "plan_detection", "manual"):
            inst = _valid_inst(dimension_source=good_source)
            self.assertTrue(passes_eligibility_gate(inst))

        for bad_source in ("", "   ", None, "unknown", "nan", "none", "null", "undefined", True, False):
            inst = _valid_inst(dimension_source=bad_source)
            self.assertFalse(
                passes_eligibility_gate(inst),
                f"dimension_source={bad_source!r} should fail eligibility gate",
            )

    def test_source_validation_in_b5_automatic(self):
        """Row with sentinel source fails _is_authorised_b5_automatic."""
        for bad_src in ("", "   ", "unknown", "nan", "none", "null", "undefined"):
            row = _valid_row(dimension_source=bad_src)
            self.assertFalse(
                _is_authorised_b5_automatic(row),
                f"dimension_source={bad_src!r} should fail _is_authorised_b5_automatic",
            )


class TestB5EndToEndDeductionSafety(unittest.TestCase):
    """Verifies net wall area calculation and B5 merge safety."""

    def test_deducted_area_m2_strict_bool_and_finite(self):
        """deducted_area_m2 ignores non-strict bool deduct and non-finite areas."""
        inst_valid = _valid_inst(width_m=1.0, height_m=2.0)
        inst_valid.deduct = True
        inst_valid.area_m2 = 2.0

        inst_fake_bool = _valid_inst(width_m=1.0, height_m=2.0)
        inst_fake_bool.deduct = 1  # non-strict bool
        inst_fake_bool.area_m2 = 2.0

        inst_nan_area = _valid_inst(width_m=1.0, height_m=2.0)
        inst_nan_area.deduct = True
        inst_nan_area.area_m2 = float("nan")

        # Only the strict True instance with finite area is summed
        total = deducted_area_m2([inst_valid, inst_fake_bool, inst_nan_area])
        self.assertEqual(total, 2.0)

    def test_net_wall_area_after_deductions_safety(self):
        """net_wall_area_after_deductions requires strict bool deduct and finite values."""
        inst1 = _valid_inst(wall_ref="W01")
        inst1.deduct = True
        inst1.area_m2 = 2.0

        inst2 = _valid_inst(wall_ref="W01")
        inst2.deduct = "true"  # non-strict bool
        inst2.area_m2 = 3.0

        res = net_wall_area_after_deductions(10.0, [inst1, inst2], "W01")
        self.assertTrue(res["valid"])
        self.assertEqual(res["net_area_m2"], 8.0)

    def test_merge_b5_authoritative_refuses_excluded_or_tampered_rows(self):
        """merge_b5_authoritative never promotes excluded or unproven B5 rows."""
        attached = [{"resolved_wall_ref": "W01", "deduct": False, "area_m2": 0.0}]

        # Row explicitly excluded (deduct=False)
        row_excluded = _valid_row(deduct=False)
        merged = merge_b5_authoritative(attached, [row_excluded])
        self.assertEqual(len(merged), 1)  # row_excluded was rejected

        # Row with sentinel workspace
        row_bad_ws = _valid_row(workspace_id="nan")
        merged2 = merge_b5_authoritative(attached, [row_bad_ws])
        self.assertEqual(len(merged2), 1)