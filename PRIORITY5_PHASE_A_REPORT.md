# Priority 5 Phase A — Geometric Opening Detection, Door/Window Association & Net Wall Deductions

**Investigation Date:** 2026-08-25  
**Baseline:** `f68102b` (main at merge of Priority 4 Phase B2)  
**Status:** Investigation only — no production changes  

**Review Status:** Amended per ChatGPT Phase A review — seven corrections applied (type mark vs instance, tolerances, quantity rule, deduction gating, count conflicts, louvre scope, dimension basis).

---

---

## 1. Existing Architecture

### 1.1 Opening Pipeline Summary

PlanReader already has a **complete but manually-driven** opening deduction pipeline. The pipeline works but depends almost entirely on estimator manual entry or 3D model rendering, not on geometric PDF extraction.

```
┌─────────────────────────────────────────────────────────────────────┐
│  DETECTION (sparse, mostly manual)                                  │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │ Manual estimator  │  │ v145 detect_     │  │ 3D model_openings│  │
│  │ entry via UI      │  │ openings from    │  │ SQL table (3D    │  │
│  │ (v134 panel)      │  │ candidates       │  │ renderer only)   │  │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘  │
│           │                     │                     │             │
│           ▼                     ▼                     ▼             │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ opening_register_v134 (JSON workspace setting)              │    │
│  │ Fields: id, kind, label, wall_ref, width_m, height_m,      │    │
│  │         quantity, deduct, source_reference, confidence      │    │
│  └────────────────────────┬────────────────────────────────────┘    │
│                           │                                         │
│  GEOMETRY ATTACHMENT      ▼                                         │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ v137 attach_openings()                                      │    │
│  │ Resolves wall_ref → resolved_wall_ref                       │    │
│  │ Derives: offset_m, sill_m, geometry_status, geometry_conf   │    │
│  │ Centre-placement only (no positional evidence)              │    │
│  └────────────────────────┬────────────────────────────────────┘    │
│                           │                                         │
│  WALL MODEL               ▼                                         │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ v139 build_registered_walls()                               │    │
│  │ For each wall:                                              │    │
│  │   gross_m2 = length_m × height_m                            │    │
│  │   deducted = sum(area_m2 where deduct=True)                 │    │
│  │   net_m2 = max(0, gross - deducted)                         │    │
│  │   wall["openings"] = attached opening records               │    │
│  └────────────────────────┬────────────────────────────────────┘    │
│                           │                                         │
│  TAKEOFF SYNC             ▼                                         │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ v141 sync_rows() → takeoff_rows table                       │    │
│  │ quantity = net_m2                                           │    │
│  │ notes = "Gross X m²; selected opening deductions Y m²"      │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 Key Source Files

| File | Version | Role | Opening Handling |
|------|---------|------|-----------------|
| `pb_opening_deductions_v134.py` | 1.3.4 | Opening register + UI panel | CRUD, kind/width/height/deduct toggle, area calc |
| `pb_opening_geometry_v137.py` | 1.3.7 | Geometry attachment | Resolves wall_ref, centre-places openings |
| `pb_unified_building_v139.py` | 1.3.9 | Registered wall model | Computes gross/deducted/net, cuts 3D meshes |
| `pb_full_reconstruction_v141.py` | 1.4.1 | Takeoff sync | Writes net_m2 to takeoff_rows table |
| `pb_accuracy_v13_engines_v145.py` | 1.4.5 | Accuracy engines | `detect_openings()` from candidates, `facade_net_area()` |
| `pb_elevation_registration_v135.py` | 1.3.5 | Elevation registration | `wall_records()` with gross/deducted/net |
| `pb_surface_evidence_v160.py` | 1.6.0 | Surface evidence | Uses `net_m2` from registered walls as measured target area |
| `tradereader_plastering.py` | — | Trade module | Own `openings_m2` column, manual deduction |
| `tradereader_universal_specialist.py` | — | Trade module | Generic `deductions` input |

### 1.3 Active vs Dormant Paths

**Active (connected to takeoff):**
- Manual estimator entry → v134 register → v137 attachment → v139 wall model → v141 takeoff sync
- v145 `detect_openings()` from candidates (but no production code currently feeds it candidates from PDF)
- Tradereader plastering manual opening deductions

**Dormant (exists but not connected to takeoff):**
- `model_openings` SQL table in `pb_planreader_3d_app.py` (DDL L523) — only used by 3D renderer, not by takeoff
- Door/window schedule page identification (`pb_page_registration_v1225.py` L38) — pages are identified but not parsed for dimensions
- `pb_planreader_offline.py` L902-903 — door/window schedule keywords recognized but no extraction
- v145 `detect_openings()` — function exists but no caller feeds it geometric candidates from PDF vectors

**Not implemented:**
- Geometric opening detection from PDF vectors (door swings, wall gaps, jamb pairs)
- Schedule parsing for door/window dimensions
- Elevation opening detection (rectangles inside facade polygons)
- Cross-source reconciliation (plan ↔ elevation ↔ schedule)
- Opening deduplication across sources
- Opening identity model (OpeningEvidence)
- Frame/leaf/reveal scope separation
- Internal vs external opening rules

---

## 2. Current Net-Wall Maths

### 2.1 Where Gross Wall m² Is Calculated

**Primary path (v135 + v139):**

```python
# pb_elevation_registration_v135.py L236
gross = _num(segment.get("length_m")) * default_height

