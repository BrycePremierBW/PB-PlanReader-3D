from __future__ import annotations

from pb_takeoff_authority_v164 import (
    approve_model_surface_row,
    model_surface_authority,
    takeoff_row_publishability,
)


def _approved_surface(**changes):
    row = {
        "workspace_id": 101,
        "row_role": "model_surface",
        "inclusion_status": "INCLUSION",
        "quantity": 12.5,
        "unit": "m²",
    }
    row.update(changes)
    return approve_model_surface_row(
        row,
        source="A-401 / M-22",
        reviewed_by="Senior Estimator",
        reviewed_at="2026-09-04T10:00:00+10:00",
    )


def test_model_surface_approval_requires_complete_attributable_provenance():
    assert model_surface_authority(_approved_surface()) == (True, "APPROVED")
    for field in (
        "commercial_authority_status",
        "commercial_authority_source",
        "commercial_authority_reviewed_by",
        "commercial_authority_reviewed_at",
    ):
        row = _approved_surface()
        row[field] = ""
        allowed, _ = model_surface_authority(row)
        assert allowed is False, field


def test_only_real_approved_status_is_accepted():
    for status in ("REVIEW_REQUIRED", "approved-ish", "true", "1", None):
        row = _approved_surface()
        row["commercial_authority_status"] = status
        allowed, _ = model_surface_authority(row)
        assert allowed is False, status


def test_approval_fingerprint_invalidates_any_consequential_change():
    approved = _approved_surface()
    assert model_surface_authority(approved)[0] is True
    for field, value in (
        ("quantity", 13.0),
        ("source_reference", "different surface"),
        ("inclusion_status", "SEPARATE ITEM"),
        ("rate_per_unit", 120.0),
    ):
        changed = {**approved, field: value}
        allowed, reason = model_surface_authority(changed)
        assert allowed is False, field
        assert "no longer matches" in reason


def test_approval_cannot_be_replayed_into_another_workspace():
    approved = _approved_surface()
    replayed = {**approved, "workspace_id": 202}

    allowed, reason = model_surface_authority(replayed)

    assert allowed is False
    assert "no longer matches" in reason


def test_approval_requires_a_positive_workspace_identity():
    for workspace_id in (None, "", 0, -1, True, "not-an-id"):
        row = {
            "workspace_id": workspace_id,
            "row_role": "model_surface",
            "quantity": 12.5,
            "unit": "m²",
        }
        try:
            approve_model_surface_row(
                row,
                source="A-401 / M-22",
                reviewed_by="Senior Estimator",
                reviewed_at="2026-09-04T10:00:00+10:00",
            )
        except ValueError as exc:
            assert "workspace identity" in str(exc)
        else:
            raise AssertionError(f"workspace_id={workspace_id!r} was accepted")


def test_surface_source_prefix_cannot_be_laundered_by_erasing_role():
    row = {
        "row_role": "",
        "source_reference": "PB 3D Surface Editor v1.2.12 · mass:7:front",
        "inclusion_status": "INCLUSION",
    }
    assert takeoff_row_publishability(row)[0] is False


def test_publishability_filters_floor_exclusions_and_unapproved_3d_rows():
    assert takeoff_row_publishability({"row_role": "work", "inclusion_status": "included"})[0] is True
    assert takeoff_row_publishability({"row_role": "floor_area", "inclusion_status": "included"})[0] is False
    for exclusion in ("excluded", "EXCLUSION", "Exclude"):
        assert takeoff_row_publishability({"row_role": "work", "inclusion_status": exclusion})[0] is False
    unapproved = _approved_surface()
    unapproved["commercial_authority_status"] = "REVIEW_REQUIRED"
    unapproved["commercial_authority_fingerprint"] = ""
    assert takeoff_row_publishability(unapproved)[0] is False
    assert takeoff_row_publishability(_approved_surface())[0] is True
