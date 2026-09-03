"""End-to-end A10 rejection observability tests for canonical openings."""
from __future__ import annotations

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


def _project_with_opening(opening: CanonicalOpening) -> CanonicalProject:
    wall = CanonicalWall(
        id="wall-host-e2e",
        name="A10 host wall",
        start_point=Vector2D(x=0.0, y=0.0),
        end_point=Vector2D(x=10.0, y=0.0),
        height_m=3.0,
        thickness_m=0.2,
        openings=[opening],
        provenance=Provenance(
            source_pdf="A10-host.pdf",
            page_number=1,
            drawing_id="A10-HOST",
        ),
    )
    level = CanonicalLevel(
        id="level-e2e",
        name="Ground",
        elevation_m=0.0,
        height_m=3.0,
        walls=[wall],
    )
    building = CanonicalBuilding(id="building-e2e", name="Building", levels=[level])
    return CanonicalProject(
        id="project-e2e",
        name="A10 rejected opening fixture",
        buildings=[building],
        provenance=Provenance(workspace_id="workspace-e2e"),
    )


def _opening(
    *,
    element_id: str,
    offset_m: float,
    sill_m: float,
    width_m: float = 1.0,
    height_m: float = 1.0,
) -> CanonicalOpening:
    return CanonicalOpening(
        id=element_id,
        name=element_id,
        wall_id="wall-host-e2e",
        opening_type="WINDOW",
        offset_along_wall_m=offset_m,
        sill_height_m=sill_m,
        width_m=width_m,
        height_m=height_m,
        provenance=Provenance(
            source_pdf="A10-openings.pdf",
            page_number=2,
            drawing_id="A10-OPENINGS",
            opening_instance_id=element_id,
        ),
    )


class WorkstreamA10RejectedOpeningE2ETests(unittest.TestCase):
    def _rejection_for(self, graph, element_id: str):
        return next(
            (
                item
                for item in graph.rejected_elements
                if item.get("element_id") == element_id
            ),
            None,
        )

    def test_horizontal_overflow_is_observable_through_public_derivation(self):
        opening = _opening(
            element_id="opening-horizontal-overflow",
            offset_m=12.0,
            sill_m=0.0,
            width_m=1.0,
            height_m=2.0,
        )

        graph = derive_3d_scene_provenance(_project_with_opening(opening))

        self.assertIsNone(graph.lookup_by_element_id(opening.id))
        rejected = self._rejection_for(graph, opening.id)
        self.assertIsNotNone(rejected)
        self.assertEqual(rejected["parent_element_id"], "wall-host-e2e")
        self.assertEqual(rejected["provenance"], opening.provenance.to_dict())
        self.assertIn("exceeds wall length", rejected["reason"].lower())
        serialized = graph.to_dict()
        self.assertIn(rejected, serialized["rejected_elements"])
        self.assertNotIn("position_3d", rejected)
        self.assertNotIn("dimensions_3d", rejected)

    def test_vertical_overflow_is_observable_through_public_derivation(self):
        opening = _opening(
            element_id="opening-vertical-overflow",
            offset_m=2.0,
            sill_m=2.5,
            width_m=1.0,
            height_m=1.0,
        )

        graph = derive_3d_scene_provenance(_project_with_opening(opening))

        self.assertIsNone(graph.lookup_by_element_id(opening.id))
        rejected = self._rejection_for(graph, opening.id)
        self.assertIsNotNone(rejected)
        self.assertEqual(rejected["parent_element_id"], "wall-host-e2e")
        self.assertIn("exceeds wall height", rejected["reason"].lower())
        self.assertNotIn("position_3d", rejected)
        self.assertNotIn("dimensions_3d", rejected)

    def test_valid_opening_is_not_rejected_and_keeps_valid_geometry(self):
        opening = _opening(
            element_id="opening-valid-e2e",
            offset_m=2.0,
            sill_m=1.0,
            width_m=1.5,
            height_m=1.0,
        )

        graph = derive_3d_scene_provenance(_project_with_opening(opening))

        node = graph.lookup_by_element_id(opening.id)
        self.assertIsNotNone(node)
        self.assertTrue(node.geometry_valid)
        self.assertEqual(node.parent_element_id, "wall-host-e2e")
        self.assertIsNone(self._rejection_for(graph, opening.id))

    def test_rejected_opening_serialization_is_deterministic(self):
        opening = _opening(
            element_id="opening-deterministic-rejection",
            offset_m=12.0,
            sill_m=0.0,
        )
        project = _project_with_opening(opening)

        first = derive_3d_scene_provenance(project).to_dict()
        second = derive_3d_scene_provenance(project).to_dict()

        self.assertEqual(first, second)
        self.assertEqual(
            first["rejected_elements"],
            second["rejected_elements"],
        )


if __name__ == "__main__":
    unittest.main()
