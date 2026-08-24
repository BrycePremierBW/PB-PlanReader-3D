# Priority 4 — Phase A Investigation: Filled Polygons, Hatches, Substrates & Finish Allocation

## Executive Summary

PlanReader already has a substantial text-based substrate/finish pipeline (material schedules, legend registers, finish codes, substrate inference, elevation regions). However, it has **zero capability** for detecting filled polygons, hatch patterns, or colour fills from PDF vector geometry. The gap is not "finishes are missing" — it is that **geometry-to-substrate association does not exist**. The text pipeline can resolve what PT01 means; it cannot determine where PT01 applies on a drawing.

Priority 4 Phase B must bridge: `authoritative measured geometry` → `substrate/finish allocation` → `take-off breakdown by substrate and coating system`.

---

## 1. Existing Architecture: Active vs Dormant Paths

### 1.1 Active Production Modules

| Module | Version | Status | Role |
|---|---|---|---|
| `pb_material_schedule_v1222.py` | 1.2.22 | **Active** | Parses finish schedules, builds code→description/substrate/finish dictionary, finds code occurrences on drawings |
| `pb_legend_register_v1227.py` | 1.2.27 | **Active** | Identifies legend/abbreviation sheets, expands project-specific abbreviations, material shorthand |
| `pb_code_register_v1225.py` | 1.2.25 | **Active** | Manual code definitions (estimator overrides), default substrate/element vocabulary |
| `pb_premier_takeoff_v1225.py` | 1.2.25 | **Active** | Painting-estimator takeoff builder: groups rows by floor/wall/ceiling/door/external, associates finish codes, coating/preparation notes |
| `pb_no_ai_takeoff_v1216.py` | 1.2.16 | **Active** | Deterministic takeoff from mapped zones, classify_context() for section/element assignment |
| `pb_registered_substrates_v138.py` | 1.3.8 | **Active** | Maps resolved takeoff evidence onto registered facade sides/walls — single-substrate sides get High confidence, mixed substrates → Review |
| `pb_elevation_regions_v1226.py` | 1.2.26 | **Active** | Substrate zoning on elevations — groups code callout positions into horizontal/vertical bands with polygon regions |
| `pb_unified_building_v139.py` | 1.3.9 | **Active** | Wall assembly: plan length × solved height − openings; calls `registered_substrates_v138` for substrate assignment |
| `pb_substrate_qa_v131.py` | 1.3.1 | **Active** | 3D substrate QA view: elevations as primary evidence, artist impressions as secondary, status colours for confirmed/probable/needs_check/conflict |
| `pb_accuracy_v13_engines_v145.py` | 1.4.5 | **Active** | Topology engines: `facade_net_area()` (gross→net by substrate), `semantic_assignment()` (code→scope resolution), `reconcile_facade()` (cross-view QA) |

### 1.2 Active Infrastructure Modules (Coordinate/Geometry)

| Module | Role | Substrate relevance |
|---|---|---|
| `pb_vector_geometry_v130.py` | Extracts line/rect primitives from `get_drawings()` | Extracts `fill` and `stroke` colours but only uses them as metadata on individual line segments. Does NOT reconstruct filled polygons. |
| `pb_room_face_takeoff.py` | Room polygon extraction with calibration | Produces floor-area takeoff rows; has `_point_in_polygon`, centroid containment logic |
| `pb_height_evidence_v150.py` | Positioned word extraction via PyMuPDF | Extracts positioned text with bbox coordinates |
| `pb_accuracy_v13_engines_v145.py` | `extract_planar_faces()`, `_point_in_polygon()` | Face extraction from line segments, point-in-polygon tests |

### 1.3 Dormant/Experimental Modules

| Module | Status | Potential for P4 |
|---|---|---|
| `reconcile_facade_v145` | Dormant (only exposed on app object, never called in production chain) | Low — cross-view QA only, not geometry-based |
| `pb_accuracy_v13_engines_v145::reconcile_overhead_regions` | Active but narrow | Medium — ceiling/soffit scope separation; could inform ceiling substrate allocation |
| `pb_accuracy_v13_engines_v145::facade_net_area` | Active — substrate net area calculation | **High** — already calculates net m² by substrate type; Priority 4 output feeds directly into this |
| `pb_accuracy_v13_engines_v145::semantic_assignment` | Active — code→scope resolution | **High** — resolves codes to substrate/finish/coating system; Priority 4 geometry feeds codes into this |
| `pb_substrate_qa_v131` | Active but UI-only (3D QA view) | Medium — schema for surface status (confirmed/probable/needs_check/conflict) is reusable |

### 1.4 Separate-Generation / Dormant Code (Not in Production Chain)

| Module/Location | Status | Potential for P4 |
|---|---|---|
| **TradeReader family** (`tradereader_*.py`) | Separate product, NOT imported by PlanReader painting chains | Medium — has wall-type/finish-schedule handling, plastering assemblies with `level_of_finish` |
| **Jobhub colour schedule** (`opencode/PremierBWJobhub-main-2/.../pb_planreader_app.py`) | Active within Jobhub app generation | **High** — has `COLOUR_SCHEDULE_COLUMNS`, `DEFAULT_FINISH_BY_SURFACE`, `resolve_colour_hex()`, `seed_colour_schedule()`, hex-swatch editor, `coating_system` DB columns |
| **Jobhub smart_intake** (`jobhub/smart_intake.py::parse_colour_schedule_bytes`) | Active in Jobhub | **High** — CSV/XLSX colour/finish schedule ingestion with header aliases (Location/Substrate/Product/"Coating System") |
| **Jobhub planrender_studio** (`planrender_studio.py`) | Active in Jobhub | Medium — interactive substrate/colour swatch studio, `.swatch-row` UI |
| **Jobhub planreader_bridge** (`jobhub/planreader_bridge.py::sync_colour_schedule`) | Active in Jobhub | Medium — upserts colour_schedule rows (colour, finish, product, hex) |
| **Root pb_planreader_3d_app.py** SUBSTRATES/FINISH_SYSTEMS constants | Active in base app | Low — older generation constants, superseded by material schedule pipeline |
| **Reconstruction-only modules** (v136-v150) | Dormant unless launched via v139 reconstruction app | Covered by active chain |

