import io
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from pb_takeoff_authority_v164 import (
    AUTHORITY_APPROVED,
    AUTHORITY_FINGERPRINT_FIELD,
    AUTHORITY_REVIEW_REQUIRED,
    AUTHORITY_REVIEWED_AT_FIELD,
    AUTHORITY_REVIEWED_BY_FIELD,
    AUTHORITY_SOURCE_FIELD,
    AUTHORITY_STATUS_FIELD,
    MODEL_SURFACE_ROLE,
    approve_model_surface_row,
    is_model_surface_row,
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


def test_p4_approval_variants_matrix():
    """P4: Test positive approval variants and strict fail-closed rejection of non-approvals."""
    import math

    # Positive approval variants (must succeed)
    positive_variants = [
        "APPROVED",
        "approved",
        "Approved",
        "  APPROVED  ",
        "\tapproved\n",
        " APPROVED ",
    ]
    for variant in positive_variants:
        row = _approved_surface()
        row["commercial_authority_status"] = variant
        allowed, reason = model_surface_authority(row)
        assert allowed is True, f"Expected {variant!r} to be allowed, got reason: {reason}"
        assert reason == "APPROVED"

    # Negative non-approval variants (must strictly fail closed without widening policy)
    negative_variants = [
        True,
        False,
        1,
        0,
        -1,
        None,
        float("nan"),
        object(),
        [],
        {},
        "REVIEW_REQUIRED",
        "approved-ish",
        "true",
        "false",
        "1",
        "0",
        "nan",
        "none",
        "null",
        "APPROVED_WITH_CONDITIONS",
        "DISAPPROVED",
        "REJECTED",
        "UNAPPROVED",
        "PENDING",
        "",
        "   ",
        "\t",
    ]
    for variant in negative_variants:
        row = _approved_surface()
        row["commercial_authority_status"] = variant
        allowed, reason = model_surface_authority(row)
        assert allowed is False, f"Expected {variant!r} to be rejected, got True!"
        assert "not received commercial approval" in reason


def test_p4_workspace_binding_validation():
    """P4: Missing or invalid workspace identity in row must fail closed in model_surface_authority."""
    for invalid_ws in (None, "", 0, -1, True, False, "not-an-id", float("nan")):
        row = _approved_surface()
        row["workspace_id"] = invalid_ws
        # Recompute fingerprint matching the invalid workspace to isolate workspace binding check
        row["commercial_authority_fingerprint"] = ""
        allowed, reason = model_surface_authority(row)
        assert allowed is False, f"Expected workspace_id={invalid_ws!r} to fail closed"
        assert "workspace identity" in reason or "no longer matches" in reason


def test_p4_sentinel_strings_in_provenance_rejected():
    """P4: Sentinel strings ('nan', 'none', 'null', whitespace) in source/reviewer/timestamp must fail closed."""
    for sentinel in ("nan", "none", "null", "NAN", "None", "NULL", "   ", ""):
        # In approve_model_surface_row
        row = {
            "workspace_id": 101,
            "row_role": "model_surface",
            "quantity": 12.5,
            "unit": "m²",
        }
        for field in ("source", "reviewed_by", "reviewed_at"):
            kwargs = {
                "source": "A-401 / M-22",
                "reviewed_by": "Senior Estimator",
                "reviewed_at": "2026-09-04T10:00:00+10:00",
            }
            kwargs[field] = sentinel
            try:
                approve_model_surface_row(row, **kwargs)
                raise AssertionError(f"Expected sentinel {sentinel!r} in {field} to raise ValueError")
            except ValueError:
                pass

        # In model_surface_authority
        for auth_field in (
            "commercial_authority_source",
            "commercial_authority_reviewed_by",
            "commercial_authority_reviewed_at",
        ):
            approved = _approved_surface()
            approved[auth_field] = sentinel
            allowed, _ = model_surface_authority(approved)
            assert allowed is False, f"Expected sentinel {sentinel!r} in {auth_field} to be rejected"


def test_p4_exhaustive_all_21_bound_fields_mutation_invalidation():
    """P4: Every single field in _AUTHORITY_BOUND_FIELDS causes fingerprint invalidation upon mutation."""
    from pb_takeoff_authority_v164 import _AUTHORITY_BOUND_FIELDS

    mutations = {
        "workspace_id": 999,
        "section": "Mutated Section",
        "element": "Mutated Element",
        "location": "Mutated Location",
        "substrate": "Mutated Substrate",
        "finish_system": "Mutated Finish",
        "quantity": 999.5,
        "unit": "lm",
        "quantity_status": "Mutated Status",
        "source_page": "Page Mutated",
        "source_reference": "Mutated Ref",
        "inclusion_status": "EXCLUDED",
        "coats": 5,
        "coverage_m2_per_litre": 50.0,
        "productivity_m2_per_hour": 50.0,
        "rate_per_unit": 999.0,
        "confidence": "low",
        "notes": "Mutated Notes",
        "row_role": "work",
        "commercial_authority_source": "Mutated Source",
        "commercial_authority_reviewed_by": "Another Person",
        "commercial_authority_reviewed_at": "2026-09-05T00:00:00Z",
    }

    assert len(_AUTHORITY_BOUND_FIELDS) == 22 or len(_AUTHORITY_BOUND_FIELDS) == 21
    for field in _AUTHORITY_BOUND_FIELDS:
        approved = _approved_surface()
        new_val = mutations.get(field, "mutated_value")
        changed = {**approved, field: new_val}
        allowed, reason = model_surface_authority(changed)
        assert allowed is False, f"Expected mutation of field {field!r} to be rejected"
        assert "no longer matches" in reason or "workspace identity" in reason


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


def _provisional_surface(**changes):
    row = {
        "workspace_id": 101,
        "section": "External",
        "element": "3D Front · Rendered Block",
        "location": "mass:1",
        "substrate": "Rendered Block",
        "finish_system": "To be confirmed",
        "quantity": 12.5,
        "unit": "m²",
        "quantity_status": "Provisional measured",
        "source_page": "3D model",
        "source_reference": "PB 3D Surface Editor v1.2.12 · mass:1:front",
        "inclusion_status": "PROVISIONAL",
        "confidence": "Derived",
        "row_role": "model_surface",
        "commercial_authority_status": "REVIEW_REQUIRED",
        "commercial_authority_source": "mass:1:front",
        "commercial_authority_reviewed_by": "",
        "commercial_authority_reviewed_at": "",
        "commercial_authority_fingerprint": "",
    }
    row.update(changes)
    return row


def test_p5_provenance_laundering_transformation_matrix():
    """P5: Exercise DataFrame copy, concat, merge, drop, rename, dict roundtrip, CSV, JSON.
    Metadata loss must never promote provisional model data to authoritative manual data."""
    base_prov = _provisional_surface()

    # 1. DataFrame copy
    df = pd.DataFrame([base_prov])
    df_copy = df.copy()
    row_copy = df_copy.iloc[0].to_dict()
    assert is_model_surface_row(row_copy) is True
    assert model_surface_authority(row_copy)[0] is False
    assert takeoff_row_publishability(row_copy)[0] is False

    # 2. DataFrame concat
    work_row = {
        "workspace_id": 101,
        "section": "Internal",
        "element": "Walls",
        "location": "L1",
        "substrate": "Plasterboard",
        "finish_system": "Internal acrylic",
        "quantity": 50.0,
        "unit": "m²",
        "quantity_status": "Measured",
        "source_page": "A101",
        "source_reference": "Manual markup",
        "inclusion_status": "INCLUSION",
        "confidence": "Measured",
        "row_role": "work",
    }
    concat_df = pd.concat([pd.DataFrame([base_prov]), pd.DataFrame([work_row])], ignore_index=True)
    concat_prov = concat_df.iloc[0].to_dict()
    assert is_model_surface_row(concat_prov) is True
    assert model_surface_authority(concat_prov)[0] is False
    assert takeoff_row_publishability(concat_prov)[0] is False

    # 3. DataFrame merge
    extra_df = pd.DataFrame([{"location": "mass:1", "extra_annotation": "Note 1"}])
    merged_df = pd.merge(pd.DataFrame([base_prov]), extra_df, on="location")
    merged_prov = merged_df.iloc[0].to_dict()
    assert is_model_surface_row(merged_prov) is True
    assert model_surface_authority(merged_prov)[0] is False
    assert takeoff_row_publishability(merged_prov)[0] is False

    # 4. Drop transformations: losing row_role, source_reference, confidence, authority metadata
    # 4a. Drop row_role from provisional: source_reference & source_page still identify it
    dropped_role = {k: v for k, v in base_prov.items() if k != "row_role"}
    assert is_model_surface_row(dropped_role) is True
    assert model_surface_authority(dropped_role)[0] is False
    assert takeoff_row_publishability(dropped_role)[0] is False

    # 4b. Drop source_reference: row_role still identifies it
    dropped_source_ref = {k: v for k, v in base_prov.items() if k != "source_reference"}
    assert is_model_surface_row(dropped_source_ref) is True
    assert model_surface_authority(dropped_source_ref)[0] is False
    assert takeoff_row_publishability(dropped_source_ref)[0] is False

    # 4c. Drop confidence: still unapproved
    dropped_conf = {k: v for k, v in base_prov.items() if k != "confidence"}
    assert is_model_surface_row(dropped_conf) is True
    assert model_surface_authority(dropped_conf)[0] is False

    # 4d. Drop authority metadata: cannot be promoted
    dropped_auth = {k: v for k, v in base_prov.items() if not k.startswith("commercial_authority")}
    assert is_model_surface_row(dropped_auth) is True
    assert model_surface_authority(dropped_auth)[0] is False
    assert takeoff_row_publishability(dropped_auth)[0] is False

    # 4e. Drop row_role from approved surface: sticky authority markers ensure fingerprint checked and fails closed
    approved = _approved_surface()
    dropped_app_role = {k: v for k, v in approved.items() if k != "row_role"}
    assert is_model_surface_row(dropped_app_role) is True
    assert model_surface_authority(dropped_app_role)[0] is False
    assert "no longer matches" in model_surface_authority(dropped_app_role)[1]

    # 5. Rename transformations
    renamed_role = {("category" if k == "row_role" else k): v for k, v in base_prov.items()}
    assert is_model_surface_row(renamed_role) is True
    assert model_surface_authority(renamed_role)[0] is False
    assert takeoff_row_publishability(renamed_role)[0] is False

    # 6. Dict roundtrip
    dict_rt = dict(base_prov)
    assert is_model_surface_row(dict_rt) is True
    assert model_surface_authority(dict_rt)[0] is False
    assert takeoff_row_publishability(dict_rt)[0] is False

    # 7. CSV serialization roundtrip
    buf = io.StringIO()
    pd.DataFrame([base_prov]).to_csv(buf, index=False)
    buf.seek(0)
    csv_df = pd.read_csv(buf)
    csv_row = csv_df.iloc[0].to_dict()
    assert is_model_surface_row(csv_row) is True
    assert model_surface_authority(csv_row)[0] is False
    assert takeoff_row_publishability(csv_row)[0] is False

    # 8. JSON serialization roundtrip
    json_str = json.dumps(base_prov)
    json_row = json.loads(json_str)
    assert is_model_surface_row(json_row) is True
    assert model_surface_authority(json_row)[0] is False
    assert takeoff_row_publishability(json_row)[0] is False


def test_p5_editor_save_reload_laundering_prevention():
    """P5: Editor save/reload prevents laundering model surface rows or injecting unearned authority."""
    import gc
    import pb_no_ai_takeoff_v1216 as no_ai

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "editor_test.sqlite"
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE takeoff_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id INTEGER NOT NULL,
                    section TEXT, element TEXT, location TEXT, substrate TEXT, finish_system TEXT,
                    quantity REAL DEFAULT 0, unit TEXT, quantity_status TEXT,
                    source_page TEXT, source_reference TEXT, inclusion_status TEXT,
                    coats REAL DEFAULT 2, coverage_m2_per_litre REAL DEFAULT 12,
                    productivity_m2_per_hour REAL DEFAULT 8, rate_per_unit REAL DEFAULT 0,
                    confidence TEXT, notes TEXT, row_role TEXT DEFAULT '',
                    commercial_authority_status TEXT DEFAULT '',
                    commercial_authority_source TEXT DEFAULT '',
                    commercial_authority_reviewed_by TEXT DEFAULT '',
                    commercial_authority_reviewed_at TEXT DEFAULT '',
                    commercial_authority_fingerprint TEXT DEFAULT '',
                    created_at TEXT, updated_at TEXT
                );
                """
            )

            # 1. Insert an approved 3D model surface row
            approved = _approved_surface(
                id=1,
                workspace_id=10,
                section="External",
                element="3D Front",
                location="mass:1",
                source_reference="PB 3D Surface Editor v1.2.12 · mass:1:front",
            )
            conn.execute(
                """INSERT INTO takeoff_rows(
                    id, workspace_id, section, element, location, substrate, finish_system,
                    quantity, unit, quantity_status, source_page, source_reference, inclusion_status,
                    coats, coverage_m2_per_litre, productivity_m2_per_hour, rate_per_unit,
                    confidence, notes, row_role, commercial_authority_status, commercial_authority_source,
                    commercial_authority_reviewed_by, commercial_authority_reviewed_at,
                    commercial_authority_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    1, 10, approved.get("section", ""), approved.get("element", ""), approved.get("location", ""),
                    approved.get("substrate", ""), approved.get("finish_system", ""), approved.get("quantity", 0),
                    approved.get("unit", ""), approved.get("quantity_status", ""), approved.get("source_page", ""),
                    approved.get("source_reference", ""), approved.get("inclusion_status", ""),
                    approved.get("coats", 0), approved.get("coverage_m2_per_litre", 0), approved.get("productivity_m2_per_hour", 0),
                    approved.get("rate_per_unit", 0), approved.get("confidence", ""), approved.get("notes", ""),
                    approved.get("row_role", "model_surface"), approved.get("commercial_authority_status", ""),
                    approved.get("commercial_authority_source", ""), approved.get("commercial_authority_reviewed_by", ""),
                    approved.get("commercial_authority_reviewed_at", ""), approved.get("commercial_authority_fingerprint", ""),
                    "2026-09-04T10:00:00", "2026-09-04T10:00:00"
                )
            )

        class DummyApp:
            def local_connect(self):
                return sqlite3.connect(db_path)
            def now_stamp(self):
                return "2026-09-05T00:00:00"

        app = DummyApp()

        # Case A: Editor attempts to change row_role to 'work' and quantity to 999.0
        edited_row = dict(approved)
        edited_row["id"] = 1
        edited_row["row_role"] = "work"
        edited_row["quantity"] = 999.0

        no_ai.save_schedule_batched(app, 10, [edited_row])

        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute("SELECT * FROM takeoff_rows WHERE workspace_id=10").fetchall()]
            assert len(rows) == 1
            saved = rows[0]

        # Laundering prevented: row_role stays model_surface, status reset to REVIEW_REQUIRED, fingerprint cleared
        assert saved["row_role"] == "model_surface"
        assert saved["commercial_authority_status"] == AUTHORITY_REVIEW_REQUIRED
        assert saved["commercial_authority_fingerprint"] == ""
        assert saved["commercial_authority_reviewed_by"] == ""
        assert model_surface_authority(saved)[0] is False
        assert takeoff_row_publishability(saved)[0] is False

        # Case B: Estimator attempts to inject unearned AUTHORITY_APPROVED directly into unapproved row
        unapproved_edit = dict(saved)
        unapproved_edit["commercial_authority_status"] = "APPROVED"
        # without valid signature/fingerprint
        no_ai.save_schedule_batched(app, 10, [unapproved_edit])

        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute("SELECT * FROM takeoff_rows WHERE workspace_id=10").fetchall()]
            assert len(rows) == 1
            saved_after_inject = rows[0]

        assert saved_after_inject["commercial_authority_status"] == AUTHORITY_REVIEW_REQUIRED
        assert saved_after_inject["commercial_authority_fingerprint"] == ""
        assert model_surface_authority(saved_after_inject)[0] is False
        gc.collect()