# pb_unified_building_v139.py L77
gross = _num(wall.get("length_m")) * _num(wall.get("height_m"))
```

Both use `length × height`. Length comes from calibrated plan footprint segments. Height comes from elevation registration or a default (2.7 m).

**Secondary path (auto_geometry):**

```python
# pb_auto_geometry_v1219.py L722
facade["gross_m2"] = round((w * h) / (pxpm * pxpm), 2)
```

This uses pixel-to-metric conversion from elevation drawings. This is a separate path from the v135/v139 registered wall model.

### 2.2 Where Openings Are Deducted

**v135 wall_records (L238-245):**
```python
deducted = app.deducted_opening_area_m2(attached)
rows.append({..., "gross_m2": gross, "opening_deduction_m2": deducted, "net_m2": max(0, gross - deducted)})
```

**v139 build_registered_walls (L77-79):**
```python
gross = length * height
deducted = sum(area_m2 for o in attached if deduct)
wall["gross_m2"] = gross; wall["opening_deduction_m2"] = deducted; wall["net_m2"] = max(0, gross - deducted)
```

**v145 facade_net_area (L203-216):**
```python
by[sub]['gross_m2'] += area_m2
by[sub]['deductions_m2'] += area_m2  # for deducted openings matching substrate
by[sub]['net_m2'] = max(0, gross - deductions)
```

### 2.3 Where Opening Confidence/Status Is Stored

- `v134`: `confidence` field (string: "To review", "Manual estimator entry", etc.)
- `v137`: `geometry_confidence` = "Review" (always, since placement is centre-only)
- `v137`: `geometry_status` = "Visual centre placement; position needs elevation evidence"
- `v145`: `geometry_confidence` = 0.97/0.78/0.55 depending on evidence completeness
- `v145`: `semantic_confidence` = 0.95 if tag else 0.75

### 2.4 Fallback/No-Opening Behavior

- `net_wall_area_m2()` returns `max(0, gross - 0)` = gross when no openings
- `v139` sets `opening_deduction_m2 = 0` when no openings are attached
- `v141` notes: "Gross X m²; selected opening deductions 0.00 m²"
- No openings detected → gross wall area flows through unchanged

### 2.5 Quantity x Geometry Inconsistency

**Current issue:** v134 allows a single opening record with `quantity > 1`. v137 multiplies `area_m2` by that quantity. But v139 creates only **one rectangular wall cut per opening record**. So a record for four identical windows mathematically deducts four windows' area while visually cutting one hole in the 3D model.

**Phase B rule:** Automatic geometric evidence must produce one record per physical opening, `quantity = 1`. Grouped commercial allowances remain a v134 estimator concept.

### 2.6 Duplicate Deduction Risk

**Current risk is LOW because there's only one active path.** However:

- `v135 wall_records()` and `v139 build_registered_walls()` both compute gross/deducted/net independently
- If both are called and their results combined, deductions could double-count
- The `pb_planreader_reconstruction_v139_app.py` composition root wires them sequentially to avoid this
- Tradereader plastering has its own independent `openings_m2` column — no automatic link to v134 register

---

## 3. Door Schedule Pipeline

### 3.1 What Exists

- Page registration identifies "Door / Window Schedule" pages (`pb_page_registration_v1225.py` L38)
- Keywords recognized: `"door schedule"`, `"window schedule"`, `"door and window schedule"`, `"door elevations"`, `"window elevations"`
- `pb_planreader_3d_app.py` has a `door_schedule` register (L184) with CRUD UI (L5395-5397)
- The register is a generic key-value store — no structured extraction of width/height/mark

### 3.2 What Can Be Extracted (Investigation Needed)

From typical architectural door schedules:
- **Door mark** (D01, D02, etc.) — present in most schedules
- **Width** (mm) — typically in a column
- **Height** (mm) — typically in a column
- **Count** — sometimes explicit, sometimes one row = one type
- **Type** (single, double, sliding, fire-rated) — text column
- **Glazed/solid** — sometimes a separate column or noted in description
- **Location/floor** — sometimes present, sometimes not
- **Finish** — separate from wall finish, usually paint/stain

### 3.3 What Does NOT Exist

- No structured parsing of door schedule pages
- No extraction of dimensions from schedule tables
- No mark-to-location mapping
- No automatic population of v134 register from schedules

### 3.4 Recommendation

Door schedules should be parsed as a **dimension authority** — when a schedule says "D01: 820 × 2040", that dimension is more reliable than geometric estimation. The schedule provides the mark + dimensions; plan/elevation provides the location.

---

## 4. Window/Glazing Schedule Pipeline

### 4.1 What Exists

- Same page registration as door schedules (combined "Door / Window Schedule")
- `pb_planreader_3d_app.py` L5714 has opening Type selectbox: `["Door", "Window", "Glazed opening", "Roller door", "Louvre", "Other"]`
- Hatch detection (`pb_hatch_detection_v160.py` L90) recognizes `louvre|louver` as hatch keywords (correctly — louvres are hatch patterns, not openings)

### 4.2 What Can Be Extracted

From typical architectural window schedules:
- **Window mark** (W01, W02, etc.)
- **Width × Height** (mm)
- **Type** (casement, sliding, fixed, awning)
- **Glazing type** (single, double, tinted, low-e)
- **Curtain wall/storefront** — typically noted as a system type
- **Count** — sometimes explicit

### 4.3 Deduction Scope Rules (Recommended)

| Opening Type | Reduces Paintable Wall? | Separate Trade Scope? |
|-------------|------------------------|----------------------|
| Standard window | YES — void area | Window frame painting (separate) |
| Door | YES — void area | Door leaf/frame painting (separate) |
| Glazed opening | YES — void area | Glazing cleaning (separate) |
| Curtain wall | MAYBE — depends on system | Usually separate trade |
| Louvre panel (in wall opening) | YES — void area | Louvre panel painting (separate) |
| Louvre pattern (hatch surface) | NO — surface treatment | Louvre painting (separate) |
| Roller door | YES — void area | Door painting (separate) |
| Garage door | YES — void area | Door painting (separate) |
| Shopfront | MAYBE — depends on system | Usually separate trade |

---

## 5. Geometric Opening Detection from Floor Plans

### 5.1 Available Signals in Native PDF Vectors

| Signal | Reliability | Extraction Difficulty | Notes |
|--------|------------|----------------------|-------|
| **Door swing arcs** | HIGH | MEDIUM | Quarter-circle or arc drawn at door leaf. Distinctive geometry. |
| **Door leaf lines** | HIGH | LOW | Single line at ~90° to wall, representing the door panel. |
| **Wall gaps** | MEDIUM | MEDIUM | Break in wall double-lines where a door sits. Must distinguish from open-plan junctions. |
| **Jamb pairs** | HIGH | LOW | Two short perpendicular lines at wall edges marking the opening. |
| **Window double-line symbols** | MEDIUM | MEDIUM | Parallel lines across wall thickness representing glazing. |
| **Glazing line pairs** | MEDIUM | MEDIUM | Two closely-spaced parallel lines within wall. |
| **Opening width between wall segments** | MEDIUM | LOW | Gap between two wall segment endpoints. |
| **Tag/mark text** | HIGH (if present) | LOW | D01, W01 etc. placed near the opening. Already filtered by v145 room_face_takeoff (L109). |

### 5.2 What Can Be Extracted Without OCR/AI

**High confidence (native vectors):**
- Door swing arc detection (arc/curve objects near wall gaps)
- Door leaf line detection (short line perpendicular to wall at gap)
- Jamb pair detection (two short perpendicular lines at wall edges)
- Wall gap detection (break in wall double-line pairs)
- Opening width from wall gap measurement

**Medium confidence (requires heuristics):**
- Window double-line detection (parallel lines crossing wall thickness)
- Distinguishing windows from other wall features
- Distinguishing door swings from furniture/equipment arcs

**Low confidence (needs schedule/elevation cross-check):**
- Opening height (not visible in plan view)
- Opening type (door vs window) from geometry alone
- Glazing type

### 5.3 Recommended Plan Detection Architecture

```
PDF vector objects (lines, curves, arcs)
    │
    ├─ Extract wall double-line segments (existing v145 wall detection)
    │
    ├─ Detect gaps in wall segments (endpoint proximity)
    │   └─ Measure gap width → opening width_m
    │
    ├─ Detect arc objects near gaps
    │   └─ Door swing → kind="door", swing=True
    │
    ├─ Detect perpendicular short lines at gap edges
    │   └─ Jamb pairs → confirm opening, refine width
    │
    ├─ Detect parallel line pairs crossing wall
    │   └─ Window/glazing symbol → kind="window"
    │
    └─ Collect tag text near opening (D01, W01)
        └─ Mark for schedule cross-reference
