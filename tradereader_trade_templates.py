from __future__ import annotations

"""Estimator starter templates for TradeReader 3D.

These templates are deliberately quantity-first. They provide a repeatable starting
schedule, document checklist, exclusions and common RFIs for each trade without
inventing project-specific quantities or rates.
"""


def _row(section: str, element: str, unit: str, measurement_basis: str, notes: str = "") -> dict:
    return {
        "section": section,
        "element": element,
        "unit": unit,
        "measurement_basis": measurement_basis,
        "notes": notes,
        "quantity": 0.0,
        "quantity_status": "To measure",
        "rate_per_unit": 0.0,
        "inclusion_status": "INCLUSION",
        "confidence": "To review",
    }


COMMON_DOCUMENTS = [
    "Current architectural drawing set and drawing register",
    "Relevant trade drawings / consultant drawings",
    "Specifications and schedules",
    "Scope of works / tender inclusions and exclusions",
    "Addenda, bulletins and latest revisions",
    "Relevant details and sections",
]

COMMON_RFI_CHECKS = [
    "Confirm latest drawing revision and tender issue",
    "Confirm supply-only versus supply-and-install scope",
    "Confirm interfaces with adjacent trades",
    "Confirm access, staging and after-hours requirements",
    "Confirm testing, commissioning, certification and handover obligations",
    "Confirm provisional quantities where dimensions or routes are not shown",
]