def test_p5_level_replication_strips_authority():
    """P5: Replicating takeoff rows to another level strips approval and resets quantity to zero."""
    import gc
    from pb_planreader_3d_app import copy_takeoff_rows_to_level
    import pb_planreader_3d_app as app_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "level_test.sqlite"
        orig_db_path = app_mod.DB_PATH
        app_mod.DB_PATH = str(db_path)
        try:
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE takeoff_rows (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        workspace_id INTEGER NOT NULL,
                        section TEXT, element TEXT, location TEXT, substrate TEXT, finish_system TEXT,
                        quantity REAL DEFAULT 0, unit TEXT, quantity_status TEXT,
                        source_page TEXT, source_reference TEXT, inclusion_status TEXT,
                        coats REAL DEFAULT 2, coverage_m2_per_litre REAL DEFAULT 12,
                        productivity_m2_per_hour REAL DEFAULT 8, rate_per_unit REAL DEFAULT 0,
                        confidence TEXT, notes TEXT, row_role TEXT DEFAULT '',
                        commercial_authority_status TEXT DEFAULT '',
                        commercial_authority_source TEXT DEFAULT '',
                        commercial_authority_reviewed_by TEXT DEFAULT '',
                        commercial_authority_reviewed_at TEXT DEFAULT '',
                        commercial_authority_fingerprint TEXT DEFAULT '',
                        created_at TEXT, updated_at TEXT
                    );
                    """
                )
                approved = _approved_surface(id=1, workspace_id=1, location="Level 1 · Front")
                conn.execute(
                    """INSERT INTO takeoff_rows(
                        id, workspace_id, section, element, location, substrate, finish_system,
                        quantity, unit, quantity_status, source_page, source_reference, inclusion_status,
                        coats, coverage_m2_per_litre, productivity_m2_per_hour, rate_per_unit,
                        confidence, notes, row_role, commercial_authority_status, commercial_authority_source,
                        commercial_authority_reviewed_by, commercial_authority_reviewed_at,
                        commercial_authority_fingerprint, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        1, 1, approved.get("section", ""), approved.get("element", ""), approved.get("location", ""),
                        approved.get("substrate", ""), approved.get("finish_system", ""), approved.get("quantity", 12.5),
                        approved.get("unit", "m²"), approved.get("quantity_status", "Measured"), approved.get("source_page", ""),
                        approved.get("source_reference", ""), approved.get("inclusion_status", "INCLUSION"),
                        approved.get("coats", 0), approved.get("coverage_m2_per_litre", 0), approved.get("productivity_m2_per_hour", 0),
                        approved.get("rate_per_unit", 0), approved.get("confidence", "Measured"), approved.get("notes", ""),
                        approved.get("row_role", "model_surface"), approved.get("commercial_authority_status", ""),
                        approved.get("commercial_authority_source", ""), approved.get("commercial_authority_reviewed_by", ""),
                        approved.get("commercial_authority_reviewed_at", ""), approved.get("commercial_authority_fingerprint", ""),
                        "2026-09-04T10:00:00", "2026-09-04T10:00:00"
                    )
                )

            copied_count = copy_takeoff_rows_to_level(1, [1], "Level 2")
            assert copied_count == 1

            with sqlite3.connect(db_path) as conn2:
                conn2.row_factory = sqlite3.Row
                cur = conn2.cursor()
                copied_row = dict(cur.execute("SELECT * FROM takeoff_rows WHERE id=2").fetchone())

            # Level 2 copy must have zero quantity, 'To measure', stripped authority, and fail closed
            assert copied_row["location"] == "Level 2 · Front"
            assert copied_row["quantity"] == 0
            assert copied_row["quantity_status"] == "To measure"
            assert copied_row["row_role"] == "model_surface"
            assert str(copied_row.get("commercial_authority_status") or "") == ""
            assert str(copied_row.get("commercial_authority_fingerprint") or "") == ""
            assert model_surface_authority(copied_row)[0] is False
            assert takeoff_row_publishability(copied_row)[0] is False
        finally:
            app_mod.DB_PATH = orig_db_path
            gc.collect()