```

---

## 6. Geometric Opening Detection from Elevations

### 6.1 Available Signals

| Signal | Reliability | Notes |
|--------|------------|-------|
| **Rectangular openings inside facade polygons** | HIGH | Standard architectural representation |
| **Window/door rectangles** | HIGH | Clear geometric shapes within facade |
| **Repeated opening arrays** | HIGH | Identical windows in a row |
| **Curtain wall zones** | MEDIUM | Large glazed areas, often at corners |
| **Voids/holes in wall faces** | MEDIUM | Openings without explicit frames |
| **RL annotations** | HIGH | Sill/head heights written as text |

### 6.2 Width/Height from Elevations

**Elevation geometry is STRONGER than plan geometry for:**
- Opening height (explicitly drawn, often dimensioned)
- Sill height (RL annotations)
- Head height (RL annotations)
- Opening count (all visible on elevation)
- Repeated opening patterns

**Elevation geometry is WEAKER than plan geometry for:**
- Opening position along wall (no wall reference system)
- Which wall the opening belongs to (needs plan cross-reference)
- Opening depth/thickness

### 6.3 Recommended Elevation Detection Architecture

```
Elevation page (identified by v135 orientation detection)
    │
    ├─ Extract facade polygon from v135 registration
    │
    ├─ Detect rectangular shapes inside facade bbox
    │   ├─ Filter by size (opening-sized, not text boxes)
    │   ├─ Filter by position (within facade boundary)
    │   └─ Classify by fill/pattern (glazed vs solid)
    │
    ├─ Extract RL annotations near openings
    │   └─ Sill RL + Head RL → height_m, sill_m
    │
    ├─ Detect repeated arrays
    │   └─ Identical rectangles in row → count
    │
    └─ Cross-reference with plan opening positions
        └─ Match by position along facade length
