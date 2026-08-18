# Editable polygon floor m²

PlanReader v1.2.8 upgrades the fast floor-area mapper from rectangles to editable polygons.

- Draw a custom floor area by clicking each corner, then finish the area.
- Existing rectangle areas are automatically represented as four polygon vertices.
- Drag any vertex to reshape the measured area.
- Click a midpoint handle on an edge to insert another vertex.
- Drag inside the polygon to move the complete area.
- Floor m² recalculates from the calibrated page scale using polygon shoelace area.
- Floor-area rows remain `row_role='floor_area'` reference/pricing-basis rows and do not create paint litres or labour by themselves.
