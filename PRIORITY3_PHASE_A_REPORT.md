# Priority 3 — Phase A Investigation Report

## 1. Where default wall heights are currently used

### Primary source: 2.7 m hardcoded everywhere

| Location | Code | Context |
|---|---|---|
| `pb_planreader_3d_app.py:1434` | `WALL_HEIGHT_M = 2.7` | Module constant — external/cladding measurement-line sync |
| `pb_planreader_3d_app.py:3248` | `default_wall_height_m` setting fallback `2.7` | Workspace settings resolver — consumed by all downstream |
| `pb_planreader_3d_app.py:6365` | `number_input("Default wall height (m)", value=2.7)` | UI Settings input |
| `pb_planreader_3d_app.py:468` | `wall_height_m REAL DEFAULT 2.7` | DB schema: `mapped_zones` |
| `pb_planreader_3d_app.py:489` | `height REAL DEFAULT 2.7` | DB schema: `model_masses` |
| `pb_planreader_3d_app.py:533` | `height REAL DEFAULT 2.1` | DB schema: `model_openings` (door height) |
| `pb_takeoff_accuracy_v125.py:226` | `height=max(.1,...,"default_wall_height_m",2.7)` | `measured_qty()` — perimeter×height takeoff |
| `pb_takeoff_accuracy_v125.py:310` | same | `auto_envelope()` — auto-detected envelope rows |
| `pb_elevation_registration_v135.py:232` | `default_height = max(0.5, ..., 2.7)` | `wall_records()` — provisional wall area |
| `pb_elevation_profile_v136.py:59` | `default=max(.5,...,2.7)` | `solve_height_from_text()` — fallback |
| `pb_unified_building_v139.py:67` | `height=max(.5, profile.height_m or row.height_m or 2.7)` | `build_registered_walls()` — chained fallback |
| `pb_unified_building_v139.py:86` | `height=_num(wall.get("height_m"),2.7)` | Wall mesh cells |
| `pb_roof_envelope_v140.py:38` | `max_h=max([_num(w.get("height_m"),2.7)...)` | Roof cap z-height |
| `pb_render_resilience_v143.py:41` | `h=max(0.1,_num(row.get("height_m") or row.get("height"),2.7))` | Render mesh fallback |
| `pb_precision_3d_v132.py:132` | `default_height` fallback 2.7 | Level stacking z-index |
| `pb_3d_quickstart_v1213.py:82` | `max(0.1, _num(zone.get("wall_height_m"), 2.7))` | Quick-start mass INSERT |
| `pb_performance_v1215.py:216` | same | Performance batch INSERT |
| `pb_auto_geometry_v1219.py:777` | `height = median(heights) if heights else 2.7` | Auto model refresh — facade median or fallback |
| `pb_planreader_3d_app.py:5628` | `number_input("Wall / extrusion height (m)", value=2.7)` | Zone editor UI default |

### Secondary: 3.0 m provisional storey height

| Location | Code | Context |
|---|---|---|
| `pb_autopilot_v1223.py:505` | `storey_height = 3.0` | Provisional storey height when no elevation data |
| `pb_elevation_profile_v136.py:49,52` | `min(diffs, key=lambda x: abs(x-3.0))` | RL/dimension solver anchor (NOT a default — picks closest to 3.0) |

### Opening heights: 2.1 m default

| Location | Code | Context |
|---|---|---|
| `pb_planreader_3d_app.py:533` | DB schema `height REAL DEFAULT 2.1` | Door height default |
| `pb_planreader_3d_app.py:5667,3061,3135` | `to_float(row.get("height"), 2.1)` | Opening INSERT fallbacks |
| `pb_3d_realistic_renderer.py:616` | `parseFloat(opening.height) \|\| 2.1` | JS renderer fallback |

---

## 2. Every hardcoded height value

| Value | Meaning | Where |
|---|---|---|
| **2.7** | Default wall/ceiling height | 17+ locations (see §1) |
| **3.0** | Provisional storey height; RL/dim solver anchor | `pb_autopilot_v1223.py:505`, `pb_elevation_profile_v136.py:49,52` |
| **2.1** | Default door height | `pb_planreader_3d_app.py:533`, renderer JS |
| **2.4** | Not found as wall height (only as board length 2400mm in tradereader) | — |
| **2700/2400/3000** | Not hardcoded as heights; only appear in dimension parser input text | — |