### 1.5 What Does NOT Exist in the PlanReader Production Chain

The following capabilities are **completely absent** from the codebase:

- **No filled polygon extraction** from `get_drawings()` — the vector geometry module only extracts individual line/rect edges
- **No hatch pattern detection** — no parallel line grouping, no angle/spacing analysis
- **No colour/fill clustering** — no RGB grouping, no swatch-to-code mapping
- **No legend swatch detection** — the legend register only parses text, not visual swatches
- **No geometry-to-finish association** — code occurrences have bbox positions but no polygon containment logic
- **No PDF pattern/shading analysis** — no handling of tiling patterns, shading objects, or PDF resources
- **No raster hatch analysis** — no image-based pattern detection

---

## 2. Native PDF Fill/Hatch Capabilities (PyMuPDF 1.28.0)

### 2.1 `page.get_drawings()` Return Structure

Each drawing path contains:

| Field | Type | Description |
|---|---|---|
| `fill` | `(r,g,b)` tuple or `None` | Fill colour (RGB, 0-1 range). `None` = no fill (stroke-only). |
| `color` | `(r,g,b)` tuple or `None` | Stroke colour. `None` = no stroke (fill-only). |
| `fill_opacity` | `float` | Fill transparency (0-1). Default 1.0. |
| `stroke_opacity` | `float` | Stroke transparency (0-1). Default 1.0. |
| `width` | `float` | Stroke width in PDF points. |
| `dashes` | `str` | Dash pattern string (e.g. `"[3 2]"`). Empty = solid. |
| `closePath` | `bool` | Whether path is explicitly closed. |
| `even_odd` | `bool` | Fill rule. |
| `lineCap` | `tuple` | Line cap style. |
| `lineJoin` | `float` | Line join style. |
| `layer` | `str` | Optional content group / layer name. |
| `items` | `list` | Drawing primitives within this path. |

### 2.2 Drawing Primitive Types

| Item kind | Meaning | Priority 4 relevance |
|---|---|---|
| `"l"` | Line segment (two endpoints) | Hatch strokes, wall lines, polygon edges |
| `"re"` | Rectangle (x0,y0,x1,y1) | Filled wall rectangles, legend swatches, zone boundaries |
| `"c"` | Bezier curve (4 control points) | Curved walls, organic shapes |
| `"qu"` | Quadrilateral (4 corner points) | Non-rectangular fills, perspective views |

### 2.3 How Architectural Hatches Appear

Based on PyMuPDF analysis, architectural hatches can appear as:

1. **True filled polygons** — A single drawing with `fill=(r,g,b)` and `"re"` (rectangle) or multiple `"l"` items forming a closed path. The fill colour is the hatch colour. `closePath` may be `True` or `False` (the last segment connects back to the first).

2. **Repeated vector strokes** — A single drawing with multiple `"l"` items at consistent angles and spacing. The `dashes` field shows dash patterns. This is the most common hatch representation in architectural PDFs.

3. **PDF pattern/shading resources** — Referenced via `/Pattern` or `/Shading` in the PDF content stream. PyMuPDF does NOT expose these through `get_drawings()` — they would need `page.get_texttrace()` or raw content stream parsing. This is rare in modern architectural PDFs.

4. **Raster images** — Embedded as `/Image` resources. PyMuPDF can extract these via `page.get_images()`. Common in scanned drawings.

5. **Combinations** — A filled polygon with overlaid hatch strokes, or a clipping path containing repeated strokes.

### 2.4 Critical Gap in Current Vector Geometry Module

`pb_vector_geometry_v130.py::extract_native_page()` (lines 67-130):
- Reads `fill` and `stroke` from each drawing
- Processes `"l"` (line) and `"re"` (rectangle) items only
- **Converts rectangles into 4 individual edge segments** (loses the rectangle identity)
- **Does NOT group items by drawing** — each line/edge is an independent segment
- **Does NOT reconstruct closed polygons** from sequential line items
- **Does NOT use `fill` for polygon detection** — it's stored as metadata on edges but never used to identify filled regions

This means: even if a wall is drawn as a filled red rectangle, the current engine sees it as 4 unrelated line segments with `fill=(1,0,0)` metadata, not as a filled polygon.

---

## 3. Coordinate System Compatibility

### 3.1 All Coordinate Systems Are PDF Points

| System | Unit | Source | Conversion |
|---|---|---|---|
| `get_drawings()` items | PDF points (1/72 inch) | PyMuPDF | Direct — no conversion needed |
| `get_text("words")` bbox | PDF points | PyMuPDF | Direct |
| `WordBox` (v150) | PDF points | PyMuPDF | Direct |
| Room polygons (Priority 2) | PDF points | Derived from `get_drawings()` segments | Same coordinate space |
| Wall geometry (v139) | PDF points | From room polygons / elevation registration | Same coordinate space |
| Elevation registration bbox | PDF points | Manual/facade detection | Same coordinate space |
| `px_per_m` calibration | px/m | `page.px_per_m` setting | PDF points × render_zoom / px_per_m = metres |