TRADE_TEMPLATES = {
    "Electrical": {
        "documents": COMMON_DOCUMENTS + [
            "Electrical layouts and schematics",
            "Lighting schedules",
            "Switchboard schedules and single-line diagrams",
            "Communications / security / fire interface drawings",
        ],
        "rows": [
            _row("Main switchboards & distribution", "Main switchboards / distribution boards", "No.", "Count from schedules and electrical drawings"),
            _row("Power", "General power outlets", "point", "Count symbols by room / area and type"),
            _row("Power", "Dedicated equipment connections", "point", "Count nominated equipment connections"),
            _row("Lighting", "Light fittings", "No.", "Count by fitting type from lighting plan and schedule"),
            _row("Lighting", "Light switches / control points", "point", "Count controls by type"),
            _row("Emergency & exit lighting", "Emergency lights and exit signs", "No.", "Count from lighting / fire interface drawings"),
            _row("Data / communications", "Data / communications outlets", "point", "Count by outlet type"),
            _row("Containment", "Cable tray / ladder / conduit", "lm", "Measure shown routes; otherwise leave provisional"),
            _row("External electrical", "External lights / power / equipment", "No.", "Count external electrical items"),
            _row("Testing / commissioning", "Testing, commissioning and certification", "item", "Project allowance / explicit scope item"),
        ],
        "exclusions": [
            "Utility authority works unless explicitly included",
            "Equipment internals supplied by specialist vendors",
            "Unshown cable routes or concealed lengths",
            "Builder's penetrations / patching unless allocated to electrical trade",
        ],
        "rfi_checks": COMMON_RFI_CHECKS + [
            "Confirm who supplies light fittings and specialist equipment",
            "Confirm fire, security, AV and data interfaces",
            "Confirm switchboard modifications versus complete replacement",
        ],
    },
    "Plumbing": {
        "documents": COMMON_DOCUMENTS + [
            "Hydraulic drawings and schematics",
            "Sanitary fixture schedules",
            "Civil / stormwater drawings where interfacing",
            "Hot-water plant schedules",
        ],
        "rows": [
            _row("Sanitary fixtures", "Toilets / pans", "No.", "Count fixture symbols and schedules"),
            _row("Sanitary fixtures", "Basins / sinks / troughs", "No.", "Count fixture types"),
            _row("Sanitary fixtures", "Taps / mixers / showers", "No.", "Count scheduled fittings"),
            _row("Cold water", "Cold-water pipework", "lm", "Measure shown routes by diameter where possible"),
            _row("Hot water", "Hot-water pipework", "lm", "Measure shown routes by diameter where possible"),
            _row("Sanitary drainage", "Sanitary drainage pipework", "lm", "Measure shown routes and risers"),
            _row("Stormwater", "Stormwater pipework / drainage", "lm", "Measure shown routes"),
            _row("Stormwater", "Pits / floor wastes / drains", "No.", "Count by type"),
            _row("Pumps / plant", "Pumps / hot-water plant / equipment", "No.", "Count scheduled plant"),
            _row("Testing / commissioning", "Testing, commissioning and certification", "item", "Explicit project scope / allowance"),
        ],
        "exclusions": [
            "Authority headworks unless explicitly included",
            "Civil drainage outside nominated hydraulic scope",
            "Equipment internals by specialist suppliers",
            "Unshown underground routes or depths",
        ],
        "rfi_checks": COMMON_RFI_CHECKS + [
            "Confirm fixture supply responsibility",
            "Confirm pipe material and diameter schedule",
            "Confirm below-ground excavation / backfill responsibility",
        ],
    },
    "HVAC / Mechanical": {
        "documents": COMMON_DOCUMENTS + [
            "Mechanical layouts and schematics",
            "Mechanical equipment schedules",
            "Duct and pipe sizing information",
            "Controls / BMS documentation",
        ],
        "rows": [
            _row("Mechanical plant", "AHUs / FCUs / condensers / packaged plant", "No.", "Count from equipment schedules"),
            _row("Ventilation", "Fans", "No.", "Count by type and duty"),
            _row("Air outlets", "Diffusers / grilles / registers", "No.", "Count by type"),
            _row("Ductwork", "Rectangular / circular ductwork", "m²", "Measure duct surface area where dimensions are available"),
            _row("Ductwork", "Duct route allowance", "lm", "Measure route length where surface area cannot yet be derived"),
            _row("Pipework", "Mechanical pipework", "lm", "Measure by service and diameter"),
            _row("Insulation", "Duct / pipe insulation", "m²", "Derive from supported duct / pipe dimensions"),
            _row("Controls", "Controls / sensors / thermostats", "No.", "Count scheduled control points"),
            _row("External mechanical", "Louvers / external plant interfaces", "No.", "Count shown external items"),
            _row("Testing / commissioning", "Testing, balancing and commissioning", "item", "Explicit project scope / allowance"),
        ],
        "exclusions": [
            "Electrical power supply beyond nominated interfaces",
            "Builder's penetrations / structural works unless included",
            "Specialist plant internals",
            "Unshown concealed duct or pipe routes",
        ],
        "rfi_checks": COMMON_RFI_CHECKS + [
            "Confirm duct gauge / insulation specification",
            "Confirm controls and BMS extent",
            "Confirm crane, lifting and plant access requirements",
        ],
    },
    "Carpentry / Joinery": {
        "documents": COMMON_DOCUMENTS + [
            "Door and hardware schedules",
            "Joinery drawings and details",
            "Wall / roof framing plans",
            "Finishes schedules",
        ],
        "rows": [
            _row("Structural carpentry", "Timber framing", "m³", "Measure framing members where sizes and lengths are shown"),
            _row("Wall framing", "Wall framing", "lm", "Measure wall lengths by type"),
            _row("Roof framing", "Roof framing / battens", "lm", "Measure members by type where detailed"),
            _row("Doors & frames", "Door leaves", "No.", "Count from door schedule"),
            _row("Doors & frames", "Door frames", "No.", "Count from door schedule"),
            _row("Skirtings / architraves", "Skirtings", "lm", "Measure room perimeters less exclusions as applicable"),
            _row("Skirtings / architraves", "Architraves", "lm", "Measure openings by frame type"),
            _row("Internal joinery", "Cabinetry / joinery units", "item", "Count / separate by joinery reference"),
            _row("Hardware", "Door / cabinet hardware sets", "set", "Count from hardware schedule"),
            _row("External timber", "External timber / battens / screens", "lm", "Measure visible documented members"),
        ],
        "exclusions": [
            "Factory-supplied specialist equipment",
            "Metal framing unless specifically included",
            "Painting / staining unless allocated to carpentry trade",
            "Unscheduled loose furniture",
        ],
        "rfi_checks": COMMON_RFI_CHECKS + [
            "Confirm supply responsibility for doors, frames and hardware",
            "Confirm joinery shop-drawing scope",
            "Confirm timber species / treatment / finish requirements",
        ],
    },
    "Plastering / Linings": {
        "documents": COMMON_DOCUMENTS + [
            "Wall type schedules",
            "Reflected ceiling plans",
            "Fire / acoustic wall details",
            "Wet-area details",
        ],
        "rows": [
            _row("Wall linings", "Plasterboard wall linings", "m²", "Wall length × height less documented openings"),
            _row("Ceilings", "Plasterboard ceilings", "m²", "Measure ceiling plan areas"),
            _row("Bulkheads", "Bulkheads / drops", "m²", "Measure faces and soffits separately where detailed"),
            _row("Wet-area linings", "Fibre-cement / wet-area linings", "m²", "Measure wet-area wall / ceiling surfaces"),
            _row("External linings", "External fibre-cement linings", "m²", "Measure elevations / plans"),
            _row("Cornices / trims", "Cornice", "lm", "Measure room perimeter where specified"),
            _row("Cornices / trims", "Angles / beads / control joints", "lm", "Measure detailed edges and joints"),
            _row("Access panels", "Access panels", "No.", "Count from RCP / services drawings"),
            _row("Acoustic / fire systems", "Fire / acoustic rated systems", "m²", "Measure wall / ceiling types separately"),
        ],
        "exclusions": [
            "Painting and decorative finishes",
            "Structural framing unless explicitly in scope",
            "Major substrate rectification",
            "Factory-finished proprietary wall systems by others",
        ],
        "rfi_checks": COMMON_RFI_CHECKS + [
            "Confirm wall heights and ceiling levels",
            "Confirm fire / acoustic build-ups and layers",
            "Confirm insulation and framing responsibility",
        ],
    },
    "Tiling": {
        "documents": COMMON_DOCUMENTS + [
            "Finishes schedules",
            "Wet-area elevations / internal elevations",
            "Waterproofing details",
            "Tile set-out drawings where issued",
        ],
        "rows": [
            _row("Floor tiling", "Floor tiles", "m²", "Net floor area by tile type"),
            _row("Wall tiling", "Wall tiles", "m²", "Wall length × tiled height less openings"),
            _row("Skirtings", "Tile skirtings", "lm", "Measure nominated perimeters"),
            _row("Splashbacks", "Splashback tiling", "m²", "Measure joinery / wet-area elevations"),
            _row("External tiling", "External tiles / pavers", "m²", "Measure documented external tiled surfaces"),
            _row("Waterproofing", "Floor waterproofing", "m²", "Measure wet-area floor extent"),
            _row("Waterproofing", "Wall waterproofing", "m²", "Measure nominated wall extents / heights"),
            _row("Movement joints", "Movement / sealant joints", "lm", "Measure nominated joint lines"),
            _row("Trims / edges", "Tile trims / edge profiles", "lm", "Measure exposed tiled edges"),
        ],
        "exclusions": [
            "Sanitary fixtures and accessories",
            "Major floor levelling / substrate remediation unless documented",
            "Painting and non-tiled finishes",
            "Stone benchtops unless explicitly included",
        ],
        "rfi_checks": COMMON_RFI_CHECKS + [
            "Confirm tile supply versus install-only scope",
            "Confirm tile sizes, patterns and wastage basis",
            "Confirm waterproofing extent and certification requirements",
        ],
    },
    "Flooring": {
        "documents": COMMON_DOCUMENTS + [
            "Floor finishes plans and schedules",
            "Room finish schedules",
            "Stair details",
            "Subfloor / moisture requirements",
        ],
        "rows": [
            _row("Carpet", "Carpet", "m²", "Net room area by carpet type"),
            _row("Carpet", "Carpet underlay", "m²", "Match carpet coverage where specified"),
            _row("Vinyl", "Vinyl / resilient flooring", "m²", "Net room area by finish type"),
            _row("Timber / laminate", "Timber / laminate flooring", "m²", "Net room area"),
            _row("Floor preparation", "Floor preparation / levelling", "m²", "Area explicitly requiring preparation"),
            _row("Skirtings / trims", "Skirting / coving", "lm", "Measure room perimeters by type"),
            _row("Skirtings / trims", "Transitions / trims", "lm", "Measure finish transitions and edges"),
            _row("Stairs", "Stair treads / risers", "No.", "Count treads / risers by finish"),
            _row("External flooring", "External floor finishes", "m²", "Measure documented external flooring"),
        ],
        "exclusions": [
            "Structural slabs",
            "Major moisture remediation unless documented",
            "Loose rugs / furniture",
            "Floor finishes allocated to tiling or specialist trades",
        ],
        "rfi_checks": COMMON_RFI_CHECKS + [
            "Confirm floor finish supply responsibility",
            "Confirm substrate moisture / preparation standard",
            "Confirm pattern direction, roll widths and wastage assumptions",
        ],
    },
    "Roofing": {
        "documents": COMMON_DOCUMENTS + [
            "Roof plans",
            "Roof elevations / sections",
            "Roofing and rainwater details",
            "Roof safety / access documentation",
        ],
        "rows": [
            _row("Roof sheeting", "Roof sheeting / roof covering", "m²", "Measure roof planes including supported slope factor"),
            _row("Flashings", "Ridge / hip / apron / barge flashings", "lm", "Measure flashing lines by type"),
            _row("Gutters", "Gutters", "lm", "Measure eaves / box gutter lengths"),
            _row("Downpipes", "Downpipes", "No.", "Count by type and size"),
            _row("Roof penetrations", "Roof penetrations / flashings", "No.", "Count vents, flues, skylights and equipment penetrations"),
            _row("Insulation / sarking", "Roof insulation / sarking", "m²", "Match documented roof coverage"),
            _row("Roof safety", "Roof anchors / static lines / walkways", "No.", "Count / measure from roof safety plan"),
            _row("Rainwater goods", "Rainheads / sumps / overflows", "No.", "Count from hydraulic / roof details"),
        ],
        "exclusions": [
            "Structural roof framing",
            "Solar / electrical equipment",
            "Mechanical plant and ductwork",
            "Unshown temporary access systems",
        ],
        "rfi_checks": COMMON_RFI_CHECKS + [
            "Confirm roof sheet profile, material and finish",
            "Confirm insulation / sarking responsibility",
            "Confirm scaffold, edge protection and crane access",
        ],
    },
    "Concreting": {
        "documents": COMMON_DOCUMENTS + [
            "Structural drawings and schedules",
            "Footing and slab plans",
            "Concrete specification",
            "Civil pavement drawings where relevant",
        ],
        "rows": [
            _row("Footings", "Pad / strip / bored footing concrete", "m³", "Calculate from documented dimensions"),
            _row("Slabs", "Ground / suspended slabs", "m³", "Area × thickness by slab type"),
            _row("Walls", "Concrete walls", "m³", "Length × thickness × height"),
            _row("Columns", "Concrete columns", "m³", "Count × cross-section × height"),
            _row("Beams", "Concrete beams", "m³", "Length × cross-section"),
            _row("Stairs", "Concrete stairs / landings", "m³", "Derive from structural detail where supported"),
            _row("External pavements", "Concrete pavements", "m³", "Area × thickness"),
            _row("Reinforcement", "Reinforcement", "t", "Use bar schedules / mesh schedules where provided"),
            _row("Formwork", "Formwork", "m²", "Measure exposed formed faces"),
            _row("Sundries", "Joints / rebates / blockouts", "lm", "Measure detailed joint and edge treatments"),
        ],
        "exclusions": [
            "Excavation and spoil unless included",
            "Reinforcement without a supported schedule / detail",
            "Structural steel",
            "Concrete pumping or special access unless specified",
        ],
        "rfi_checks": COMMON_RFI_CHECKS + [
            "Confirm concrete grades and finish classes",
            "Confirm reinforcement supply / fixing scope",
            "Confirm excavation, formwork and pumping responsibilities",
        ],
    },
    "Landscaping": {
        "documents": COMMON_DOCUMENTS + [
            "Landscape plans and schedules",
            "Planting schedules",
            "Irrigation plans",
            "Civil levels / grading drawings",
        ],
        "rows": [
            _row("Earthworks", "Imported soil / growing media", "m³", "Area × documented depth"),
            _row("Paving", "Landscape paving", "m²", "Net paved areas by type"),
            _row("Retaining", "Retaining walls", "lm", "Measure wall lengths by type and height band"),
            _row("Planting", "Trees", "No.", "Count from planting plan and schedule"),
            _row("Planting", "Shrubs / groundcovers", "No.", "Count or scheduled density × area"),
            _row("Planting", "Mulch", "m³", "Planting area × mulch depth"),
            _row("Turf", "Turf / lawn", "m²", "Measure lawn areas"),
            _row("Irrigation", "Irrigation pipework", "lm", "Measure shown routes where documented"),
            _row("Fencing / screens", "Fencing / screens", "lm", "Measure plan lengths by type"),
            _row("External furniture", "Bins / seats / bollards / furniture", "No.", "Count from landscape plan / schedule"),
        ],
        "exclusions": [
            "Civil authority works",
            "Major bulk earthworks unless allocated to landscaping",
            "Electrical services beyond nominated interfaces",
            "Unscheduled maintenance periods unless specified",
        ],
        "rfi_checks": COMMON_RFI_CHECKS + [
            "Confirm plant sizes, pot sizes and establishment period",
            "Confirm soil depths and imported soil specification",
            "Confirm irrigation controls and water connection responsibility",
        ],
    },
    "Custom trade": {
        "documents": COMMON_DOCUMENTS,
        "rows": [
            _row("Primary scope", "Primary measured work", "item", "Set unit and measurement rule for this trade"),
            _row("Secondary scope", "Secondary measured work", "item", "Set unit and measurement rule for this trade"),
            _row("Equipment / fixtures", "Equipment / fixtures", "No.", "Count scheduled or shown items"),
            _row("Materials", "Measured materials", "m²", "Choose m² / m³ / lm / kg / t as appropriate"),
            _row("External works", "External trade scope", "item", "Measure only documented scope"),
            _row("Testing / completion", "Testing / commissioning / certification", "item", "Explicit project requirement"),
            _row("Provisional items", "Unresolved / provisional scope", "allowance", "Use only where project documents require an allowance"),
        ],
        "exclusions": [
            "Work clearly allocated to another trade",
            "Unsupported quantities or dimensions",
            "Rates or pricing not supplied by the estimator",
        ],
        "rfi_checks": COMMON_RFI_CHECKS,
    },
}


def get_trade_template(trade_name: str) -> dict:
    """Return a safe copy of the requested trade template."""
    import copy

    return copy.deepcopy(TRADE_TEMPLATES.get(trade_name) or TRADE_TEMPLATES["Custom trade"])
