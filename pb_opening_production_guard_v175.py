"""Startup verification for PlanReader P5 opening production integration.

The production integration is safety-critical. If a legacy alias was not replaced
because import/application order changed, startup must fail rather than silently
return to auto-deduct behaviour.
"""
from __future__ import annotations

from typing import Any, Dict

VERSION = "1.7.5"


def _unsafe() -> Dict[str, Any]:
    return {
        "kind": "door",
        "tag": "D01",
        "wall_ref": "W01",
        "width_m": 1.0,
        "height_m": 2.0,
        "area_m2": 2.0,
        "quantity": 1,
        "deduct": True,
        "substrate": "TEST",
    }


def verify(app: Any) -> None:
    required = (
        "detect_openings_v145",
        "room_quantity_summary_v145",
        "facade_net_area_v145",
        "normalise_opening",
        "deducted_opening_area_m2",
        "analyse_stored_page_v130",
        "run_p5_opening_native_payload_v175",
        "is_authorised_opening_deduction_v175",
    )
    missing = [name for name in required if not hasattr(app, name)]
    if missing:
        raise RuntimeError("P5 opening production guard missing required bindings: " + ", ".join(missing))
    if not getattr(app, "_pb_opening_legacy_safety_v175", False):
        raise RuntimeError("P5 legacy deduction safety fence was not installed")
    if not getattr(app, "_pb_opening_consumer_attach_v175", False):
        raise RuntimeError("P5 authoritative attach_openings_v137 consumer wrapper was not installed")
    if not getattr(app, "_pb_opening_native_bridge_v175", False):
        raise RuntimeError("P5 native-vector opening bridge was not installed")

    unsafe = _unsafe()
    detected = app.detect_openings_v145([unsafe])
    if any(bool(row.get("deduct")) for row in detected or []):
        raise RuntimeError("Unsafe v145 detect_openings alias still permits automatic deduction")

    rooms = [{"floor_area_m2": 10.0, "ceiling_reference_m2": 10.0, "perimeter_m": 12.0}]
    room_summary = app.room_quantity_summary_v145(rooms, [unsafe])
    if float(room_summary.get("opening_deduction_m2") or 0.0) != 0.0:
        raise RuntimeError("Unsafe v145 room summary alias still subtracts legacy opening defaults")

    facade = app.facade_net_area_v145(
        [{"substrate": "TEST", "area_m2": 10.0}],
        [unsafe],
    )
    if float((facade.get("TEST") or {}).get("deductions_m2") or 0.0) != 0.0:
        raise RuntimeError("Unsafe v145 facade alias still subtracts legacy opening defaults")

    normalised = app.normalise_opening(unsafe)
    if bool(normalised.get("deduct")):
        raise RuntimeError("Legacy v134 normaliser still preserves an unproven deduct=True default")
    if float(app.deducted_opening_area_m2([unsafe]) or 0.0) != 0.0:
        raise RuntimeError("Legacy v134 area consumer still subtracts an unproven opening")