**Finding: All geometry operates in the same PDF coordinate space.** No conversion is required between fills, hatches, room polygons, wall geometry, and positioned text. This is a strong foundation.

### 3.2 Calibration Chain

The authoritative calibration chain (established in Priority 1):
```
PDF points × (25.4/72) = page mm
page mm × real_metres_per_page_mm = real metres
```

Or equivalently: `rpm = render_zoom × 2.834646 / px_per_m`

Fill polygons extracted from `get_drawings()` are in PDF points and can be directly converted using the same `px_per_m` calibration. No new coordinate system is needed.

---

## 4. Existing Legend/Schedule Architecture

### 4.1 Text-Based Code Recognition

**`pb_material_schedule_v1222.py`** provides:

- `CODE_RE`: Recognises codes matching `EC\d+|FC\d+|RBL\d*|SOF\d*|CL\d+|PT\d+|PF\d+|WF\d+|BA\d+|SCR\d*|SHD\d*|DP\d*|GD\d*|RS\d*|BC\d*`
- `parse_schedule_text()`: Extracts code→description pairs from schedule pages
- `_infer_substrate()`: Keyword matching (lineaboard, textureboard, easylap, fibre cement, render, timber, soffit, etc.)
- `_infer_finish()`: Code prefix + keyword matching (dulux, paint, primer, etc.)
- `build_material_dictionary()`: Aggregates across schedule pages, detects conflicts
- `_page_occurrences()`: Finds code references on drawing pages with bbox positions

**`pb_legend_register_v1227.py`** provides:

- `is_legend_page()`: Identifies legend/abbreviation sheets by page_type and title phrases
- Abbreviation expansion via `expand_abbreviations()`
- Material keyword recognition (`_material_words` tuple)
- Code pattern recognition (`_CODE_RE`, `_START_RE`, `_INLINE_PAIR_RE`)

**`pb_code_register_v1225.py`** provides:

- Manual code definitions (estimator overrides)
- Default substrate vocabulary (`DEFAULT_SUBSTRATES`)
- Default element vocabulary (`DEFAULT_ELEMENTS`)
- Priority: manual > schedule > legend

### 4.2 What Legend/Schedule Detection Cannot Do

1. **Cannot detect legend swatches** — The legend register only reads text. If a legend shows a coloured rectangle next to "PT01 — Dulux White", the system reads "PT01 — Dulux White" but does NOT detect the swatch colour or its association with the code.

2. **Cannot detect hatch samples in legends** — If a legend shows a hatched rectangle next to "FC01 — Fibre Cement", the hatch pattern is invisible to the text parser.

3. **Cannot associate schedule codes with drawing regions** — Code occurrences have bbox positions but no polygon containment logic. If "PT01" appears at coordinates (100, 200), the system knows PT01 is at that point but does NOT know which room polygon or wall face it belongs to.

4. **Cannot distinguish substrates by colour** — Two codes might share the same hatch pattern or colour on a drawing. The text parser cannot distinguish them.

### 4.3 Active Evidence Chain

The current evidence chain for substrate/finish is:

```
Schedule pages → parse_schedule_text() → code→description/substrate/finish
     ↓
Legend pages → expand_abbreviations() → project shorthand mapping
     ↓
Manual overrides → set_manual_code() → estimator corrections
     ↓
Drawing pages → _page_occurrences() → code positions with bbox
     ↓
Elevation regions → _region_bands() → substrate zones by callout positions
     ↓
Registered substrates → assign_substrates() → wall-level substrate assignment
     ↓
PB takeoff → build_pb_schedule() → painting scope rows with finish_code
```

**Gap: There is no geometry-based step between "code positions" and "substrate zones".** The elevation regions module groups callout positions into bands, but this is purely positional — it does not use fill polygons or hatch patterns.

---

## 5. Existing Substrate/Finish Logic (Detailed)

### 5.1 Substrate Inference (`_MATERIAL_HINTS`)

From `pb_material_schedule_v1222.py`:

```python
_MATERIAL_HINTS = (
    (("lineaboard", "linea"), "Lineaboard Cladding"),
    (("textureboard",), "Textureboard Cladding"),
    (("easylap",), "Easylap Cladding"),
    (("fibre cement", "fiber cement", "fc sheet", "fc cladding"), "Fibre Cement Cladding"),
    (("render", "rendered", "blockwork", "masonry"), "Rendered / Blockwork"),
    (("timber", "weatherboard"), "Timber / Weatherboard Cladding"),
    (("soffit", "eave"), "Soffits / Eaves"),
    (("screen",), "Screens"),
    (("balustrade",), "Balustrade"),
    (("sunhood", "sun hood"), "Sunhoods"),
    (("downpipe",), "Downpipes"),
    (("garage door",), "Garage Doors"),
    (("roof sheet", "roofing"), "Roof Sheet"),
    (("gutter", "capping", "parapet cap"), "Cappings & Gutters"),
)
```

### 5.2 Finish Inference (`_FINISH_HINTS`)

```python
_FINISH_HINTS = (
    "dulux", "haymes", "taubmans", "resene", "wattyl", "low sheen", "semi gloss",
    "semigloss", "matt", "matte", "gloss", "satin", "paint", "colour", "color",
    "primer", "undercoat", "topcoat", "clear finish", "stain",
)
```

### 5.3 Elevation Region Zoning

`pb_elevation_regions_v1226.py::_region_bands()`:
- Groups code callout positions by (code, substrate)
- Determines dominant axis (x vs y spread)
- Creates horizontal or vertical bands as rectangular polygons
- Each band has a polygon, area, and boundary basis
- Status is always "Derived" — never promoted to Measured without estimator confirmation