---

## 3. Which take-off calculations consume height

### Five independent wall-area paths → takeoff_rows

| Path | Height source | Calculation | Status |
|---|---|---|---|
| **A. Elevation bbox** (`v1219._build_facade_rows`) | Measured (bbox_h ÷ px_per_m) | `gross_m2 = w_px × h_px / pxpm²` | Provisional measured / Derived |
| **B. Registered walls** (`v135→v139→v141`) | RL solver → Verified; dim → High; fallback 2.7 → Review | `gross = length_m × height_m` | Measured/Provisional/Review |
| **C. Auto envelope** (`v125.auto_envelope`) | Default 2.7 | `perimeter × 2.7` | Mapped (AUTO_GEOMETRY_UNVERIFIED) |
| **D. Studio faces** (`v1211`) | N/A (traced area) | Direct polygon area | Measured/Provisional |
| **E. Zones/import/AI** | N/A or user-entered | Varies | Mixed |

### Internal walls

- **No automatic generation.** Premier builder refuses: "Never use floor area as wall m²"
- Only: manual mapper lines, spreadsheet import, AI draft
- Height enters only if estimator chooses `footprint_perimeter_height` basis → `perimeter × default_wall_height_m`

---

## 4. Internal vs external wall height sources

| | Internal | External |
|---|---|---|
| Auto-generation | ❌ None | ✅ Three engines (A, B, C) |
| Height source | Only perimeter×2.7 if chosen | RL solver → dims → 2.7 fallback |
| Per-wall height | ❌ No per-wall distinction | ✅ Per-side registered (N/E/S/W) |
| Openings | Not auto-deducted | First-class deductions (v134/v137) |

---

## 5. Existing section/elevation extraction logic

### Elevation registration (`pb_elevation_registration_v135.py` — PRODUCTION, 270 lines)
- `orientation_from_text()`: regex matches cardinal directions in page text
- `dimension_candidates_m()`: parses 3–5 digit numbers as mm→m (band 0.25–150 m)
- `footprint_facades()`: calibrated prisms → N/E/S/W wall segments with `length_m`
- `register_elevations()`: cross-view matching (elevation page ↔ cardinal side by name + width)
- `wall_records()`: builds wall rows with default 2.7 height, flagged provisional

### Elevation height solver (`pb_elevation_profile_v136.py` — PRODUCTION, 64 lines)
- `rl_values()`: extracts RL/AHD values from page text
- `vertical_dimension_candidates()`: dims 1.8–12 m (storey-height band)
- `solve_height_from_text()`: RL differences → smallest closest to 3.0 → "Verified"; else dim → "High"; else default → "Review"
- `build_profiles()`: calls v135 registration, solves height per registered side

### Auto-geometry cross-calibration (`pb_auto_geometry_v1219.py`)
- `_cross_calibrate_elevations()`: plan footprint width → elevation px_per_m
- `_build_facade_rows()`: dominant connected-component bbox → width_m, height_m, gross_m2
- `_refresh_auto_model()`: median facade heights → model_masses.height

### Dormant hook
- `reconcile_facade_v145()`: plan vs elevation width/height comparison (2% tolerance) — wired but NEVER CALLED

### Section extraction — MAJOR GAP
- Sections are classified but **never feed height data**
- `pb_planreader_3d_app.py:6074-6088`: section dimensions just dumped as "Dimension" rows
- No section → height pipeline exists

---

## 6. Existing dimension-text parsing

| Parser | File | Handles | Band |
|---|---|---|---|
| `_DIM_RE` | `v135:18` | 3–5 digit numbers, optional mm/m suffix | 0.25–150 m |
| `_DIM_RE` | `v136:15` | Same | 1.8–12 m (height filter) |
| `_RL_RE` | `v136:14` | `RL/AHD` + number | — |
| `_dimension_value_m()` | `v1219:156` | 2–5 digit numbers | 0.30–100 m |
| `DIMENSION_PATTERNS` | `pb_planreader_offline.py:110-120` | mm tokens | Various |