```

---

## 7. Plan ↔ Elevation ↔ Schedule Reconciliation

### 7.1 Recommended Evidence Hierarchy

```
Schedule dimensions (most reliable for width/height)
    +
Plan location/identity (most reliable for wall_ref, position)
    +
Elevation geometry (most reliable for height, sill, count)
    →
OpeningEvidence (reconciled record)
```

### 7.2 Reconciliation Rules

1. **Schedule is dimension authority** — if schedule says W01 is 1200×1500, use those dimensions
2. **Plan is location authority** — if plan shows W01 on wall N03 at position 5.2m, use that location
3. **Elevation is height authority** — if elevation shows sill at 900mm and head at 2400mm, use that height
4. **Cross-check when possible** — if plan width ≈ schedule width within 5%, confidence increases
5. **Conflict → Review** — if plan width differs from schedule width by >10%, flag for review
6. **Missing source → lower confidence** — if only plan detection (no schedule), confidence = 0.55
7. **Multiple sources → higher confidence** — if schedule + plan + elevation agree, confidence = 0.97

### 7.3 Deduplication Strategy

The same physical window may appear on:
- Floor plan (as wall gap + symbol)
- Elevation (as rectangle)
- Schedule (as row with dimensions)
- Detail drawing (as section)

**Physical-instance identity (not type-mark identity):**

A `type_mark` (W01, D01) identifies a window/door *type*, not a physical instance. The same type mark can appear many times — even on the same wall and level (e.g., four identical W01 windows in a row). Physical identity must use `opening_instance_id` (UUID-based) derived from geometric position and source instance.

**Deduplication: matching across sources (same physical opening seen on plan + elevation + schedule):**

Use explicit tolerance comparisons, not rounding:
```python
def same_opening(a: OpeningEvidence, b: OpeningEvidence) -> bool:
    # Must be same wall and same level
    if a.wall_ref != b.wall_ref: return False
    if a.level != b.level: return False
    # Width and height within 50mm tolerance
    if a.width_m and b.width_m:
        if abs(a.width_m - b.width_m) > 0.05: return False
    if a.height_m and b.height_m:
        if abs(a.height_m - b.height_m) > 0.05: return False
    # Position along wall within 200mm tolerance (plan vs elevation)
    if a.position_along_wall_m is not None and b.position_along_wall_m is not None:
        if abs(a.position_along_wall_m - b.position_along_wall_m) > 0.20: return False
    return True