### 5.4 Substrate Assignment to Walls

`pb_registered_substrates_v138.py::assign_substrates()`:
- Looks up takeoff evidence for each wall side
- Single substrate on a side → "Resolved" with High confidence
- Multiple substrates → "Mixed / zone required" with Review confidence
- No evidence → "To confirm" with Review confidence

### 5.5 PB Takeoff Substrate Categories

From `pb_premier_takeoff_v1225.py`:
- Internal floor area (pricing basis)
- Ceilings (plasterboard default)
- Internal walls (plasterboard default, pending measurement)
- Entry doors & frames (timber)
- Internal doors/frames/architraves/skirting (timber trim)
- External walls/cladding (from elevation measurements)
- External soffits/eaves (pending measurement)
- Specialist finishes (pending measurement)

---

## 6. Major Accuracy Gaps

### 6.1 Geometry → Substrate (Critical Gap)

**No module extracts filled polygons or hatch patterns from PDF and associates them with measured surfaces.**

Current flow:
```
PDF → get_drawings() → individual line segments (fill metadata ignored)
                    → no polygon reconstruction
                    → no hatch detection
                    → no colour clustering
```

Required flow:
```
PDF → get_drawings() → filled polygon detection → polygon geometry
                   → hatch stroke grouping → pattern signature
                   → colour fill extraction → RGB classification
                      ↓
              associate with measured room/wall/elevation geometry
                      ↓
              substrate/finish code assignment
```

### 6.2 Legend Swatch → Code Association (Medium Gap)

The legend register reads text but cannot detect visual swatches. A legend entry like:

```
[coloured rectangle] PT01 — Dulux White Low Sheen
[hatched rectangle]  FC01 — Fibre Cement Sheet
```

The text parser reads "PT01 — Dulux White Low Sheen" and "FC01 — Fibre Cement Sheet" correctly. But it does NOT:
- Detect the swatch colour (red/blue/etc.)
- Detect the hatch pattern (45° diagonal, cross-hatch, etc.)
- Associate the visual sample with the code

### 6.3 Code Occurrence → Surface Allocation (Medium Gap)

Code occurrences have bbox positions but no polygon containment logic. If "PT01" appears at (100, 200):
- The system knows PT01 is at that point
- It does NOT know which room polygon contains that point
- It does NOT know which wall face that point is on
- It does NOT know which elevation region that point belongs to

The elevation regions module (`_region_bands`) partially addresses this for elevations by grouping callout positions, but:
- Only works for elevation pages
- Uses positional grouping, not polygon containment
- Does not work for floor plans or RCPs

### 6.4 Hatch → Substrate Association (Critical Gap)

Even if hatch patterns are detected, there is no mapping from pattern → substrate:
- 45° diagonal lines could mean render, concrete, or insulation
- Cross-hatch could mean masonry, brick, or blockwork
- Dots could mean concrete, sand, or insulation
- The mapping is project-specific and must come from legend/schedule

### 6.5 Multi-Substrate Surfaces (Complex Gap)

A single wall face may have:
- Render on the lower portion
- FC cladding on the upper portion
- Feature timber on a bay window
- Paint bands at different levels

The elevation regions module handles this for elevation callouts, but:
- Does not handle floor-plan substrates
- Does not handle ceiling substrates
- Does not handle hatch-based detection (only text-based callouts)

### 6.6 Finish System vs Substrate (Conceptual Gap)

The material schedule module conflates substrate and finish in some cases:
- `_infer_substrate()` and `_infer_finish()` are keyword-based
- Some codes map to finishes (PT01 = paint), others to substrates (FC01 = fibre cement)
- The `semantic_assignment()` function in v145 correctly separates `substrate` and `coating_system`
- But the elevation regions module only tracks `code` and `substrate`, not `finish_code` and `coating_system`

---

## 7. Recommended Evidence Model

### 7.1 `SurfaceEvidence` Record

```python
@dataclass
class SurfaceEvidence:
    # Identity
    workspace_id: int
    surface_id: str              # e.g. "page_5:R04", "elev_North:W01:upper"
    
    # Geometry
    geometry: List[Tuple[float, float]]  # Polygon vertices in PDF points
    geometry_type: str            # "filled_polygon", "hatch_region", "colour_fill", "text_associated"
    area_m2: float                # Calibrated area
    
    # Source detection
    source_type: str              # "pdf_fill", "pdf_hatch", "colour_cluster", "legend_swatch",
                                 # "schedule_text", "manual", "schedule_code_occurrence"
    source_bbox: List[float]      # Bounding box of source evidence
    page_id: int
    page_no: int
    page_label: str
    drawing_no: str
    
    # Fill/hatch signature (for pattern matching)
    fill_colour: Optional[Tuple[float, float, float]]  # RGB (0-1) if detected
    fill_opacity: Optional[float]
    hatch_angle_deg: Optional[float]                    # Primary angle if hatch
    hatch_spacing_pt: Optional[float]                   # Spacing in PDF points
    hatch_dashes: Optional[str]                         # Dash pattern string
    hatch_signature: Optional[str]                      # Hash of angle+spacing+dashes
    
    # Substrate/finish codes
    substrate_code: str           # e.g. "FC01", "R01" — from schedule/legend
    substrate_name: str           # e.g. "Fibre Cement Cladding"
    finish_code: str              # e.g. "PT01", "EXT01"
    finish_name: str              # e.g. "Dulux White Low Sheen"
    system_code: str              # e.g. "EXT-SYS-01" — coating system
    
    # Association
    legend_reference: str         # Legend sheet/position where code was defined
    schedule_reference: str       # Schedule page where code was described
    specification_reference: str  # Specification section if available
    room_ref: Optional[str]       # Associated room (if floor-plan)
    wall_ref: Optional[str]       # Associated wall (if elevation)
    elevation_side: Optional[str] # "North", "South", etc.
    
    # Confidence
    geometry_confidence: float    # 0-1: how accurately was the polygon detected
    substrate_confidence: float   # 0-1: how certain is the substrate assignment
    finish_confidence: float      # 0-1: how certain is the finish assignment
    association_confidence: float # 0-1: how certain is the geometry→code association
    
    # Status
    status: str                   # "Measured", "Provisional measured", "Derived", "Review"
    evidence: List[str]           # Human-readable evidence chain
    notes: str
```

