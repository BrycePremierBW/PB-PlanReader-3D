"""Workstream A10 regression tests for canonical spatial provenance."""
from __future__ import annotations

import sqlite3
from typing import Any
import unittest

from pb_3d_spatial_provenance_v179 import (
    derive_3d_scene_provenance,
    _opening_geometry,
)
from pb_canonical_building import (
    CanonicalBuilding,
    CanonicalLevel,
    CanonicalOpening,
    CanonicalProject,
    CanonicalWall,
    Provenance,
    Vector2D,
)


def _canonical_project(
    wall: CanonicalWall | list[CanonicalWall],
    *,
    elevation_m: float | None = 4.0,
    workspace_id: Any = "workspace-real-7",
) -> CanonicalProject:
    walls = [wall] if isinstance(wall, CanonicalWall) else list(wall)
    level = CanonicalLevel(
        id="level-real-1",
        name="Level 1",
        elevation_m=elevation_m,
        height_m=3.0,
        walls=walls,
    )
    building = CanonicalBuilding(id="building-real-1", name="Building", levels=[level])
    return CanonicalProject(
        id="project-real-1",
        name="Canonical project",
        buildings=[building],
        provenance=Provenance(workspace_id=workspace_id),
    )


def _wall(**overrides) -> CanonicalWall:
    values = {
        "id": "wall-real-42",
        "name": "Evidence wall",
        "start_point": Vector2D(x=2.0, y=3.0),
        "end_point": Vector2D(x=8.0, y=3.0),
        "height_m": 3.0,
        "thickness_m": 0.2,
        "provenance": Provenance(
            source_pdf="architectural.pdf",
            page_number=5,
            drawing_id="A-105",
            source_coords={"baseline": [[2.0, 3.0], [8.0, 3.0]]},
            producer_module="canonical-adapter",
            producer_version="1.0",
        ),
    }
    values.update(overrides)
    return CanonicalWall(**values)