```

**Deduplication rules:**
1. Same wall_ref + same level + dimensions within tolerance -> same opening (merge evidence)
2. Same wall_ref + same level + position within tolerance -> same opening
3. Different wall_ref or different level -> definitely different openings
4. Otherwise -> uncertain, flag for review

---

## 8. Opening Identity Model (Recommended)

### 8.1 OpeningEvidence Record

```python
@dataclass
class OpeningEvidence:
    # Identity
    opening_instance_id: str     # unique per-physical-opening ID (UUID-based)
    type_mark: str               # D01, W01 — the TYPE mark, NOT physical identity.
                                 # A type mark can repeat many times: multiple
                                 # identical windows on the same wall, same level.
    workspace_id: int
    page_id: Optional[int]       # primary source page
    
    # Location
    wall_ref: str                # resolved wall reference
    level: str                   # floor/storey (Ground, First, etc.)
    room_ref: str                # adjacent room (if known)
    elevation_side: str          # North/South/East/West
    position_along_wall_m: float # distance from wall start (geometric centre)
    
    # Type
    opening_type: str            # "door", "window", "glazed_opening", etc.
    
    # Quantity (always 1 for geometric evidence; grouped counts are commercial)
    quantity: int                # MUST be 1 for auto-detected geometric evidence.
                                 # Manual grouped allowances (e.g. "4x identical
                                 # windows") are a separate commercial concept
                                 # handled by v134, not geometric evidence.
    
    # Dimensions
    width_m: Optional[float]
    height_m: Optional[float]
    dimension_basis: str         # "rough_opening" | "frame" | "leaf" |
                                 # "clear_opening" | "unknown"
                                 # For wall deduction we need the wall void.
                                 # Schedule dimensions may be nominal frame,
                                 # leaf size, structural opening, or rough
                                 # opening. Unknown basis -> lower confidence.
    sill_m: float                # 0.0 for doors, 0.9 for windows (default)
    area_m2: Optional[float]     # computed: width x height x quantity
    
    # Geometry
    plan_geometry: Optional[Dict]    # bbox/polygon from floor plan
    elevation_geometry: Optional[Dict] # bbox from elevation drawing
    source_bbox: Optional[Tuple]     # PDF bounding box
    
    # Evidence sources
    schedule_ref: str            # schedule page/mark reference
    extraction_method: str       # "plan_vector", "elevation_rect", "schedule_parse", "manual"
    
    # Confidence
    geometry_confidence: float   # 0.0-1.0
    dimension_confidence: float  # 0.0-1.0
    association_confidence: float # 0.0-1.0
    
    # Status
    deduction_status: str        # "deducted", "not_deducted", "review"
    evidence: List[str]          # source references
    notes: str
```

### 8.2 Critical: One Record Per Physical Opening

Geometric evidence must create **one OpeningEvidence record per physical opening**, with `quantity = 1`. This is because:
- v137 currently multiplies `area_m2` by quantity, but v139 creates only ONE rectangular wall cut per record
- A single record with `quantity = 4` would mathematically deduct four windows while visually cutting one hole
- Automatic geometric detection cannot group openings — it sees individual physical instances

**Grouped commercial allowances** (e.g., "4x W01 on North elevation") remain a v134 estimator concept. The estimator can create a single v134 record with `quantity = 4` for pricing; geometric evidence should not do this.

### 8.3 Why Not Implement Schema Yet

The existing v134 register already stores the commercial fields (kind, width, height, deduct). The v145 `detect_openings()` already returns a richer record. Phase B should:
1. Extend v145's output format to include the new fields
2. Keep v134 as the commercial layer (estimator toggle)
3. Add a new `opening_evidence_vXXX` module for the geometric evidence layer

---

## 9. Wall Association Strategy

### 9.1 Available Methods (Ranked by Reliability)

| Method | Reliability | When Available |
|--------|------------|----------------|
| **Schedule location note** | HIGH | "North elevation, bay 3" in schedule |
| **Mark-to-wall mapping** | HIGH | If mark appears on plan near wall |
| **Plan gap alignment** | HIGH | Opening gap aligns with wall segment endpoint |
| **Centreline projection** | MEDIUM | Opening centre projects to wall centreline |
| **Facade containment** | MEDIUM | Opening bbox inside facade polygon (elevation) |
| **Room adjacency** | LOW | Opening near room boundary |
| **Nearest-wall** | LOW | Fallback only |

### 9.2 Recommended Approach

1. **Primary:** Use plan gap position → wall segment matching (geometric intersection)
2. **Secondary:** Use elevation facade containment → wall side matching
3. **Tertiary:** Use mark text proximity → wall reference matching
4. **Fallback:** Nearest wall with distance threshold (only when confidence < 0.7)

### 9.3 Avoid

- Nearest-wall-only association without geometric evidence
- Associating an opening to a wall that doesn't geometrically contain it
- Assuming every scheduled opening appears on every wall

---

## 10. Duplicate Prevention

### 10.1 Sources of Duplicates

| Source | Duplicate Risk | Mitigation |
|--------|---------------|------------|
| Plan detection + elevation detection | HIGH | Geometry position matching |
| Plan detection + schedule row | HIGH | Mark matching |
| Elevation detection + schedule row | MEDIUM | Mark matching + dimension check |
| Multiple elevation drawings | MEDIUM | Orientation-based dedup |
| Detail drawing + plan | LOW | Scale/position check |

### 10.2 Deterministic Deduplication

```python
def dedup_key(opening: OpeningEvidence) -> Tuple:
    """Deterministic deduplication key."""
    return (
        opening.mark.upper() if opening.mark else "",
        opening.wall_ref,
        round(opening.width_m or 0, 2),   # 50mm tolerance
        round(opening.height_m or 0, 2),   # 50mm tolerance
    )

