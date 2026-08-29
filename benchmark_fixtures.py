"""benchmark_fixtures.py — deterministic B3 benchmark fixture generator.

Generates the independent-truth JSON fixtures committed under
tests/fixtures/ that back the B3 benchmark expansion suite
(tests/test_b3_benchmark_expansion_v178.py).

Design contract (mirrors the LAGO benchmark authority):
  - Fixture generation is FULLY deterministic: no UUIDs, no timestamps,
    no randomisation.  Running this script twice produces byte-identical
    fixture files (verified by hashing the output of consecutive runs).
  - Every fixture carries committed INDEPENDENT TRUTH (synthesised plan
    geometry + schedule records) in the "truth" block.  Detector output
    never defines truth; the suite asserts pipeline outcomes against
    this committed truth.
  - Safety boundaries are encoded in the truth block:
      * ground floor: one door + one window, non-deducting (unknown basis)
      * multi-window wall: three windows detected, hatch-like batten
        region conservatively rejected (zero candidates), never deducts
      * envelope schedule: generic width/height schedule headings never
        enable an automatic void deduction (LAGO safety authority)
  - The geometry is expressed in PDF points at a fixed 50 pt/m scale,
    identical semantics to the scale-aware B1 test scenes.

Run from the repository root:
    python benchmark_fixtures.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

FIXTURE_VERSION = 3
PT_PER_M = 50.0          # deterministic benchmark raster scale (50 PDF pt == 1 m)
OUTPUT_DIR = Path(__file__).resolve().parent / "tests" / "fixtures"


def _seg(x1: float, y1: float, x2: float, y2: float,
         drawing_index: int) -> Dict[str, Any]:
    return {
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "drawing_index": drawing_index,
    }


def _word(text: str, cx: float, cy: float, page_no: int) -> Dict[str, Any]:
    """TextWord-compatible record (10pt wide, 5pt tall box centred at cx,cy)."""
    return {
        "text": text,
        "x0": round(cx - 10.0, 4),
        "y0": round(cy - 5.0, 4),
        "x1": round(cx + 10.0, 4),
        "y1": round(cy + 5.0, 4),
        "page_no": page_no,
    }


def _wall(wall_ref: str, x1: float, y1: float, x2: float, y2: float
          ) -> Dict[str, Any]:
    return {"wall_ref": wall_ref, "x1": x1, "y1": y1, "x2": x2, "y2": y2}


# ---------------------------------------------------------------------------
# Scenario 1 — bench_ground_floor: one door + one window on a ground floor.
# ---------------------------------------------------------------------------
def build_ground_floor() -> Dict[str, Any]:
    page = 1
    segments: List[Dict[str, Any]] = []
    # W-GF1: continuous 18 m wall along y=0 from x=0 to x=900pt.
    segments.append(_seg(0.0, 0.0, 900.0, 0.0, 1))
    # Door D01: 0.9 m leaf perpendicular to the wall, centred at 3.2 m.
    #   leaf from (160,0) down to (160,-45); 45pt == 0.9 m at 50pt/m.
    segments.append(_seg(160.0, 0.0, 160.0, -45.0, 2))
    # Window W01: 0.9 m opening, jamb pair centred at 10.25 m.
    #   jambs 38pt (0.76 m) perpendicular, 45pt (0.9 m) apart.
    segments.append(_seg(490.0, -8.0, 490.0, 30.0, 3))
    segments.append(_seg(535.0, -8.0, 535.0, 30.0, 4))

    words = [
        _word("D01", 168.0, -48.0, page),    # 26.7pt from leaf centre (< 30pt)
        _word("W01", 512.5, 60.0, page),     # 49pt from pair centre (< 120pt)
    ]

    return {
        "fixture_version": FIXTURE_VERSION,
        "workspace": {
            "id": "bench_ground_floor",
            "page_no": page,
            "title": "Benchmark Ground Floor — Plan GF.01",
            "scale_pt_per_m": PT_PER_M,
        },
        "source": {
            "pdf": "synthetic-benchmark-plans.pdf",
            "pdf_page_1based": page,
            "pdf_page_0based": page - 1,
            "drawing_ref": "BMK/GF/01",
            "drawing_title": "Ground Floor Plan",
            "scale_a1": "1:100",
            "scale_pt_per_m": PT_PER_M,
            "extraction": "deterministic synthetic geometry (50 pt/m)",
            "generation": "benchmark_fixtures.py",
        },
        "truth": {
            "authority": (
                "committed independent plan truth (synthesised); "
                "detector output never defines truth"),
            "doors": [
                {
                    "mark": "D01",
                    "opening_type": "door",
                    "wall_ref": "W-GF1",
                    "position_along_wall_m": 3.2,
                    "width_m": 0.9,
                    "height_m": None,
                }
            ],
            "windows": [
                {
                    "mark": "W01",
                    "opening_type": "window",
                    "wall_ref": "W-GF1",
                    "position_along_wall_m": 10.25,
                    "width_m": 0.9,
                    "height_m": None,
                }
            ],
            "gaps": [],
            "expected_candidate_counts": {"door": 1, "window": 1, "gap": 0},
            "dimension_basis": "unknown",
            "deduct_must_be_false": True,
            "safety_note": (
                "Plan-only evidence has unknown dimension basis; "
                "deduction must remain review, never deduct."),
        },
        "geometry": {
            "wall_lines": [_wall("W-GF1", 0.0, 0.0, 900.0, 0.0)],
            "segments": segments,
            "words": words,
        },
    }


# ---------------------------------------------------------------------------
# Scenario 2 — bench_multi_window_wall: three windows on one wall plus a
# hatch-like batten region that must be conservatively rejected.
# ---------------------------------------------------------------------------
def build_multi_window_wall() -> Dict[str, Any]:
    page = 2
    segments: List[Dict[str, Any]] = []
    di = 1

    # Wall A (W-MW1): 24 m horizontal wall at y=100, x=0..1200pt.
    segments.append(_seg(0.0, 100.0, 1200.0, 100.0, di)); di += 1
    # Three identical windows: 40pt (0.8 m) openings, centres 6/12/18 m.
    # Jamb segments 30pt (0.6 m).  Window centres 300pt apart == 6 m,
    # i.e. far above the 1.5 m hatch-suspicion gap.
    for cx in (300.0, 600.0, 900.0):
        segments.append(_seg(cx - 20.0, 85.0, cx - 20.0, 115.0, di)); di += 1
        segments.append(_seg(cx + 20.0, 85.0, cx + 20.0, 115.0, di)); di += 1

    # Wall B (W-MW2): separate 16 m wall at y=400, x=0..800pt.
    segments.append(_seg(0.0, 400.0, 800.0, 400.0, di)); di += 1
    # Hatch-like battens: five uniformly spaced pairs (centres every 50pt
    # == 1 m, jamb spacing 20pt == 0.4 m).  Regular repetition below the
    # 1.5 m scale gate triggers the hatch filter.  No window tag anywhere.
    for cx in (150.0, 200.0, 250.0, 300.0, 350.0):
        segments.append(_seg(cx - 10.0, 385.0, cx - 10.0, 415.0, di)); di += 1
        segments.append(_seg(cx + 10.0, 385.0, cx + 10.0, 415.0, di)); di += 1

    words = [
        _word("W01", 300.0, 65.0, page),
        _word("W02", 600.0, 65.0, page),
        _word("W03", 900.0, 65.0, page),
    ]

    return {
        "fixture_version": FIXTURE_VERSION,
        "workspace": {
            "id": "bench_multi_window_wall",
            "page_no": page,
            "title": "Benchmark Multi-Window Wall + Hatch Rejection",
            "scale_pt_per_m": PT_PER_M,
        },
        "source": {
            "pdf": "synthetic-benchmark-plans.pdf",
            "pdf_page_1based": page,
            "pdf_page_0based": page - 1,
            "drawing_ref": "BMK/MW/02",
            "drawing_title": "Multi-Window Wall Plan",
            "scale_a1": "1:100",
            "scale_pt_per_m": PT_PER_M,
            "extraction": "deterministic synthetic geometry (50 pt/m)",
            "generation": "benchmark_fixtures.py",
        },
        "truth": {
            "authority": (
                "committed independent plan truth (synthesised); "
                "detector output never defines truth"),
            "doors": [],
            "windows": [
                {
                    "mark": "W01",
                    "opening_type": "window",
                    "wall_ref": "W-MW1",
                    "position_along_wall_m": 6.0,
                    "width_m": 0.8,
                    "height_m": None,
                },
                {
                    "mark": "W02",
                    "opening_type": "window",
                    "wall_ref": "W-MW1",
                    "position_along_wall_m": 12.0,
                    "width_m": 0.8,
                    "height_m": None,
                },
                {
                    "mark": "W03",
                    "opening_type": "window",
                    "wall_ref": "W-MW1",
                    "position_along_wall_m": 18.0,
                    "width_m": 0.8,
                    "height_m": None,
                },
            ],
            "gaps": [],
            "expected_candidate_counts": {"door": 0, "window": 3, "gap": 0},
            "rejected_regions": [
                {
                    "wall_ref": "W-MW2",
                    "description": (
                        "hatch-like batten repetition — must be "
                        "conservatively rejected, never a false positive"),
                    "expected_candidates": 0,
                }
            ],
            "dimension_basis": "unknown",
            "deduct_must_be_false": True,
            "safety_note": (
                "Three independently-tagged windows must each resolve "
                "without tie-ambiguity; the batten region must never "
                "produce an opening or a deduction."),
        },
        "geometry": {
            "wall_lines": [
                _wall("W-MW1", 0.0, 100.0, 1200.0, 100.0),
                _wall("W-MW2", 0.0, 400.0, 800.0, 400.0),
            ],
            "segments": segments,
            "words": words,
        },
    }


# ---------------------------------------------------------------------------
# Scenario 3 — bench_envelope_schedule: plan-derived opening evidence
# enriched by a door/window schedule with GENERIC width/height headings.
# The generic headings must never enable an automatic void deduction.
# ---------------------------------------------------------------------------
def build_envelope_schedule() -> Dict[str, Any]:
    page = 3
    segments: List[Dict[str, Any]] = []
    # W-ENV1: 16 m envelope wall at y=100, x=0..800pt.
    segments.append(_seg(0.0, 100.0, 800.0, 100.0, 1))
    # W01: 0.9 m opening, jamb pair centred at 4.75 m (237.5pt).
    segments.append(_seg(215.0, 85.0, 215.0, 115.0, 2))
    segments.append(_seg(260.0, 85.0, 260.0, 115.0, 3))
    # W02: 0.9 m opening, jamb pair centred at 10.75 m (537.5pt).
    segments.append(_seg(515.0, 85.0, 515.0, 115.0, 4))
    segments.append(_seg(560.0, 85.0, 560.0, 115.0, 5))

    words = [
        _word("W01", 237.5, 65.0, page),
        _word("W02", 537.5, 65.0, page),
    ]

    return {
        "fixture_version": FIXTURE_VERSION,
        "workspace": {
            "id": "bench_envelope_schedule",
            "page_no": page,
            "title": "Benchmark Envelope + Door/Window Schedule",
            "scale_pt_per_m": PT_PER_M,
        },
        "source": {
            "pdf": "synthetic-benchmark-plans.pdf",
            "pdf_page_1based": page,
            "pdf_page_0based": page - 1,
            "drawing_ref": "BMK/ES/03",
            "drawing_title": "Envelope Plan + Schedule 01",
            "scale_a1": "1:100",
            "scale_pt_per_m": PT_PER_M,
            "extraction": "deterministic synthetic geometry (50 pt/m)",
            "generation": "benchmark_fixtures.py",
        },
        "session": {
            "schedule": {
                "header": ["mark", "width", "height"],
                "rows": [
                    ["W01", "900", "1500"],
                    ["W02", "900", "1200"],
                ],
                "page_no": page,
                "heading_dimension_basis": "",
                "heading_note": (
                    "generic width/height headings — schedule dimensions "
                    "alone must never create an automatic void deduction"),
            }
        },
        "truth": {
            "authority": (
                "committed independent plan + schedule truth (synthesised); "
                "detector output never defines truth"),
            "doors": [],
            "windows": [
                {
                    "mark": "W01",
                    "opening_type": "window",
                    "wall_ref": "W-ENV1",
                    "position_along_wall_m": 4.75,
                    "width_m": 0.9,
                    "height_m_mm": 1500,
                },
                {
                    "mark": "W02",
                    "opening_type": "window",
                    "wall_ref": "W-ENV1",
                    "position_along_wall_m": 10.75,
                    "width_m": 0.9,
                    "height_m_mm": 1200,
                },
            ],
            "gaps": [],
            "expected_candidate_counts": {"door": 0, "window": 2, "gap": 0},
            "schedule_safety": {
                "dimension_basis_expected_after_enrichment": "unknown",
                "deduct_must_be_false": True,
                "reason": (
                    "LAGO safety authority: generic schedule WIDTH/HEIGHT "
                    "headings do not prove a rough-opening basis, so "
                    "schedule dimensions alone must never create an "
                    "automatic wall-void deduction."),
            },
        },
        "geometry": {
            "wall_lines": [_wall("W-ENV1", 0.0, 100.0, 800.0, 100.0)],
            "segments": segments,
            "words": words,
        },
    }


_SPECS = {
    "bench_ground_floor": build_ground_floor,
    "bench_multi_window_wall": build_multi_window_wall,
    "bench_envelope_schedule": build_envelope_schedule,
}


def _serialise(fixture: Dict[str, Any]) -> str:
    return json.dumps(fixture, sort_keys=True, indent=2) + "\n"


def write_fixtures(output_dir: Optional[Path] = None, print_hashes: bool = True
                   ) -> Dict[str, str]:
    """Generate and persist all fixtures.  Returns {name: sha256}."""
    output_dir = Path(output_dir) if output_dir else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    digests: Dict[str, str] = {}
    for name, builder in sorted(_SPECS.items()):
        payload = _serialise(builder())
        target = output_dir / f"{name}.json"
        # Bytes mode: the on-disk content is byte-identical to the in-memory
        # payload (LF-only), so the printed hash matches Get-FileHash and
        # consecutive runs produce byte-identical files.
        target.write_bytes(payload.encode("utf-8"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        digests[name] = digest
        if print_hashes:
            print(f"{name}.json  sha256={digest}  bytes={len(payload)}")
    if print_hashes:
        combined = hashlib.sha256(
            "\n".join(f"{k}:{v}" for k, v in sorted(digests.items())).encode("utf-8")
        ).hexdigest()
        print(f"combined sha256={combined}")
    return digests


def main() -> None:
    write_fixtures(OUTPUT_DIR)
    print(f"Wrote {len(_SPECS)} deterministic fixture(s) to {OUTPUT_DIR.resolve()}")
    for name in sorted(_SPECS):
        target = OUTPUT_DIR / f"{name}.json"
        assert target.exists(), f"fixture file missing after write: {target}"


if __name__ == "__main__":
    main()