### Missing parsers
- ❌ No "CH" (ceiling height) parser
- ❌ No "CLG" / "FCL" parser  
- ❌ No "FFL" value parser (only classification token)
- ❌ No explicit "floor-to-floor" parser
- ❌ No "parapet height" parser
- ❌ No "soffit height" parser

---

## 7. Existing elevation/cross-view registration

**Three-layer architecture (all production):**

1. **v135** — Side registration: prisms → N/E/S/W facades; elevation pages matched to sides by explicit name + width agreement
2. **v136** — Height solver: per-side RL/dimension → height_m with confidence
3. **v139** — Wall assembly: plan length × solved height → gross/net m²

Plus:
- **v1219** — Older elevation-bbox path (independent, parallel)
- **v145** — `reconcile_facade()` dormant hook (never invoked)
- **v137** — Opening geometry attachment
- **v138** — Substrate assignment
- **v141** — Takeoff sync

---

## 8. Page classification

| Type | Keywords | Files |
|---|---|---|
| Floor Plan | floor plan, level plan, partition plan | `v1225.py:30-44` |
| Elevation | north/south/east/west elevation, facade, building elevation | Same |
| Section | building section, wall section, cross section | Same |
| RCP | reflected ceiling plan, rcp, ceiling layout | Same |
| Roof Plan | roof plan | Same |

### Critical gap
- **No "Internal Elevation" vs "External Elevation" distinction** — only single "Elevation" type
- This means all elevation pages are treated identically regardless of interior vs exterior

---

## 9. Where height data is currently stored

| Store | What | Writer |
|---|---|---|
| `workspace_settings["elevation_profiles_v136"]` | Per-side height with confidence/status/RLs/dims | v136 `build_profiles()` |
| `workspace_settings["elevation_registration_v135"]` | Facade registrations (no height) | v135 |
| `workspace_settings["default_wall_height_m"]` | Global fallback 2.7 | Settings UI |
| `mapped_zones.wall_height_m` | Per-zone height (default 2.7) | Zone editor |
| `model_masses.height` | 3D mass extrusion height | Various builders |
| In-memory wall dicts | `height_m`, `height_status`, `height_confidence` | v139 `build_registered_walls()` |
| `takeoff_rows` | Final m² quantity (height hidden in notes/status) | All paths |

---

## 10. Where height data SHOULD live

### Recommended: Per-room height evidence table

Currently height lives scattered across workspace settings, DB schema defaults, and in-memory dicts. There is no structured, queryable height record per room/wall.

**Proposed**: New `page_height_evidence` table or `workspace_settings["height_evidence_v150"]` JSON:

```
{
  "height_records": [
    {
      "id": "H001",
      "workspace_id": 4,
      "source_page_id": 12,
      "source_drawing": "A301",
      "target_type": "room|wall|facade|storey",
      "target_ref": "BED 1|N01|all",
      "height_type": "floor_to_ceiling|floor_to_floor|wall_finish|parapet|soffit|facade",
      "raw_text": "CH 2700",
      "height_m": 2.7,
      "extraction_method": "rl_difference|dimension_parsed|elevation_bbox|schedule|default",
      "scale_source": "1:50|auto_cross_ref|...",
      "semantic_confidence": 0.95,
      "geometry_confidence": 0.90,
      "status": "Measured|Provisional measured|Default/fallback|Review",
      "evidence": ["RL 10.000 → RL 12.700 = 2.7 m difference"],
      "notes": ""
    }
  ]
}
```

This allows:
- Per-room height association
- Height type preservation
- Source traceability
- Confidence/status propagation to all downstream engines

---

## Summary of gaps blocking Priority 3

1. **No section→height pipeline**: Sections carry important storey heights but contribute nothing to the height model
2. **No ceiling-height parser**: "CH 2700", "CLG 3000", "FCL 2700" are not recognized
3. **No per-room height association**: Heights are per-facade-side (external only), never per-room
4. **No height type distinction**: floor-to-ceiling vs floor-to-floor are conflated everywhere
5. **No structured height record**: Height lives in scattered locations, no single queryable store
6. **`reconcile_facade_v145` dormant**: Plan-vs-elevation cross-check exists but is never invoked
7. **No internal elevation distinction**: Internal vs external elevations not classified separately
8. **Default 2.7 is deeply embedded**: 17+ locations all independently falling back to the same magic number