def merge_openings(existing: OpeningEvidence, new: OpeningEvidence) -> OpeningEvidence:
    """Merge evidence from duplicate openings. Keep highest-confidence values."""
    merged = existing
    # Merge evidence sources
    merged.evidence = list(set(existing.evidence + new.evidence))
    # Upgrade confidence if new source confirms
    merged.geometry_confidence = max(existing.geometry_confidence, new.geometry_confidence)
    merged.dimension_confidence = max(existing.dimension_confidence, new.dimension_confidence)
    # Prefer schedule dimensions over geometric estimation
    if new.extraction_method == "schedule_parse":
        merged.width_m = new.width_m or existing.width_m
        merged.height_m = new.height_m or existing.height_m
    return merged
```

---

## 11. Deduction Authority Rules

### 11.1 Critical Rules

1. **No blind subtraction from text counts alone** — a schedule saying "12 doors" does not automatically deduct 12 doors from walls
2. **Opening evidence may change net wall m² ONLY when:**
   - Association to a specific wall is confirmed (association_confidence ≥ 0.7)
   - Dimensions are known (dimension_confidence ≥ 0.7)
   - The estimator has not unticked the deduct checkbox
3. **Uncertain opening:**
   - Retain gross wall area
   - Flag opening for Review
   - Do NOT silently deduct
4. **Schedule-only openings:**
   - Create an OpeningEvidence record
   - Set deduction_status = "review"
   - Wait for plan/elevation confirmation before deducting
5. **Deduction gating is a Phase B0 safety contract, not a Phase B5 afterthought.** Detection (B1-B3) must initially create evidence only. An uncertain opening must never alter net m2. The `deduct` field defaults to `False` for all auto-detected evidence — only confirmed, wall-associated, dimension-known instances may set `deduct = True`.

### 11.2 Confidence Thresholds

| Confidence Level | Behavior |
|-----------------|----------|
| ≥ 0.9 | Auto-deduct (if estimator allows) |
| 0.7 – 0.9 | Deduct with "Derived" status |
| 0.5 – 0.7 | Flag for Review, do not deduct |
| < 0.5 | Record existence only, no deduction |

---

## 12. Frame/Leaf/Reveal Scope Separation

### 12.1 What the Wall Deduction Covers

The wall m² deduction is the **opening void area** — the hole in the wall where the door/window sits. This is:
- `width_m × height_m` (the clear opening)
- Not the frame, not the leaf, not the glass

### 12.2 Separate Trade Scopes

| Component | Trade | Quantity | Notes |
|-----------|-------|----------|-------|
| Opening void | Wall painter | Deducted from wall m² | The hole |
| Door leaf | Joiner/painter | m² or per-item | Paintable surface area |
| Door frame | Joiner/painter | Linear m or per-item | Architrave/frame painting |
| Window frame | Painter | Linear m or per-item | Frame painting |
| Glazing | Glazier | m² | Not paintable |
| Reveal | Plasterer/painter | Linear m × reveal depth | Internal reveal surfaces |
| Soffit/head | Painter | Linear m × depth | Top of opening |
| Jamb | Plasterer/painter | Linear m × reveal depth | Sides of opening |

### 12.3 Current State

- v134 only handles the void deduction
- No separate tracking of frame/leaf/reveal quantities
- Tradereader plastering has an `openings_m2` column but no frame tracking
- Jobhub (dormant) has door frame/window frame paint allowance logic

### 12.4 Recommendation

Phase B should:
1. First: Get void deductions working reliably (the main goal)
2. Later: Add frame/leaf/reveal as separate quantity lines (separate priority)

---

## 13. Internal vs External Logic

### 13.1 Current State

- v135 elevation registration only handles external facades
- v139 registered walls are external walls only
- Internal walls are handled by room face takeoff (v145/pb_room_face_takeoff.py)
- Internal openings (doors between rooms) are NOT currently deducted from anything

### 13.2 Recommended Rules

| Opening Type | External Wall? | Internal Wall? | Deduction Rule |
|-------------|---------------|---------------|----------------|
| External window | YES | N/A | Deduct from external wall m² |
| External door | YES | N/A | Deduct from external wall m² |
| Internal door | N/A | RARELY needed | Usually not deducted (paint goes around frame) |
| Internal glazing | N/A | Sometimes | Deduct if significant |
| Curtain wall | MAYBE | N/A | Depends on system |

### 13.3 Phase B Scope

Phase B should focus on **external openings only** (matching v135/v139 scope). Internal opening deductions are a separate concern.

---

## 14. Multi-Storey / Repeated Openings

### 14.1 Risks

- Identical unit layouts on multiple floors → same schedule marks repeated
- Stacked windows → same position on each floor
- Mirrored units → same marks, different orientation

### 14.2 Deduplication Rules

1. **Same physical opening detected on plan + elevation** -> merge into one instance
2. **Same type mark on same wall on different floors** -> one instance per floor (level-based)
3. **Same type mark on mirrored unit** -> different wall_ref (orientation-based)
4. **Schedule count vs plan count conflict** -> report `count_conflict`, require reconciliation. Do NOT silently choose the lesser number — that hides a source error

### 14.3 Level Awareness

- v135 tracks `level_name` per wall segment
- Opening association should include level matching
- A window marked "W01" on Ground Floor is different from "W01" on First Floor

---

## 15. Benchmark Strategy

### 15.1 Existing Targets

| Metric | Current Target | Source |
|--------|---------------|--------|
| door_count | 10 (level-1) | `test_accuracy_engine_v130.py` L85 |
| window_count | — | `pb_accuracy_benchmark_v130.py` L21 |

### 15.2 Recommended New Fixtures

**Not modifying existing benchmarks.** New seeded fixtures for opening-specific testing:

| Fixture | Description | Expected |
|---------|-------------|----------|
| `one_wall_one_door` | 10m wall + 1 door (0.9×2.1) | gross=27, deduct=1.89, net=25.11 |
| `one_wall_one_window` | 10m wall + 1 window (1.2×1.5) | gross=27, deduct=1.8, net=25.2 |
| `door_and_window_same_wall` | 10m wall + door + window | gross=27, deduct=3.69, net=23.31 |
| `repeated_windows` | 10m wall + 4× identical windows | gross=27, deduct=7.2, net=19.8 |
| `curtain_wall` | 8m wall + 6m curtain wall | gross=21.6, deduct=varies, review flag |
| `schedule_mark_crosscheck` | Schedule W01=1200×1500, plan shows W01 on N03 | Mark matches, dimensions from schedule |
| `plan_elevation_mismatch` | Plan shows 1200 wide, elevation shows 1400 wide | Conflict → review |
| `duplicate_across_sheets` | Same W01 on plan sheet and elevation sheet | Dedup to ONE opening |
| `unknown_height` | Schedule W02 width only, no height | dimension_confidence < 0.7, no deduction |
| `uncertain_not_deducted` | Opening detected but association uncertain | gross=27, no deduction, review flag |
| `gross_30_minus_6` | 30m² gross - 6m² openings | net=24m² |

---

## 16. Production Architecture (Phase B Recommendation)

### 16.1 Integration Points

Phase B should integrate with EXISTING infrastructure:

```
Priority 1 (v1219/v1222) — Calibration + material schedule
    └─ Provides: px_per_m, scale, finish codes
    