### 7.2 Minimal Viable Schema (for Phase B Start)

The full schema above is the target. For Phase B start, the minimum viable fields are:

- `surface_id`, `geometry`, `geometry_type`, `area_m2`
- `source_type`, `page_id`
- `fill_colour`, `hatch_signature`
- `substrate_code`, `substrate_name`, `finish_code`
- `status`, `confidence`

The rest can be added incrementally.

---

## 8. Recommended Association Algorithm

### 8.1 Hierarchy of Evidence (Priority 4 Geometry Pipeline)

```
1. Native vector fill/polygon → polygon area + fill colour
2. Native vector hatch strokes → pattern signature + bounding region
3. Schedule text code occurrence → bbox position
4. Legend swatch + code → colour/pattern signature
5. Manual/estimator assignment
6. Review (unresolved)
```

### 8.2 Association Steps

**Step 1: Extract fill polygons from `get_drawings()`**
- Group items by drawing index (same fill/stroke properties)
- Identify filled paths: `fill is not None` AND (items form closed path OR `"re"` item)
- Reconstruct polygon vertices from sequential line items
- Calibrate area using `px_per_m`

**Step 2: Extract hatch regions from `get_drawings()`**
- Identify stroke-only paths with repeated parallel lines
- Group by: angle (within 5°), spacing (within 20%), stroke width, dash pattern
- Compute bounding polygon of grouped strokes
- Create hatch signature: hash(angle, spacing, width, dashes, colour)

**Step 3: Extract colour fills**
- Group drawings by fill colour (RGB within tolerance)
- Merge adjacent/overlapping same-colour fills
- Compute total area per colour cluster

**Step 4: Match fills/hatches to legend swatches**
- If legend has coloured swatches: match fill colour to swatch colour
- If legend has hatch samples: match hatch signature to sample signature
- Associate code from legend text with matched fill/hatch

**Step 5: Match fills/hatches to schedule code occurrences**
- If a code occurrence bbox is inside a fill polygon: associate code with fill
- If a code occurrence bbox is inside a hatch region: associate code with hatch
- Use point-in-polygon containment (existing `_point_in_polygon()`)

**Step 6: Match to measured geometry**
- Room polygons (Priority 2): use centroid containment or polygon intersection
- Wall faces (Priority 3): use elevation region intersection
- Use existing `_point_in_polygon()` and `_count_other_centroids_inside_candidate()` patterns

**Step 7: Allocate substrate/finish**
- For each measured surface with associated code:
  - Look up code in material dictionary → substrate, finish, coating system
  - Look up code in semantic_assignment() → included/excluded status
  - Apply to take-off row with substrate/finish metadata

### 8.3 Containment Strategy

**Do NOT rely only on centroid matching.** Use a layered approach:

1. **Full containment** (polygon A entirely inside polygon B): Strongest association
2. **Majority overlap** (>50% of polygon A area inside polygon B): Strong association
3. **Centroid containment**: Moderate association (existing `_point_in_polygon`)
4. **Proximity** (centroid within N metres): Weak association, requires Review
5. **No containment**: No association

For walls with multiple substrates:
- Split wall face into substrate zones (using elevation region logic)
- Each zone has its own polygon and substrate assignment
- Net area per substrate = zone area − openings within zone

---

## 9. Conflict Handling Model

### 9.1 Conflict Types

| Conflict | Detection | Resolution |
|---|---|---|
| Two codes overlapping same surface | Polygon intersection area > 0 | Review — estimator must resolve |
| Legend conflicts with schedule | Different descriptions for same code | Schedule wins (per existing `_compatible_descriptions`) |
| Schedule contradicts drawing note | Text conflict detection | Review — surface both issues |
| Multiple substrates within one wall | Elevation region analysis | Zone the wall (existing `_region_bands` logic) |
| Hatch region partially covering wall | Polygon intersection < 100% | Allocate proportionally, mark Review |
| Code without substrate | `_infer_substrate()` returns "" | Status = "Review" — substrate unknown |
| Substrate without finish | `_infer_finish()` returns "" | Status = "Provisional" — finish unknown |
| Specification conflicts with drawing | Cross-reference check | Review — surface both references |

### 9.2 Status Model

Reuse existing status vocabulary from `pb_substrate_qa_v131.py`:

```python
STATUS_COLOURS = {
    "confirmed": "#2E8B57",    # Green — estimator verified
    "probable": "#D7A21B",     # Amber — high confidence auto-detection
    "needs_check": "#D4553D",  # Red — requires review
    "conflict": "#B33A3A",     # Dark red — conflicting evidence
    "unreviewed": "#8993A1",   # Grey — not yet reviewed
}
```

Extended for Priority 4:

| Status | Meaning | Takeoff behaviour |
|---|---|---|
| `confirmed` | Estimator verified substrate/finish | Include in takeoff with full confidence |
| `probable` | Auto-detected with strong evidence | Include in takeoff, mark as provisional |
| `needs_check` | Detected but uncertain | Include in takeoff, mark as Review |
| `conflict` | Conflicting substrate/finish evidence | Include in takeoff, mark as Review + show conflict |
| `unreviewed` | Not yet classified | Include in takeoff with generic "To confirm" substrate |

### 9.3 Protection of Measured Quantities

**Critical rule: Substrate/finish detection must not silently change authoritative geometry.**

- Priority 4 classifies/allocates measured surfaces
- It does NOT invent extra m² because a hatch was detected
- `authoritative measured surface` × `substrate/finish allocation` = take-off breakdown
- If substrate allocation is uncertain: retain geometry, mark Review, do not discard quantity

---

## 10. Benchmark Strategy

### 10.1 Existing Benchmark Infrastructure

- `test_planreader_accuracy.py`: Tests measurement aggregation, floor-area imports, rate calculations
- `test_accuracy_v13_engines_v145.py`: Tests topology, facade reconciliation, semantic assignment
- `test_substrate_qa_v131.py`: Tests page grouping, schema, surface ID stability
- `test_material_schedule_v1222.py`: Tests schedule parsing, code recognition, conflict detection
- `test_legend_register_v1227.py`: Tests legend page identification, abbreviation expansion

### 10.2 Required New Benchmarks (Phase B)

| Benchmark | Input | Expected output |
|---|---|---|
| Simple filled rectangle | PDF with one red filled rectangle | 1 SurfaceEvidence, fill_colour=(1,0,0), area_m2=X |
| Two substrates on one wall | PDF with two coloured zones | 2 SurfaceEvidence, correct substrate assignment |
| Hatch-only region | PDF with 45° diagonal strokes | 1 SurfaceEvidence, hatch_angle=45, hatch_signature |
| Same colour, different codes | Two red zones, legend says PT01 and PT02 | 2 SurfaceEvidence, correct code disambiguation |
| Legend swatch association | Legend with coloured swatch + code text | Swatch colour → code mapping |
| Room finish schedule | Floor plan with room labels + finish codes | Per-room substrate/finish allocation |
| External elevation with render + FC | Elevation with two hatch patterns + callouts | 2 substrate zones, correct net areas |
| Overlapping finishes | Two hatch patterns overlapping | Review status, conflict flagged |
| Unclassified region | Room polygon with no code/hatch | "To confirm" substrate, geometry retained |

### 10.3 Ground Truth Data

Priority 4 requires seeded PDF test fixtures with known:
- Fill polygons (colour, position, area)
- Hatch patterns (angle, spacing, coverage)
- Legend entries (code → colour/pattern mapping)
- Schedule entries (code → description/substrate/finish)
- Expected allocation results

These should be synthetic PDFs created with PyMuPDF for deterministic testing, similar to the pattern used in `pymupdf_drawings_test.py`.

---

## 11. Production Takeoff Requirements

### 11.1 Internal Breakdown

```
Section: Internal
├── Floor area
│   ├── Level 1 / Unit A / 45.0 m² / PT01 / Plasterboard walls / Dulux White
│   ├── Level 1 / Unit B / 52.0 m² / PT01 / Plasterboard walls / Dulux White
│   └── Level 2 / Unit A / 45.0 m² / PT01 / Plasterboard walls / Dulux White
├── Ceilings
│   ├── Level 1 / All units / 97.0 m² / Ceiling white / Plasterboard ceiling
│   └── Level 2 / All units / 45.0 m² / Ceiling white / Plasterboard ceiling
├── Internal walls
│   ├── Level 1 / Unit A / 38.0 m² / PT01 / Plasterboard / Dulux Low Sheen
│   └── Level 1 / Unit B / 42.0 m² / PT01 / Plasterboard / Dulux Low Sheen
├── Wet area walls
│   ├── Level 1 / Unit A / 12.0 m² / PT02 / FC sheet / Dulux Wet Area
│   └── Level 1 / Unit B / 12.0 m² / PT02 / FC sheet / Dulux Wet Area
├── Doors / frames
│   ├── Level 1 / 8 No. / PT03 / Timber / Dulux Enamel
│   └── Level 2 / 4 No. / PT03 / Timber / Dulux Enamel
└── Skirting / architraves
    └── All levels / 120 lm / PT03 / Timber / Dulux Enamel
```

### 11.2 External Breakdown

```
Section: External
├── Rendered walls
│   ├── North / Level 1 / 52.0 m² / EXT01 / Render / Dulux Exterior
│   ├── South / Level 1 / 48.0 m² / EXT01 / Render / Dulux Exterior
│   └── East / Level 1 / 35.0 m² / EXT01 / Render / Dulux Exterior
├── FC cladding
│   ├── North / Level 2 / 28.0 m² / EXT02 / Fibre Cement / Dulux exterior
│   └── South / Level 2 / 25.0 m² / EXT02 / Fibre Cement / Dulux exterior
├── Soffits / eaves
│   ├── North / 18.0 m² / FCS1 / Soffit / Dulux Exterior
│   └── South / 16.0 m² / FCS1 / Soffit / Dulux Exterior
├── Feature timber
│   └── Entry / 4.5 m² / WF1 / Timber / Clear sealer system
└── Balustrades
    └── Balconies / 12.0 m² / BAL1 / Metal / Dulux Metal Primer + finish
```

### 11.3 Key Data Fields Required

The PB takeoff module already has the right column structure. Priority 4 adds:

| Field | Source | Already exists? |
|---|---|---|
| `substrate` | Schedule lookup / geometry detection | ✅ In takeoff_rows |
| `finish_system` | Schedule lookup | ✅ In takeoff_rows |
| `finish_code` | Schedule lookup / legend lookup | ✅ In PB takeoff rows |
| `element` | Context classification | ✅ In takeoff_rows |
| `section` | Context classification | ✅ In takeoff_rows |
| `level` | Page/level detection | ✅ In PB takeoff rows |
| `location` | Room/elevation reference | ✅ In takeoff_rows |
| `coating_preparation` | PB takeoff builder | ✅ In PB takeoff rows |

Priority 4's contribution is not new columns but **populating substrate/finish more accurately** by using geometry-based detection instead of purely text-based inference.

---

## 12. Existing Code Reuse Opportunities

### 12.1 Directly Reusable

| Component | Module | Use in Priority 4 |
|---|---|---|
| `_point_in_polygon()` | `pb_accuracy_v13_engines_v145.py`, `pb_room_face_takeoff.py`, `pb_height_evidence_v150.py` | Polygon containment for fill→room/wall association |
| `_polygon_area()` | `pb_accuracy_v13_engines_v145.py`, `pb_room_face_takeoff.py` | Area calculation for fill polygons |
| `_count_other_centroids_inside_candidate()` | `pb_room_face_takeoff.py` | Containment hierarchy analysis |
| `facade_net_area()` | `pb_accuracy_v13_engines_v145.py` | Gross→net area by substrate |
| `semantic_assignment()` | `pb_accuracy_v13_engines_v145.py` | Code→scope resolution |
| `CODE_RE` | `pb_material_schedule_v1222.py` | Finish code recognition |
| `parse_schedule_text()` | `pb_material_schedule_v1222.py` | Schedule parsing |
| `_infer_substrate()` | `pb_material_schedule_v1222.py` | Keyword-based substrate inference |
| `extract_native_page()` | `pb_vector_geometry_v130.py` | Starting point for fill extraction (needs extension) |
| `split_segments_at_intersections()` | `pb_accuracy_v13_engines_v145.py` | Line graph construction |
| `extract_planar_faces()` | `pb_accuracy_v13_engines_v145.py` | Face extraction from linework |
| Status colours | `pb_substrate_qa_v131.py` | QA status visualisation |
| `_region_bands()` | `pb_elevation_regions_v1226.py` | Elevation zone construction |

### 12.2 Needs Extension

| Component | Module | Extension needed |
|---|---|---|
| `extract_native_page()` | `pb_vector_geometry_v130.py` | Must extract filled polygons, not just edge segments |
| `_page_occurrences()` | `pb_material_schedule_v1222.py` | Must associate code occurrences with polygons |
| `assign_substrates()` | `pb_registered_substrates_v138.py` | Must accept polygon-based evidence, not just text-based |
| `_region_bands()` | `pb_elevation_regions_v1226.py` | Must work with hatch-detected substrates, not just callout positions |

---

## 13. Proposed Phase B Architecture

### 13.1 New Module: `pb_fill_hatch_extraction.py`

Responsibilities:
- Extract filled polygons from `get_drawings()`
- Group sequential line items into closed paths
- Detect fill-only vs stroke-only vs fill+stroke drawings
- Reconstruct polygon vertices from item sequences
- Classify fills by colour (RGB clustering)

### 13.2 New Module: `pb_hatch_pattern_detection.py`

Responsibilities:
- Detect repeated parallel/crossed strokes
- Calculate hatch angle, spacing, and signature
- Group hatch strokes by visual similarity
- Compute bounding regions for hatch groups
- Match hatch patterns to legend swatches

### 13.3 New Module: `pb_surface_finish_association.py`

Responsibilities:
- Associate fill polygons / hatch regions with measured geometry (rooms, walls, elevations)
- Apply containment hierarchy (full containment > majority overlap > centroid > proximity)
- Look up codes from material dictionary
- Produce `SurfaceEvidence` records
- Allocate substrate/finish to take-off rows

### 13.4 Extension: `pb_vector_geometry_v130.py`

Modify `extract_native_page()` to:
- Group items by drawing index (preserve drawing-level grouping)
- Extract filled rectangles as polygons (not just 4 edges)
- Extract filled paths as polygons (sequential line items with fill)
- Store polygon geometry alongside edge segments
- Preserve `fill_colour` and `hatch_signature` at polygon level

### 13.5 Extension: `pb_material_schedule_v1222.py`

Add:
- Legend swatch detection (colour + code association)
- Hatch pattern → code mapping
- Code occurrence → polygon containment association

### 13.6 Extension: `pb_premier_takeoff_v1225.py`

Enhance to:
- Accept `SurfaceEvidence` records as input
- Populate `substrate`, `finish_code`, `colour_finish` from geometry-based detection
- Break down wall/ceiling areas by substrate type
- Produce per-substrate take-off rows

---

## 14. Exact Files/Functions Phase B Should Modify

### 14.1 New Files to Create

| File | Purpose |
|---|---|
| `pb_fill_hatch_extraction.py` | PDF fill polygon and hatch stroke extraction |
| `pb_hatch_pattern_detection.py` | Hatch pattern analysis and signature generation |
| `pb_surface_finish_association.py` | Geometry→code→substrate/finish association pipeline |
| `tests/test_fill_hatch_extraction.py` | Tests for fill/hatch extraction |
| `tests/test_hatch_pattern_detection.py` | Tests for hatch pattern detection |
| `tests/test_surface_finish_association.py` | Tests for association pipeline |

