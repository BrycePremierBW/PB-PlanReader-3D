"""Workstream A10 regression tests for canonical spatial provenance."""
from __future__ import annotations

import sqlite3
import unittest

from pb_3d_spatial_provenance_v179 import derive_3d_scene_provenance
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
    wall: CanonicalWall,
    *,
    elevation_m: float | None = 4.0,
) -> CanonicalProject:
    level = CanonicalLevel(
        id="level-real-1",
        name="Level 1",
        elevation_m=elevation_m,
        height_m=3.0,
        walls=[wall],
    )
    building = CanonicalBuilding(id="building-real-1", name="Building", levels=[level])
    return CanonicalProject(
        id="project-real-1",
        name="Canonical project",
        buildings=[building],
        provenance=Provenance(workspace_id="workspace-real-7"),
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


if __name__ == "__main__":
    unittest.main()