Priority 2 (v1225/v1226) — Registration + page classification
    └─ Provides: page types (door schedule, window schedule, elevation)
    
Priority 3 (v150) — Height evidence
    └─ Provides: wall heights, RL values, sill/head heights
    
Priority 4 (v160) — Surface evidence + hatch detection
    └─ Provides: surface geometry, hatch patterns (louvre exclusion)
    
v135 — Elevation registration
    └─ Provides: facade sides, wall segments, wall refs
    
v139 — Registered wall model
    └─ Consumer: Opening deductions flow INTO v139 wall model
```

### 16.2 Revised Phase B Order

```
B0 — OpeningEvidence contract + safety rules
     Define OpeningEvidence, separate instance ID from type mark,
     explicit tolerances, per-instance quantity=1, dimension_basis,
     evidence provenance, confidence calculation, deduction gating.
     Add seeded tests FIRST. No production changes.
     
B1 — Plan vector candidate detection
     Door swings, jamb pairs, wall gaps, glazing pairs, nearby tags.
     Output EVIDENCE CANDIDATES ONLY. No take-off changes.

B2 — Door/window schedule parsing
     Parse marks, dimensions, descriptions, counts as schedule evidence.
     Schedule rows describe opening TYPES, not physical wall instances.

B3 — Elevation candidate detection
     Detect candidates inside registered facades. Do NOT treat
     "rectangle inside facade" alone as high-confidence — facades
     contain panels, grid lines, annotation boxes.

