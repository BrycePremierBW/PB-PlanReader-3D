"""
PlanReader Synthetic 3D Viewer Demonstration Fixture.

FIXTURE LABEL: SYNTHETIC VIEWER DEMONSTRATION — NOT BENCHMARK TRUTH

This module provides a realistic 3-level commercial/residential building model
containing external walls, internal walls, doors, windows, balconies, soffits,
parapets, columns, roofs, and varied substrate/finish assignments across
CONFIRMED, INFERRED, and REVIEW_REQUIRED review states.

FOR ARCHITECTURE AND RENDERING TESTING ONLY. NOT TAKEOFF AUTHORITATIVE.
"""

from pb_canonical_building import (
    CanonicalProject,
    CanonicalBuilding,
    CanonicalLevel,
    CanonicalWall,
    CanonicalOpening,
    CanonicalSpace,
    CanonicalFloor,
    CanonicalCeiling,
    CanonicalRoof,
    CanonicalSoffit,
    CanonicalBalcony,
    CanonicalParapet,
    CanonicalColumn,
    CanonicalBalustrade,
    CanonicalScreen,
    Vector2D,
    Provenance,
    ReviewState,
)


def get_synthetic_viewer_demo_model() -> CanonicalProject:
    """
    Constructs a synthetic 3-level building model fixture for testing
    rendering fidelity, level isolation, object selection, and evidence panels.

    Fixture Identifier: SYNTHETIC VIEWER DEMONSTRATION — NOT BENCHMARK TRUTH
    """
    project = CanonicalProject(
        id="proj_synth_001",
        name="SYNTHETIC VIEWER DEMONSTRATION — NOT BENCHMARK TRUTH",
        is_synthetic_demo=True,
        confidence=1.0,
        review_state=ReviewState.CONFIRMED,
        takeoff_eligible=False,      # Demo fixture is NOT takeoff authoritative
        deduction_authority=False,   # Demo fixture is NOT takeoff authoritative
        metadata={
            "disclaimer": "SYNTHETIC VIEWER DEMONSTRATION — NOT BENCHMARK TRUTH",
            "purpose": "Architecture & 3D Viewer Verification Only",
        },
    )

    building = CanonicalBuilding(
        id="bld_main",
        name="Main Demonstration Building",
        parent_id=project.id,
        substrate="Mixed Structure",
        finish="Architectural Paint & Render",
    )

    # ----------------------------------------------------
    # LEVEL 0: GROUND FLOOR (FFL 0.0m, height 3.0m)
    # ----------------------------------------------------
    lvl0 = CanonicalLevel(
        id="lvl_0",
        name="Ground Floor",
        elevation_m=0.0,
        height_m=3.0,
        level_index=0,
        confidence=1.0,
        review_state=ReviewState.CONFIRMED,
        provenance=Provenance(
            source_pdf="Architectural_Set_RevB.pdf",
            page_number=3,
            drawing_id="A101",
            scale_source="1:100 Graphic Scale",
            contributing_evidence=["A101 Floor Plan Ground", "SEC-01 Cross Section"],
        ),
    )

    prov_lvl0_ext = Provenance(source_pdf="Architectural_Set_RevB.pdf", page_number=3, drawing_id="A101")
    
    wall_g1 = CanonicalWall(
        id="wall_g_ext_south",
        name="Ground South External Wall",
        level_id=lvl0.id,
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(12.0, 0.0),
        thickness_m=0.23,
        height_m=3.0,
        is_external=True,
        substrate="Rendered Masonry",
        finish="Taubmans Weatherbeater Acrylic - Lexicon Half",
        confidence=1.0,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl0_ext,
    )
    d01 = CanonicalOpening(
        id="door_d01",
        name="Main Entry Door (D01)",
        wall_id=wall_g1.id,
        level_id=lvl0.id,
        opening_type="DOOR",
        offset_along_wall_m=2.0,
        sill_height_m=0.0,
        width_m=1.2,
        height_m=2.3,
        mark="D01",
        substrate="Solid Timber Core",
        finish="Gloss Clear Varnish",
        confidence=1.0,
        review_state=ReviewState.CONFIRMED,
        provenance=Provenance(source_pdf="Architectural_Set_RevB.pdf", page_number=8, drawing_id="SCH-01"),
    )
    w01 = CanonicalOpening(
        id="win_w01",
        name="Living Room Front Window (W01)",
        wall_id=wall_g1.id,
        level_id=lvl0.id,
        opening_type="WINDOW",
        offset_along_wall_m=6.5,
        sill_height_m=0.9,
        width_m=2.4,
        height_m=1.5,
        mark="W01",
        substrate="Powdercoated Aluminium Frame & Clear Float Glass",
        finish="Monument Satin",
        confidence=1.0,
        review_state=ReviewState.CONFIRMED,
        provenance=Provenance(source_pdf="Architectural_Set_RevB.pdf", page_number=8, drawing_id="SCH-02"),
    )
    wall_g1.openings = [d01, w01]

    wall_g2 = CanonicalWall(
        id="wall_g_ext_east",
        name="Ground East External Wall",
        level_id=lvl0.id,
        start_point=Vector2D(12.0, 0.0),
        end_point=Vector2D(12.0, 8.0),
        thickness_m=0.23,
        height_m=3.0,
        is_external=True,
        substrate="Rendered Masonry",
        finish="Taubmans Weatherbeater Acrylic - Lexicon Half",
        confidence=1.0,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl0_ext,
    )
    w02 = CanonicalOpening(
        id="win_w02",
        name="Dining Window (W02)",
        wall_id=wall_g2.id,
        level_id=lvl0.id,
        opening_type="WINDOW",
        offset_along_wall_m=2.5,
        sill_height_m=0.9,
        width_m=1.8,
        height_m=1.5,
        mark="W02",
        substrate="Aluminium Glass",
        finish="Monument Satin",
        confidence=0.95,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl0_ext,
    )
    wall_g2.openings = [w02]

    wall_g3 = CanonicalWall(
        id="wall_g_ext_north",
        name="Ground North External Wall",
        level_id=lvl0.id,
        start_point=Vector2D(12.0, 8.0),
        end_point=Vector2D(0.0, 8.0),
        thickness_m=0.23,
        height_m=3.0,
        is_external=True,
        substrate="Fibre Cement Sheet Cladding",
        finish="Haymes Solashield Low Sheen - Charcoal",
        confidence=1.0,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl0_ext,
    )
    d02 = CanonicalOpening(
        id="door_d02_sliding",
        name="Rear Alfresco Sliding Door (D02)",
        wall_id=wall_g3.id,
        level_id=lvl0.id,
        opening_type="DOOR",
        offset_along_wall_m=4.0,
        sill_height_m=0.0,
        width_m=3.0,
        height_m=2.4,
        mark="D02",
        substrate="Double Glazed Aluminium Slider",
        finish="Anodised Black",
        confidence=1.0,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl0_ext,
    )
    wall_g3.openings = [d02]

    wall_g4 = CanonicalWall(
        id="wall_g_ext_west",
        name="Ground West External Wall",
        level_id=lvl0.id,
        start_point=Vector2D(0.0, 8.0),
        end_point=Vector2D(0.0, 0.0),
        thickness_m=0.23,
        height_m=3.0,
        is_external=True,
        substrate="Rendered Masonry",
        finish="Taubmans Weatherbeater Acrylic - Lexicon Half",
        confidence=1.0,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl0_ext,
    )

    wall_g_int1 = CanonicalWall(
        id="wall_g_int_hall",
        name="Ground Entry Hallway Spine Wall",
        level_id=lvl0.id,
        start_point=Vector2D(4.5, 0.0),
        end_point=Vector2D(4.5, 5.5),
        thickness_m=0.11,
        height_m=2.7,
        is_external=False,
        substrate="Plasterboard on Stud Framework",
        finish="Haymes Expressions Interior Washable - Natural White",
        confidence=0.95,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl0_ext,
    )
    d03 = CanonicalOpening(
        id="door_d03_int",
        name="Internal Living Room Passage Door (D03)",
        wall_id=wall_g_int1.id,
        level_id=lvl0.id,
        opening_type="DOOR",
        offset_along_wall_m=1.8,
        sill_height_m=0.0,
        width_m=0.82,
        height_m=2.04,
        mark="D03",
        substrate="Hollow Core Flush Panel Timber",
        finish="Semi-Gloss Enamel White",
        confidence=0.95,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl0_ext,
    )
    wall_g_int1.openings = [d03]

    floor_g = CanonicalFloor(
        id="floor_g0",
        name="Ground Concrete Floor Slab",
        level_id=lvl0.id,
        polygon=[Vector2D(0, 0), Vector2D(12, 0), Vector2D(12, 8), Vector2D(0, 8)],
        thickness_m=0.15,
        elevation_offset_m=0.0,  # Explicit zero offset known
        substrate="Reinforced Concrete Slab",
        finish="Polished Epoxy Sealant",
        confidence=1.0,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl0_ext,
    )

    space_living = CanonicalSpace(
        id="space_living_g",
        name="Ground Living & Dining Area",
        level_id=lvl0.id,
        room_number="G01",
        boundary_polygon=[Vector2D(4.5, 0), Vector2D(12, 0), Vector2D(12, 8), Vector2D(4.5, 8)],
        height_m=2.7,
        specified_floor_area_m2=60.0,
        substrate="Plasterboard / Timber Flooring",
        finish="Low Sheen Acrylic",
        confidence=0.95,
        review_state=ReviewState.CONFIRMED,
    )

    col_porch = CanonicalColumn(
        id="col_porch_01",
        name="Front Porch Feature Pillar",
        level_id=lvl0.id,
        center=Vector2D(1.0, -1.0),
        width_m=0.4,
        depth_m=0.4,
        height_m=3.0,
        substrate="Off-Form Concrete",
        finish="Clear Protective Sealer",
        confidence=1.0,
        review_state=ReviewState.CONFIRMED,
    )

    lvl0.walls = [wall_g1, wall_g2, wall_g3, wall_g4, wall_g_int1]
    lvl0.floors = [floor_g]
    lvl0.spaces = [space_living]
    lvl0.columns = [col_porch]

    # ----------------------------------------------------
    # LEVEL 1: FIRST FLOOR (FFL 3.0m, height 2.8m)
    # ----------------------------------------------------
    lvl1 = CanonicalLevel(
        id="lvl_1",
        name="Level 1 Upper Storey",
        elevation_m=3.0,
        height_m=2.8,
        level_index=1,
        confidence=0.85,
        review_state=ReviewState.INFERRED,
        provenance=Provenance(
            source_pdf="Architectural_Set_RevB.pdf",
            page_number=4,
            drawing_id="A102",
            scale_source="1:100 Graphic Scale",
            contributing_evidence=["A102 Level 1 Plan", "EL-01 Front Elevation"],
        ),
    )

    prov_lvl1 = Provenance(source_pdf="Architectural_Set_RevB.pdf", page_number=4, drawing_id="A102")

    wall_l1_south = CanonicalWall(
        id="wall_l1_ext_south",
        name="Level 1 South External Wall",
        level_id=lvl1.id,
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(12.0, 0.0),
        thickness_m=0.20,
        height_m=2.8,
        is_external=True,
        substrate="Lightweight Fibre Cement Weatherboard",
        finish="Haymes Solashield - White Duck Half",
        confidence=0.85,
        review_state=ReviewState.INFERRED,
        provenance=prov_lvl1,
    )
    d101 = CanonicalOpening(
        id="door_d101_balcony",
        name="Master Bedroom Balcony Sliding Door (D101)",
        wall_id=wall_l1_south.id,
        level_id=lvl1.id,
        opening_type="DOOR",
        offset_along_wall_m=1.0,
        sill_height_m=0.0,
        width_m=2.1,
        height_m=2.1,
        mark="D101",
        substrate="Aluminium Sliding Glass",
        finish="Anodised Black",
        confidence=0.85,
        review_state=ReviewState.INFERRED,
        provenance=prov_lvl1,
    )
    w101 = CanonicalOpening(
        id="win_w101",
        name="Bedroom 2 Front Window (W101)",
        wall_id=wall_l1_south.id,
        level_id=lvl1.id,
        opening_type="WINDOW",
        offset_along_wall_m=7.0,
        sill_height_m=0.9,
        width_m=1.8,
        height_m=1.4,
        mark="W101",
        substrate="Aluminium Glass",
        finish="Monument Satin",
        confidence=0.85,
        review_state=ReviewState.INFERRED,
        provenance=prov_lvl1,
    )
    wall_l1_south.openings = [d101, w101]

    wall_l1_east = CanonicalWall(
        id="wall_l1_ext_east",
        name="Level 1 East External Wall",
        level_id=lvl1.id,
        start_point=Vector2D(12.0, 0.0),
        end_point=Vector2D(12.0, 8.0),
        thickness_m=0.20,
        height_m=2.8,
        is_external=True,
        substrate="Lightweight FC Cladding",
        finish="Haymes Solashield - White Duck Half",
        confidence=0.85,
        review_state=ReviewState.INFERRED,
        provenance=prov_lvl1,
    )
    wall_l1_north = CanonicalWall(
        id="wall_l1_ext_north",
        name="Level 1 North External Wall",
        level_id=lvl1.id,
        start_point=Vector2D(12.0, 8.0),
        end_point=Vector2D(0.0, 8.0),
        thickness_m=0.20,
        height_m=2.8,
        is_external=True,
        substrate="Lightweight FC Cladding",
        finish="Haymes Solashield - White Duck Half",
        confidence=0.85,
        review_state=ReviewState.INFERRED,
        provenance=prov_lvl1,
    )
    wall_l1_west = CanonicalWall(
        id="wall_l1_ext_west",
        name="Level 1 West External Wall",
        level_id=lvl1.id,
        start_point=Vector2D(0.0, 8.0),
        end_point=Vector2D(0.0, 0.0),
        thickness_m=0.20,
        height_m=2.8,
        is_external=True,
        substrate="Lightweight FC Cladding",
        finish="Haymes Solashield - White Duck Half",
        confidence=0.85,
        review_state=ReviewState.INFERRED,
        provenance=prov_lvl1,
    )

    floor_l1 = CanonicalFloor(
        id="floor_l1_slab",
        name="Level 1 Timber Floor Joist Structure",
        level_id=lvl1.id,
        polygon=[Vector2D(0, 0), Vector2D(12, 0), Vector2D(12, 8), Vector2D(0, 8)],
        thickness_m=0.25,
        elevation_offset_m=0.0,  # Explicit zero offset known
        substrate="Engineered Timber Joists & Particleboard Floor",
        finish="Carpet & Underlay",
        confidence=0.85,
        review_state=ReviewState.INFERRED,
    )

    balcony_l1 = CanonicalBalcony(
        id="balcony_l1_front",
        name="Master Bedroom Cantilevered Balcony",
        level_id=lvl1.id,
        polygon=[Vector2D(0.0, -1.8), Vector2D(4.5, -1.8), Vector2D(4.5, 0.0), Vector2D(0.0, 0.0)],
        thickness_m=0.20,
        elevation_offset_m=0.0,  # Explicit zero offset known
        substrate="Waterproofed FC Sheet Substrate",
        finish="External Non-Slip Porcelain Tiles",
        confidence=0.90,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl1,
    )

    soffit_balcony = CanonicalSoffit(
        id="soffit_l1_balcony",
        name="Level 1 Balcony Underside Exterior Soffit",
        level_id=lvl1.id,
        parent_id=balcony_l1.id,
        polygon=[Vector2D(0.0, -1.8), Vector2D(4.5, -1.8), Vector2D(4.5, 0.0), Vector2D(0.0, 0.0)],
        thickness_m=0.02,
        elevation_offset_m=-0.20,
        substrate="Villaboard Cement Sheet Soffit Lining",
        finish="Exterior Acrylic Flat White Paint",
        confidence=0.90,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl1,
    )

    balustrade_front = CanonicalBalustrade(
        id="balustrade_b1",
        name="Front Balcony Toughened Glass Balustrade",
        level_id=lvl1.id,
        parent_id=balcony_l1.id,
        start_point=Vector2D(0.0, -1.8),
        end_point=Vector2D(4.5, -1.8),
        height_m=1.05,
        substrate="12mm Frameless Toughened Safety Glass",
        finish="Polished Stainless Steel Hardware",
        confidence=0.90,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl1,
    )
    balustrade_side = CanonicalBalustrade(
        id="balustrade_b2",
        name="Front Balcony Side Glass Balustrade",
        level_id=lvl1.id,
        parent_id=balcony_l1.id,
        start_point=Vector2D(4.5, -1.8),
        end_point=Vector2D(4.5, 0.0),
        height_m=1.05,
        substrate="12mm Frameless Toughened Safety Glass",
        finish="Polished Stainless Steel Hardware",
        confidence=0.90,
        review_state=ReviewState.CONFIRMED,
        provenance=prov_lvl1,
    )
    balcony_l1.balustrade_ids = [balustrade_front.id, balustrade_side.id]

    lvl1.walls = [wall_l1_south, wall_l1_east, wall_l1_north, wall_l1_west]
    lvl1.floors = [floor_l1]
    lvl1.balconies = [balcony_l1]
    lvl1.soffits = [soffit_balcony]
    lvl1.balustrades = [balustrade_front, balustrade_side]

    # ----------------------------------------------------
    # LEVEL 2: ROOF LEVEL & PARAPET (FFL 5.8m, height 1.2m)
    # ----------------------------------------------------
    lvl2 = CanonicalLevel(
        id="lvl_roof",
        name="Roof & Parapet Level",
        elevation_m=5.8,
        height_m=1.2,
        level_index=2,
        confidence=0.45,
        review_state=ReviewState.REVIEW_REQUIRED,
        provenance=Provenance(
            source_pdf="Architectural_Set_RevB.pdf",
            page_number=6,
            drawing_id="EL-01",
            scale_source="Unverified Section Height Note",
            contributing_evidence=["EL-01 Front Elevation Note 'Parapet Height Unconfirmed'"],
        ),
    )

    prov_lvl2_rev = Provenance(
        source_pdf="Architectural_Set_RevB.pdf",
        page_number=6,
        drawing_id="EL-01",
        contributing_evidence=["Elevation section height unverified; required manual review"],
    )

    parapet_south = CanonicalParapet(
        id="parapet_south_roof",
        name="Roof Perimeter Parapet Wall - South",
        level_id=lvl2.id,
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(12.0, 0.0),
        height_m=1.1,
        thickness_m=0.20,
        substrate="Rendered Masonry Capped",
        finish="Taubmans Weatherbeater Acrylic - Monument",
        confidence=0.45,
        review_state=ReviewState.REVIEW_REQUIRED,
        provenance=prov_lvl2_rev,
    )

    roof_main = CanonicalRoof(
        id="roof_main_envelope",
        name="Main Metal Deck Low Pitch Roof Envelope",
        level_id=lvl2.id,
        polygon=[Vector2D(0, 0), Vector2D(12, 0), Vector2D(12, 8), Vector2D(0, 8)],
        thickness_m=0.15,
        elevation_offset_m=0.0,  # Explicit zero offset known
        pitch_deg=3.0,
        overhang_m=0.3,
        roof_type="FLAT",
        substrate="Colorbond Custom Orb Metal Sheet Roofing",
        finish="Colorbond Monument Matte",
        confidence=0.50,
        review_state=ReviewState.REVIEW_REQUIRED,
        provenance=prov_lvl2_rev,
    )

    lvl2.parapets = [parapet_south]
    lvl2.roofs = [roof_main]

    building.levels = [lvl0, lvl1, lvl2]
    project.buildings = [building]

    return project