class WorkstreamA10ProvenanceTests(unittest.TestCase):
    def test_complete_geometry_and_identity_come_from_canonical_scene(self):
        wall = _wall()
        graph = derive_3d_scene_provenance(_canonical_project(wall))

        node = graph.lookup_by_element_id(wall.id)
        self.assertIsNotNone(node)
        self.assertEqual(node.element_id, wall.id)
        self.assertEqual(node.element_type, "WALL")
        self.assertTrue(node.geometry_valid)
        self.assertEqual(node.position_3d, (5.0, 3.0, 5.5))
        self.assertEqual(node.dimensions_3d, (6.0, 0.2, 3.0))
        self.assertEqual(node.provenance, wall.provenance.to_dict())
        self.assertEqual(graph.project_id, "project-real-1")
        self.assertEqual(graph.workspace_id, "workspace-real-7")
        self.assertEqual(graph.source_status, "CANONICAL_SCENE")

        serialized = graph.to_dict()
        self.assertNotIn("provenance_hash", repr(serialized))
        self.assertNotIn("takeoff_row_id", repr(serialized))
        self.assertNotIn("measurement_line_id", repr(serialized))
        self.assertNotIn("sheet_number", repr(serialized))

    def test_missing_level_elevation_stays_explicitly_unavailable(self):
        wall = _wall()
        graph = derive_3d_scene_provenance(
            _canonical_project(wall, elevation_m=None)
        )

        node = graph.lookup_by_element_id(wall.id)
        self.assertIsNotNone(node)
        self.assertFalse(node.geometry_valid)
        self.assertIsNone(node.position_3d)
        self.assertIsNone(node.dimensions_3d)
        self.assertIn("level elevation", node.geometry_error)
        self.assertEqual(node.provenance, wall.provenance.to_dict())

    def test_missing_thickness_and_zero_length_are_never_completed(self):
        missing_thickness = _wall(id="wall-no-thickness", thickness_m=None)
        missing_graph = derive_3d_scene_provenance(_canonical_project(missing_thickness))
        missing_node = missing_graph.lookup_by_element_id("wall-no-thickness")

        self.assertIsNotNone(missing_node)
        self.assertFalse(missing_node.geometry_valid)
        self.assertIsNone(missing_node.position_3d)
        self.assertIsNone(missing_node.dimensions_3d)
        self.assertIn("thickness", missing_node.geometry_error)

        zero_length = _wall(
            id="wall-zero-length",
            end_point=Vector2D(x=2.0, y=3.0),
        )
        zero_graph = derive_3d_scene_provenance(_canonical_project(zero_length))
        self.assertIsNone(zero_graph.lookup_by_element_id("wall-zero-length"))

    def test_opening_depth_comes_only_from_canonical_host_wall(self):
        opening = CanonicalOpening(
            id="opening-real-9",
            name="Door D09",
            wall_id="wall-real-42",
            opening_type="DOOR",
            offset_along_wall_m=1.0,
            sill_height_m=0.0,
            width_m=1.0,
            height_m=2.0,
            provenance=Provenance(
                source_pdf="door-schedule.pdf",
                page_number=2,
                drawing_id="D-002",
                opening_instance_id="opening-real-9",
            ),
        )
        wall = _wall(openings=[opening])
        graph = derive_3d_scene_provenance(_canonical_project(wall))

        node = graph.lookup_by_element_id(opening.id)
        self.assertIsNotNone(node)
        self.assertEqual(node.parent_element_id, wall.id)
        self.assertTrue(node.geometry_valid)
        self.assertEqual(node.position_3d, (3.5, 3.0, 5.0))
        self.assertEqual(node.dimensions_3d, (1.0, 0.2, 2.0))
        self.assertEqual(node.provenance, opening.provenance.to_dict())

        wall.thickness_m = None
        incomplete = derive_3d_scene_provenance(_canonical_project(wall))
        incomplete_node = incomplete.lookup_by_element_id(opening.id)
        self.assertIsNotNone(incomplete_node)
        self.assertFalse(incomplete_node.geometry_valid)
        self.assertIsNone(incomplete_node.position_3d)
        self.assertIsNone(incomplete_node.dimensions_3d)

    def test_raw_takeoff_database_is_ignored_without_even_opening_a_cursor(self):
        class RawDatabaseTrap:
            def cursor(self):
                raise AssertionError("A10 must never query raw workspace tables")

        graph = derive_3d_scene_provenance(RawDatabaseTrap(), workspace_id=7)
        self.assertEqual(graph.nodes, [])
        self.assertEqual(graph.source_status, "CANONICAL_PROJECT_REQUIRED")
        self.assertIsNone(graph.project_id)
        self.assertIsNone(graph.workspace_id)

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE takeoff_rows "
                "(id INTEGER, workspace_id INTEGER, element TEXT, quantity REAL)"
            )
            conn.execute(
                "INSERT INTO takeoff_rows VALUES (100, 7, 'Wall', 50.0)"
            )
            conn.commit()
            legacy_graph = derive_3d_scene_provenance(conn, 7)
            self.assertEqual(legacy_graph.nodes, [])
            self.assertEqual(
                legacy_graph.source_status,
                "CANONICAL_PROJECT_REQUIRED",
            )
        finally:
            conn.close()

    def test_derivation_is_deterministic_and_does_not_mutate_authority(self):
        opening = CanonicalOpening(
            id="opening-real-10",
            wall_id="wall-real-42",
            opening_type="WINDOW",
            offset_along_wall_m=2.0,
            sill_height_m=1.0,
            width_m=1.5,
            height_m=1.0,
            deduction_authority=True,
        )
        wall = _wall(
            openings=[opening],
            takeoff_eligible=True,
            deduction_authority=True,
        )
        project = _canonical_project(wall)
        before = project.to_json()

        first = derive_3d_scene_provenance(project).to_dict()
        second = derive_3d_scene_provenance(project).to_dict()

        self.assertEqual(first, second)
        self.assertEqual(project.to_json(), before)
        self.assertTrue(wall.takeoff_eligible)
        self.assertTrue(wall.deduction_authority)
        self.assertTrue(opening.deduction_authority)

    def test_production_adapter_identity_and_provenance_survive(self):
        from pb_production_3d_adapter import planreader_to_canonical_model

        payload = {
            "project_id": "production-project",
            "project_name": "Production path fixture",
            "is_synthetic_demo": False,
            "levels": [
                {
                    "id": "production-level",
                    "name": "Ground",
                    "elevation_m": 0.0,
                    "height_m": 3.2,
                    "level_index": 0,
                }
            ],
            "walls": [
                {
                    "wall_ref": "production-wall",
                    "name": "South wall",
                    "level_id": "production-level",
                    "a": {"x": 1.0, "y": 2.0},
                    "b": {"x": 5.0, "y": 2.0},
                    "height_m": 3.2,
                    "height_status": "confirmed",
                    "thickness_m": 0.23,
                    "provenance": {
                        "source_pdf": "A101.pdf",
                        "page_number": 3,
                        "drawing_id": "A101",
                    },
                }
            ],
        }
        project, _ = planreader_to_canonical_model(
            payload,
            is_validated_internal_workspace=True,
        )

        canonical_wall = project.buildings[0].levels[0].walls[0]
        graph = derive_3d_scene_provenance(project)
        node = graph.lookup_by_element_id(canonical_wall.id)

        self.assertIsNotNone(node)
        self.assertTrue(node.geometry_valid)
        self.assertEqual(node.element_id, canonical_wall.id)
        self.assertEqual(canonical_wall.provenance.wall_ref, "production-wall")
        self.assertEqual(node.position_3d, (3.0, 2.0, 1.6))
        self.assertEqual(node.dimensions_3d, (4.0, 0.23, 3.2))
        self.assertEqual(node.provenance["source_pdf"], "A101.pdf")
        self.assertEqual(node.provenance["drawing_id"], "A101")

    def test_synthetic_demo_project_is_rejected(self):
        project = _canonical_project(_wall())
        project.is_synthetic_demo = True

        graph = derive_3d_scene_provenance(project)

        self.assertEqual(graph.nodes, [])
        self.assertEqual(
            graph.source_status,
            "SYNTHETIC_CANONICAL_PROJECT_REJECTED",
        )

    def test_legacy_non_canonical_caller_fails_explicitly(self):
        """Non-canonical inputs must fail with explicit unambiguous status."""
        # Test with various non-canonical inputs
        non_canonical_inputs = [
            None,
            "string_input",
            123,
            {"dict": "input"},
            sqlite3.connect(":memory:"),
        ]

        for input_obj in non_canonical_inputs:
            graph = derive_3d_scene_provenance(input_obj)
            # Must not be mistaken for a successful empty canonical project
            self.assertEqual(graph.nodes, [])
            self.assertEqual(graph.source_status, "CANONICAL_PROJECT_REQUIRED")
            self.assertIsNone(graph.project_id)
            self.assertIsNone(graph.workspace_id)
            # No duplicate conflicts tracking for non-canonical inputs
            self.assertEqual(graph.duplicate_id_conflicts, [])

        # Verify the status is distinguishable from successful empty canonical project
        empty_canonical = _canonical_project(_wall(id="wall-1"))
        # Remove the wall to create an empty but valid canonical project
        empty_canonical.buildings[0].levels[0].walls = []
        empty_graph = derive_3d_scene_provenance(empty_canonical)
        
        # Empty canonical project should have CANONICAL_SCENE status
        self.assertEqual(empty_graph.source_status, "CANONICAL_SCENE")
        self.assertNotEqual(empty_graph.source_status, "CANONICAL_PROJECT_REQUIRED")
        # Empty canonical project has no duplicate conflicts
        self.assertEqual(empty_graph.duplicate_id_conflicts, [])

    def test_opening_outside_host_geometry_fails_closed(self):
        """Opening physically outside host wall must not receive valid geometry."""
        # Test horizontal overflow - opening extends beyond host wall (via internal helper)
        wall_obj = {
            "type": "WALL",
            "id": "wall-host-1",
            "start_point": {"x": 0.0, "y": 0.0},
            "end_point": {"x": 10.0, "y": 0.0},
            "height_m": 3.0,
            "thickness_m": 0.2,
        }
        
        opening_outside = {
            "type": "DOOR",
            "id": "opening-outside-1",
            "wall_id": "wall-host-1",
            "is_host_attached": True,
            "offset_along_wall_m": 12.0,  # Beyond wall length of 10
            "sill_height_m": 0.0,
            "width_m": 1.0,
            "height_m": 2.0,
        }
        
        objects_by_id = {"wall-host-1": wall_obj}
        geometry_valid, position, dimensions, error = _opening_geometry(
            opening_outside, 0.0, objects_by_id
        )
        
        self.assertFalse(geometry_valid)
        self.assertIsNone(position)
        self.assertIsNone(dimensions)
        self.assertIn("extends beyond", error.lower())
        
        # Test vertical overflow - opening taller than host wall (via internal helper)
        opening_overflow = {
            "type": "WINDOW",
            "id": "opening-overflow-1",
            "wall_id": "wall-host-1",
            "is_host_attached": True,
            "offset_along_wall_m": 2.0,
            "sill_height_m": 0.0,
            "width_m": 1.0,
            "height_m": 5.0,  # Taller than wall height of 3.0
        }
        
        geometry_valid_overflow, position_overflow, dimensions_overflow, error_overflow = _opening_geometry(
            opening_overflow, 0.0, objects_by_id
        )
        
        self.assertFalse(geometry_valid_overflow)
        self.assertIsNone(position_overflow)
        self.assertIsNone(dimensions_overflow)
        self.assertIn("height", error_overflow.lower())
        
        # Test end-to-end with a CanonicalProject that has missing required geometry
        # to ensure A10's own geometry validation works through the full path
        wall_missing_thickness = _wall(
            id="wall-missing-thickness",
            start_point=Vector2D(x=0.0, y=0.0),
            end_point=Vector2D(x=10.0, y=0.0),
            height_m=3.0,
            thickness_m=None,  # Missing required thickness
        )
        
        graph_missing = derive_3d_scene_provenance(_canonical_project(wall_missing_thickness))
        node_missing = graph_missing.lookup_by_element_id("wall-missing-thickness")
        
        # The wall should be present but marked as invalid geometry
        self.assertIsNotNone(node_missing)
        self.assertFalse(node_missing.geometry_valid)
        self.assertIsNone(node_missing.position_3d)
        self.assertIsNone(node_missing.dimensions_3d)
        self.assertIn("thickness", node_missing.geometry_error.lower())

    def test_duplicate_canonical_ids_are_explicitly_excluded(self):
        """Duplicate canonical IDs must not create ambiguous mappings."""
        # Create two walls with the same ID
        wall1 = _wall(
            id="duplicate-wall-id",
            start_point=Vector2D(x=0.0, y=0.0),
            end_point=Vector2D(x=5.0, y=0.0),
        )
        wall2 = _wall(
            id="duplicate-wall-id",  # Same ID
            start_point=Vector2D(x=10.0, y=0.0),
            end_point=Vector2D(x=15.0, y=0.0),
        )
        
        # Create a level with both walls (same ID)
        level = CanonicalLevel(
            id="level-dup-test",
            name="Level Dup Test",
            elevation_m=0.0,
            height_m=3.0,
            walls=[wall1, wall2],
        )
        building = CanonicalBuilding(id="building-dup", name="Building Dup", levels=[level])
        project = CanonicalProject(
            id="project-dup",
            name="Duplicate ID Project",
            buildings=[building],
            provenance=Provenance(workspace_id="workspace-dup"),
        )
        
        graph = derive_3d_scene_provenance(project)
        
        # The duplicate ID should not be in the graph (excluded by id_counts check)
        duplicate_node = graph.lookup_by_element_id("duplicate-wall-id")
        self.assertIsNone(duplicate_node)
        
        # The graph should still be valid (not an error state)
        self.assertEqual(graph.source_status, "CANONICAL_SCENE")
        
        # CRITICAL: The duplicate conflict must be explicitly observable
        self.assertIsNotNone(graph.duplicate_id_conflicts)
        self.assertIn("duplicate-wall-id", graph.duplicate_id_conflicts)
        
        # Verify that other valid elements still work
        # Add a wall with unique ID to ensure the graph isn't completely broken
        wall_unique = _wall(
            id="unique-wall-id",
            start_point=Vector2D(x=20.0, y=0.0),
            end_point=Vector2D(x=25.0, y=0.0),
        )
        level_unique = CanonicalLevel(
            id="level-unique",
            name="Level Unique",
            elevation_m=0.0,
            height_m=3.0,
            walls=[wall_unique],
        )
        building_unique = CanonicalBuilding(id="building-unique", name="Building Unique", levels=[level_unique])
        project_unique = CanonicalProject(
            id="project-unique",
            name="Unique ID Project",
            buildings=[building_unique],
            provenance=Provenance(workspace_id="workspace-unique"),
        )
        
        graph_unique = derive_3d_scene_provenance(project_unique)
        unique_node = graph_unique.lookup_by_element_id("unique-wall-id")
        
        self.assertIsNotNone(unique_node)
        self.assertTrue(unique_node.geometry_valid)
        # Unique ID project should have no duplicate conflicts
        self.assertEqual(graph_unique.duplicate_id_conflicts, [])

    def test_p6_workspace_identity_and_binding_matrix(self):
        """P6 Audit: Workspace binding validation across caller, project, and elements."""
        wall = _wall()
        proj = _canonical_project(wall, workspace_id="workspace-real-7")

        # 1. Caller provides mismatching workspace -> fails closed with WORKSPACE_MISMATCH
        mismatch_graph = derive_3d_scene_provenance(proj, workspace_id="workspace-other-99")
        self.assertEqual(mismatch_graph.nodes, [])
        self.assertEqual(mismatch_graph.source_status, "WORKSPACE_MISMATCH")

        # 2. Positive int workspace_id is normalized to string representation
        proj_int = _canonical_project(wall, workspace_id=7)
        int_graph = derive_3d_scene_provenance(proj_int)
        self.assertEqual(int_graph.workspace_id, "7")
        self.assertEqual(int_graph.source_status, "CANONICAL_SCENE")

        # 3. Non-positive and boolean workspace identities are rejected
        for bad_ws in [0, -1, True, False, "nan", "none", "null", "   "]:
            proj_bad = _canonical_project(wall, workspace_id=bad_ws)
            bad_graph = derive_3d_scene_provenance(proj_bad)
            self.assertIsNone(bad_graph.workspace_id)

        # 4. Caller matching integer workspace passes
        matching_graph = derive_3d_scene_provenance(proj_int, workspace_id=7)
        self.assertEqual(matching_graph.workspace_id, "7")
        self.assertEqual(matching_graph.source_status, "CANONICAL_SCENE")

        # 5. Element provenance containing a conflicting workspace ID must fail closed
        wall_cross = _wall(
            id="wall-cross-ws",
            provenance=Provenance(
                workspace_id="workspace-hacked-99",
                source_pdf="architectural.pdf",
                page_number=5,
                drawing_id="A-105",
            ),
        )
        cross_proj = _canonical_project(wall_cross, workspace_id="workspace-real-7")
        cross_graph = derive_3d_scene_provenance(cross_proj)
        cross_node = cross_graph.lookup_by_element_id("wall-cross-ws")
        self.assertIsNotNone(cross_node)
        self.assertFalse(cross_node.geometry_valid)
        self.assertIsNone(cross_node.position_3d)
        self.assertIsNone(cross_node.dimensions_3d)
        self.assertIn("workspace", cross_node.geometry_error.lower())

    def test_p6_page_reference_validation_matrix(self):
        """P6 Audit: Invalid, non-finite, negative, or sentinel page references fail closed."""
        invalid_pages = [-1, 0, float("nan"), float("inf"), float("-inf"), True, False, "nan", "none", "null"]
        for bad_page in invalid_pages:
            wall = _wall(
                id=f"wall-bad-page-{bad_page}",
                provenance=Provenance(
                    source_pdf="architectural.pdf",
                    page_number=bad_page,
                    drawing_id="A-105",
                ),
            )
            graph = derive_3d_scene_provenance(_canonical_project(wall))
            node = graph.lookup_by_element_id(wall.id)
            self.assertIsNotNone(node)
            self.assertFalse(node.geometry_valid, f"Page value {bad_page!r} should be invalid")
            self.assertIsNone(node.position_3d)
            self.assertIsNone(node.dimensions_3d)
            self.assertIn("page", node.geometry_error.lower())

        # Sentinel page_id fails closed
        wall_sent_page_id = _wall(
            id="wall-sent-page-id",
            provenance=Provenance(
                source_pdf="architectural.pdf",
                page_number=5,
                page_id="nan",
                drawing_id="A-105",
            ),
        )
        graph_sent = derive_3d_scene_provenance(_canonical_project(wall_sent_page_id))
        node_sent = graph_sent.lookup_by_element_id(wall_sent_page_id.id)
        self.assertIsNotNone(node_sent)
        self.assertFalse(node_sent.geometry_valid)
        self.assertIn("sentinel", node_sent.geometry_error.lower())

    def test_p6_drawing_revision_superseded_rejected(self):
        """P6 Audit: Superseded or unapproved drawing revisions fail closed."""
        # 1. Explicitly superseded revision via metadata / provenance
        wall_super = _wall(
            id="wall-superseded",
            provenance=Provenance(
                source_pdf="architectural.pdf",
                page_number=5,
                drawing_id="A-105",
            ),
        )
        # Provenance marking as superseded
        setattr(wall_super.provenance, "is_superseded", True)
        graph = derive_3d_scene_provenance(_canonical_project(wall_super))
        node = graph.lookup_by_element_id("wall-superseded")
        self.assertIsNotNone(node)
        self.assertFalse(node.geometry_valid)
        self.assertIn("superseded", node.geometry_error.lower())

        # 2. Sentinel drawing_id
        wall_sent_drawing = _wall(
            id="wall-sent-drawing",
            provenance=Provenance(
                source_pdf="architectural.pdf",
                page_number=5,
                drawing_id="none",
            ),
        )
        graph_sent = derive_3d_scene_provenance(_canonical_project(wall_sent_drawing))
        node_sent = graph_sent.lookup_by_element_id(wall_sent_drawing.id)
        self.assertIsNotNone(node_sent)
        self.assertFalse(node_sent.geometry_valid)
        self.assertIn("sentinel", node_sent.geometry_error.lower())

    def test_p6_parent_relationship_level_and_geometry_consistency(self):
        """P6 Audit: Cross-level opening attachment and invalid host wall fail closed."""
        # Opening level_id does not match host wall level_id
        wall_obj = {
            "type": "WALL",
            "id": "wall-host-p6",
            "level_id": "level-1",
            "start_point": {"x": 0.0, "y": 0.0},
            "end_point": {"x": 10.0, "y": 0.0},
            "height_m": 3.0,
            "thickness_m": 0.2,
        }
        op_cross_level = {
            "type": "DOOR",
            "id": "opening-cross-level",
            "level_id": "level-2",  # Mismatch with wall-host-p6 (level-1)
            "wall_id": "wall-host-p6",
            "is_host_attached": True,
            "offset_along_wall_m": 1.0,
            "sill_height_m": 0.0,
            "width_m": 1.0,
            "height_m": 2.0,
        }
        objects_by_id = {"wall-host-p6": wall_obj}
        valid, pos, dims, err = _opening_geometry(op_cross_level, 4.0, objects_by_id)
        self.assertFalse(valid)
        self.assertIsNone(pos)
        self.assertIsNone(dims)
        self.assertIn("level", err.lower())

    def test_p6_copied_and_duplicate_spatial_geometry(self):
        """P6 Audit: Cloned/copied spatial geometry on same level fails closed to prevent double counting."""
        wall_orig = _wall(
            id="wall-orig-1",
            start_point=Vector2D(x=0.0, y=0.0),
            end_point=Vector2D(x=6.0, y=0.0),
        )
        wall_clone = _wall(
            id="wall-clone-2",
            start_point=Vector2D(x=0.0, y=0.0),
            end_point=Vector2D(x=6.0, y=0.0),
        )
        level = CanonicalLevel(
            id="level-dup-geom",
            name="Level Dup Geom",
            elevation_m=0.0,
            height_m=3.0,
            walls=[wall_orig, wall_clone],
        )
        building = CanonicalBuilding(id="bld-dup-geom", name="Bld", levels=[level])
        project = CanonicalProject(
            id="proj-dup-geom",
            name="Duplicate Geometry Project",
            buildings=[building],
            provenance=Provenance(workspace_id="workspace-dup-geom"),
        )
        graph = derive_3d_scene_provenance(project)
        # Both walls with identical spatial coordinates should have invalid geometry to prevent double counting
        node_orig = graph.lookup_by_element_id("wall-orig-1")
        node_clone = graph.lookup_by_element_id("wall-clone-2")
        self.assertIsNotNone(node_orig)
        self.assertIsNotNone(node_clone)
        self.assertFalse(node_orig.geometry_valid)
        self.assertFalse(node_clone.geometry_valid)
        self.assertIn("duplicate", node_orig.geometry_error.lower())
        self.assertIn("duplicate", node_clone.geometry_error.lower())

    def test_p6_stale_scene_fail_closed(self):
        """P6 Audit: Stale canonical projects or revisions fail closed with 0 valid nodes."""
        wall = _wall()

        # 1. Project with is_stale=True
        proj_stale = _canonical_project(wall)
        proj_stale.is_stale = True
        g1 = derive_3d_scene_provenance(proj_stale)
        self.assertEqual(g1.nodes, [])
        self.assertEqual(g1.source_status, "STALE_CANONICAL_SCENE_REJECTED")

        # 2. Project provenance with is_stale=True
        proj_prov_stale = _canonical_project(wall)
        setattr(proj_prov_stale.provenance, "is_stale", True)
        g2 = derive_3d_scene_provenance(proj_prov_stale)
        self.assertEqual(g2.nodes, [])
        self.assertEqual(g2.source_status, "STALE_CANONICAL_SCENE_REJECTED")

        # 3. Project with source_status='STALE'
        proj_status_stale = _canonical_project(wall)
        setattr(proj_status_stale, "source_status", "STALE")
        g3 = derive_3d_scene_provenance(proj_status_stale)
        self.assertEqual(g3.nodes, [])
        self.assertEqual(g3.source_status, "STALE_CANONICAL_SCENE_REJECTED")

        # 4. Project with review_state='SUPERSEDED'
        proj_super = _canonical_project(wall)
        setattr(proj_super, "review_state", "SUPERSEDED")
        g4 = derive_3d_scene_provenance(proj_super)
        self.assertEqual(g4.nodes, [])
        self.assertEqual(g4.source_status, "STALE_CANONICAL_SCENE_REJECTED")


if __name__ == "__main__":
    unittest.main()