B4 — Reconciliation + wall association
     Merge plan/elevation/schedule evidence using explicit tolerance
     checks and per-instance identity.

B5 — Controlled deduction integration
     ONLY NOW feed confirmed instances into v134/v137/v139.
     High-confidence + wall-associated + dimension-known → deduct.
     Everything else → Review, gross m² untouched.

B6 — Benchmark verification
     Prove exact opening count, deduction m², final net wall m²
     on seeded fixtures and real benchmark drawings.
```

**Core safety rule:** PlanReader can miss an uncertain deduction and flag it for review, but it must never confidently subtract an opening it cannot prove exists and belongs to that wall.

### 16.3 New Module Structure

```
pb_opening_evidence_vXXX.py       — OpeningEvidence dataclass + safety contract (B0)
pb_opening_detection_vXXX.py      — Geometric detection from PDF vectors (B1, B3)
pb_opening_schedule_vXXX.py       — Schedule parsing for dimensions (B2)
pb_opening_reconciliation_vXXX.py — Cross-source dedup and confidence (B4)
```

### 16.4 Files to Modify (Phase B)

| File | Change |
|------|--------|
| `pb_opening_deductions_v134.py` | Extend register with new fields (mark, geometry_confidence, etc.) |
| `pb_opening_geometry_v137.py` | Replace centre-placement with geometric positioning |
| `pb_unified_building_v139.py` | Consume new OpeningEvidence for deduction |
| `pb_accuracy_v13_engines_v145.py` | Feed `detect_openings()` with real geometric candidates |
| `pb_elevation_registration_v135.py` | Add elevation opening detection |
| `pb_page_registration_v1225.py` | Ensure door/window schedule pages are correctly classified |
| `pb_plan_read_engine_v1228.py` | Add schedule parsing capability |

### 16.5 Do NOT Create

- Do NOT create a parallel wall engine
- Do NOT modify v139's wall model structure (it's working)
- Do NOT change the v134 deduct toggle mechanism (estimator control is correct)
- Do NOT auto-deduct without confidence thresholds

---

## 17. Risks and Regression Points

### 17.1 High-Risk Areas

| Risk | Mitigation |
|------|-----------|
| Changing v134 register format breaks existing saved openings | Version the register, auto-migrate old format |
| New opening detection creates false deductions | Confidence thresholds + estimator toggle |
| Double-counting openings (plan + elevation) | Deduplication before deduction |
| Breaking v139 wall model | Test with existing fixtures unchanged |
| Breaking v141 takeoff sync | Test takeoff_rows output unchanged |
| Breaking tradereader plastering | No changes to tradereader modules |

### 17.2 Regression Test Points

- `test_opening_deductions_v134.py` — must pass unchanged
- `test_accuracy_v13_engines_v145.py` — must pass unchanged
- `test_full_reconstruction_v141.py` — must pass unchanged
- `test_height_evidence_v150.py` — must pass unchanged
- `test_elevation_registration_v135.py` — must pass unchanged
- `test_surface_evidence_v160.py` — must pass unchanged
- Full suite: 673 passing, 8 pre-existing failures, 0 regressions

---

## 18. Summary

### What Exists
- Complete manual opening pipeline (v134 → v137 → v139 → v141)
- Estimator deduct toggle (commercial control preserved)
- v145 `detect_openings()` function (exists but no production callers)
- Door/window schedule page identification (pages found but not parsed)
- Elevation registration with wall segments and refs

### What's Missing
- Geometric opening detection from PDF vectors (plan + elevation)
- Schedule dimension parsing
- Cross-source reconciliation and deduplication
- Opening identity model
- Wall association beyond label matching
- Frame/leaf/reveal scope separation
- Confidence-based deduction authority
- Level-aware multi-storey handling

### Phase B Priority Order
1. **Plan geometric opening detection** (door swings, wall gaps, jamb pairs)
2. **Schedule parsing** (door/window dimensions from schedule pages)
3. **Elevation opening detection** (rectangles inside facade polygons)
4. **Cross-source reconciliation** (dedup + confidence scoring)
5. **Wall association** (geometric intersection + facade containment)
6. **Deduction authority** (confidence thresholds + estimator control)

---

*Report generated by OpenCode Phase A investigation. No production changes made.*