### 14.2 Existing Files to Modify

| File | Function/Area | Change |
|---|---|---|
| `pb_vector_geometry_v130.py` | `extract_native_page()` | Add polygon extraction alongside edge extraction |
| `pb_material_schedule_v1222.py` | New functions | Legend swatch detection, code→polygon association |
| `pb_elevation_regions_v1226.py` | `build_regions()` | Accept hatch-detected substrates |
| `pb_registered_substrates_v138.py` | `assign_substrates()` | Accept polygon-based evidence |
| `pb_premier_takeoff_v1225.py` | `build_pb_schedule()` | Accept SurfaceEvidence for substrate/finish |
| `pb_planreader_v126_app.py` | Startup chain | Wire new modules |
| `pb_planreader_reconstruction_v139_app.py` | Reconstruction chain | Wire new modules |
| `tests/test_accuracy_v13_engines_v145.py` | New test cases | Benchmarks for fill→substrate allocation |

### 14.3 Files NOT to Modify

| File | Reason |
|---|---|
| `pb_height_evidence_v150.py` | Priority 3 — complete, no substrate logic needed |
| `pb_room_face_takeoff.py` | Priority 2 — complete, geometry only |
| `pb_unified_building_v139.py` | Wall assembly — receives substrate from upstream |
| `pb_planreader_3d_app.py` | Main app — only needs new module wiring |

---

## 15. Dormant/Jobhub Reuse Opportunities

The Jobhub app (separate product) contains mature colour/finish scheduling code directly applicable to Priority 4:

| Component | Location | Use in Priority 4 |
|---|---|---|
| `COLOUR_SCHEDULE_COLUMNS` + `DEFAULT_FINISH_BY_SURFACE` | Jobhub `pb_planreader_app.py` | Colour/finish mapping structure |
| `resolve_colour_hex(colour)` | Jobhub `pb_planreader_app.py` | Colour name → hex conversion |
| `seed_colour_schedule(job)` | Jobhub `pb_planreader_app.py` | Draft colour schedule from take-off rows |
| `parse_colour_schedule_bytes(file_bytes)` | Jobhub `smart_intake.py` | CSV/XLSX colour schedule ingestion |
| `sync_colour_schedule(job_id, rows)` | Jobhub `planreader_bridge.py` | DB persistence of colour schedule |
| `coating_system` DB column | Jobhub `database.py` | Coating system storage schema |
| Substrate box editor | Jobhub `planreader_substrate_component` | Draw-and-label substrate boxes on elevations |

The TradeReader family also has wall-type schedule handling and plastering assemblies with `level_of_finish`, but is less directly applicable.

---

## 16. Risks and Regression Points

### 15.1 Regression Risks

| Risk | Mitigation |
|---|---|
| Modifying `extract_native_page()` breaks existing line/rect extraction | Add polygon extraction as NEW output field; do not remove existing `segments` list |
| New modules slow down processing | Lazy-load fill extraction; process only selected pages |
| Hatch detection produces false positives | Require minimum stroke count and angle consistency; mark uncertain as Review |
| Colour clustering misclassifies similar colours | Use perceptual colour distance (CIEDE2000 or simple Euclidean with tolerance) |
| Containment logic assigns wrong substrate | Use layered containment (full > majority > centroid); Review ambiguous cases |
| Schedule text parsing changes break existing tests | Do not modify `parse_schedule_text()` core; add new functions alongside |
| Fill extraction produces huge polygon lists | Limit to fills within calibrated page area; reject fills outside known geometry bounds |

### 15.2 Key Regression Test Points

| Test | What it protects |
|---|---|
| `test_accuracy_v13_engines_v145.py` | Topology, facade reconciliation, semantic assignment |
| `test_material_schedule_v1222.py` | Schedule parsing, code recognition |
| `test_legend_register_v1227.py` | Legend page identification |
| `test_room_face_takeoff.py` | Room polygon extraction (must not be affected) |
| `test_height_evidence_v150.py` | Height evidence pipeline (must not be affected) |
| `test_substrate_qa_v131.py` | Substrate QA schema |
| `test_offline_wall_units.py` | Unit conversion (must not be affected) |

### 15.3 Do Not Break

- **Measurement quantities**: Fill/hatch detection must not change authoritative geometry areas
- **Existing takeoff rows**: New substrate/finish data enriches rows, does not replace quantities
- **Priority 1-3 work**: Height evidence, room faces, unit conversion must be unaffected
- **Startup chain**: New modules must follow existing `apply()` monkey-patch pattern

---

## 16. Summary of Key Findings

1. **The text-based substrate/finish pipeline is mature** — schedules, legends, codes, substrates, coatings are all well-handled
2. **The geometry-based pipeline is completely absent** — no filled polygon detection, no hatch detection, no colour clustering
3. **All coordinate systems are compatible** — PDF points throughout, no conversion needed
4. **PyMuPDF `get_drawings()` provides rich data** — fill, stroke, opacity, dashes, closePath, item grouping — but the current vector module discards most of it
5. **Existing containment infrastructure is reusable** — `_point_in_polygon()`, centroid analysis, polygon area calculation
6. **The PB takeoff module already has the right structure** — it just needs geometry-based substrate/finish input
7. **Elevation regions partially solve the problem** — but only for callout-position-based zoning, not hatch/fill-based
8. **Protection of measured quantities is critical** — Priority 4 classifies, it does not measure
9. **The hatch→substrate mapping is project-specific** — must come from legend/schedule, not from pattern recognition alone
10. **Phase B should create 3 new modules and extend 5 existing ones** — following the established `apply()` monkey-patch pattern