def test_p5_merge_rows_with_model_surface_forces_unapproved_model_role():
    """P5: Merging rows where any constituent is a model surface forces unapproved model_surface role."""
    import gc
    import pb_takeoff_review_v1226 as review

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "merge_test.sqlite"
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE takeoff_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id INTEGER NOT NULL,
                    section TEXT, element TEXT, location TEXT, substrate TEXT, finish_system TEXT,
                    quantity REAL DEFAULT 0, unit TEXT, quantity_status TEXT,
                    source_page TEXT, source_reference TEXT, inclusion_status TEXT,
                    coats REAL DEFAULT 2, coverage_m2_per_litre REAL DEFAULT 12,
                    productivity_m2_per_hour REAL DEFAULT 8, rate_per_unit REAL DEFAULT 0,
                    confidence TEXT, notes TEXT, row_role TEXT DEFAULT '',
                    commercial_authority_status TEXT DEFAULT '',
                    commercial_authority_source TEXT DEFAULT '',
                    commercial_authority_reviewed_by TEXT DEFAULT '',
                    commercial_authority_reviewed_at TEXT DEFAULT '',
                    commercial_authority_fingerprint TEXT DEFAULT '',
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE measurement_lines (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER, takeoff_row_id INTEGER);
                CREATE TABLE pages (id INTEGER PRIMARY KEY, workspace_id INTEGER, page_label TEXT, page_type TEXT, image_path TEXT, width_px REAL, height_px REAL, px_per_m REAL, selected INTEGER);
                """
            )
            conn.execute("INSERT INTO pages VALUES(1, 1, 'A201', 'Floor Plan', '', 1000, 1000, 100, 1)")
            # Row 1: Plain manual work row
            conn.execute(
                """INSERT INTO takeoff_rows(id, workspace_id, section, element, location, substrate, finish_system,
                   quantity, unit, quantity_status, source_page, source_reference, inclusion_status, coats,
                   coverage_m2_per_litre, productivity_m2_per_hour, rate_per_unit, confidence, notes, row_role,
                   created_at, updated_at)
                   VALUES(1, 1, 'External', 'Walls', 'Area 1', 'Render', 'Exterior', 20.0, 'm²', 'Measured',
                          'A201', 'ref-1', 'INCLUSION', 3, 12, 8, 35, 'Measured', '', '', 'x', 'x')"""
            )
            # Row 2: Approved 3D model surface row
            approved = _approved_surface(id=2, workspace_id=1, location="Area 2")
            conn.execute(
                """INSERT INTO takeoff_rows(
                    id, workspace_id, section, element, location, substrate, finish_system,
                    quantity, unit, quantity_status, source_page, source_reference, inclusion_status,
                    coats, coverage_m2_per_litre, productivity_m2_per_hour, rate_per_unit,
                    confidence, notes, row_role, commercial_authority_status, commercial_authority_source,
                    commercial_authority_reviewed_by, commercial_authority_reviewed_at,
                    commercial_authority_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    2, 1, approved.get("section", ""), approved.get("element", ""), approved.get("location", ""),
                    approved.get("substrate", ""), approved.get("finish_system", ""), approved.get("quantity", 30.0),
                    approved.get("unit", "m²"), approved.get("quantity_status", "Measured"), approved.get("source_page", ""),
                    approved.get("source_reference", ""), approved.get("inclusion_status", "INCLUSION"),
                    approved.get("coats", 0), approved.get("coverage_m2_per_litre", 0), approved.get("productivity_m2_per_hour", 0),
                    approved.get("rate_per_unit", 0), approved.get("confidence", "Measured"), approved.get("notes", ""),
                    approved.get("row_role", "model_surface"), approved.get("commercial_authority_status", ""),
                    approved.get("commercial_authority_source", ""), approved.get("commercial_authority_reviewed_by", ""),
                    approved.get("commercial_authority_reviewed_at", ""), approved.get("commercial_authority_fingerprint", ""),
                    "2026-09-04T10:00:00", "2026-09-04T10:00:00"
                )
            )

        class DummyApp:
            def __init__(self, path):
                self.path = path
                self.settings = {}
            def local_connect(self):
                return sqlite3.connect(self.path)
            def lquery(self, query, params=()):
                with sqlite3.connect(self.path) as c:
                    c.row_factory = sqlite3.Row
                    return [dict(r) for r in c.execute(query, params).fetchall()]
            def workspace_setting(self, workspace_id, key, default=""):
                return self.settings.get((int(workspace_id), str(key)), default)
            def set_workspace_setting(self, workspace_id, key, value):
                self.settings[(int(workspace_id), str(key))] = value
            def now_stamp(self):
                return "2026-09-05T00:00:00"

        app = DummyApp(db_path)

        # Attemping to merge and declare row_role='' (plain work)
        new_id = review.merge_rows(
            app, 1, [1, 2],
            {"section": "External", "element": "Walls", "location": "Merged Area", "substrate": "Render", "row_role": ""}
        )

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            merged = dict(conn.execute("SELECT * FROM takeoff_rows WHERE id=?", (new_id,)).fetchone())

        # Merged row inherits model_surface role and unapproved status
        assert merged["row_role"] == "model_surface"
        assert merged["commercial_authority_status"] == AUTHORITY_REVIEW_REQUIRED
        assert merged["commercial_authority_fingerprint"] == ""
        assert model_surface_authority(merged)[0] is False
        assert takeoff_row_publishability(merged)[0] is False
        gc.collect()


def test_p5_workspace_clone_and_cross_workspace_replay():
    """P5: Cross-workspace replay or clone of approved/provisional rows fails closed."""
    approved_ws101 = _approved_surface(workspace_id=101)
    assert model_surface_authority(approved_ws101)[0] is True
    assert takeoff_row_publishability(approved_ws101)[0] is True

    # Replay into workspace 202 (same fingerprint, wrong workspace_id)
    replayed_ws202 = {**approved_ws101, "workspace_id": 202}
    allowed, reason = model_surface_authority(replayed_ws202)
    assert allowed is False
    assert "no longer matches" in reason
    assert takeoff_row_publishability(replayed_ws202)[0] is False

    # Provisional row in workspace 101 cloned to workspace 202 remains unapproved
    prov_ws101 = _provisional_surface(workspace_id=101)
    prov_ws202 = {**prov_ws101, "workspace_id": 202}
    assert is_model_surface_row(prov_ws202) is True
    assert model_surface_authority(prov_ws202)[0] is False
    assert takeoff_row_publishability(prov_ws202)[0] is False
