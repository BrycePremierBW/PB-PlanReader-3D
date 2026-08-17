"""Premier Brushworks PlanReader v1.2 import fixes.

This overlay sits on top of the v1.1 estimating behaviour and fixes the
spreadsheet import path used for PB / JobHub take-offs.

Key differences from the generic importer:
- scans every Excel worksheet, not just sheet 1;
- recognises JobHub column names directly;
- understands separate Qty m² / Lineal m / Count columns;
- never drops a line just because its quantity is not in the first quantity column;
- normalises imported rows through the Premier Brushworks v1.1 take-off method.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pb_takeoff_v11 as v11

PB_IMPORT_VERSION = "2026.08.07-2"


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower().replace("²", "2").replace("³", "3"))


# Exact PB / JobHub spreadsheet names. These are checked before the generic
# fuzzy matcher because e.g. labour_category should be an element, not a section.
_EXACT_HEADER_TARGETS = {
    "internalexternal": "section",
    "internalorexternal": "section",
    "arealocation": "location",
    "locationarea": "location",
    "labourcategory": "element",
    "laborcategory": "element",
    "workitem": "element",
    "substrate": "substrate",
    "surface": "substrate",
    "finishsystem": "finish_system",
    "paintsystem": "finish_system",
    "qtym2": "quantity",
    "quantitym2": "quantity",
    "aream2": "quantity",
    "m2": "quantity",
    "sqm": "quantity",
    "linealm": "quantity",
    "linearm": "quantity",
    "linealmetres": "quantity",
    "linearmetres": "quantity",
    "lm": "quantity",
    "count": "quantity",
    "qtyno": "quantity",
    "quantityno": "quantity",
    "coats": "coats",
    "rateexgst": "rate_per_unit",
    "rateperunit": "rate_per_unit",
    "unitrate": "rate_per_unit",
    "confidence": "confidence",
    "sourcenote": "notes",
    "notes": "notes",
    "comments": "notes",
    "sourcepage": "source_page",
    "sourcereference": "source_reference",
    "inclusionstatus": "inclusion_status",
    "quantitystatus": "quantity_status",
    "unit": "unit",
    "quantity": "quantity",
    "intext": "section",
    "intorext": "section",
    "intorexternal": "section",
    "include": "inclusion_status",
    "sourcedrawing": "source_reference",
    "drawingsource": "source_reference",
    "drawingreference": "source_reference",
    "paintcoverageallowance": "coverage_m2_per_litre",
    "coverageallowance": "coverage_m2_per_litre",
    "productivityqtyhr": "productivity_m2_per_hour",
    "qtyperhr": "productivity_m2_per_hour",
    "paintqtyperhour": "productivity_m2_per_hour",
    "floorarea": "floor_area",
    "flooraream2": "floor_area",
    "internalfloorarea": "floor_area",
    "internalflooraream2": "floor_area",
    "netfloorarea": "floor_area",
    "nettfloorarea": "floor_area",
    "grossfloorarea": "floor_area",
}

_M2_HEADERS = {
    "qtym2", "quantitym2", "aream2", "m2", "sqm", "squaremetres", "squaremeters"
}
_LM_HEADERS = {
    "linealm", "linearm", "linealmetres", "linearmetres", "linealmeters", "linearmeters", "lm"
}
_COUNT_HEADERS = {
    "count", "qtyno", "quantityno", "no", "number", "each", "ea"
}

_JOBHUB_DIRECT = {
    "internalexternal": "section",
    "intext": "section",
    "intorext": "section",
    "arealocation": "location",
    "labourcategory": "element",
    "laborcategory": "element",
    "substrate": "substrate",
    "coats": "coats",
    "rateexgst": "rate_per_unit",
    "confidence": "confidence",
    "sourcenote": "notes",
    "include": "inclusion_status",
    "sourcedrawing": "source_reference",
    "paintcoverageallowance": "coverage_m2_per_litre",
    "productivityqtyhr": "productivity_m2_per_hour",
}

_IGNORED_CALCULATED_HEADERS = {
    "labourhours", "laborhours", "paintlitres", "paintliters", "valueexgst",
    "totalexgst", "extendedvalue", "updatedat", "id", "jobid"
}

_TOTAL_LABEL = re.compile(r"\b(total|subtotal|grand\s*total|sum|average|base\s+totals)\b", re.I)

_LOCATION_PREFIX = re.compile(
    r"^\s*(?:units?\s+\d+(?:\s*[\u2013\u2014\-]\s*\d+)?\s+|"
    r"unit\s+\d+\s+|"
    r"block\s+[a-z0-9]+\s*|"
    r"level\s+\d+\s+|"
    r"[a-z]+\s+street\s+)",
    re.I,
)

# (regex, label) in priority order. Matched against the Area/Location text once
# any "Units 5-9 / Block B / King Street"-style location prefix has been removed.
_ELEMENT_RULES = [
    (re.compile(r"\binternal\s+walls?\b", re.I), "Internal walls"),
    (re.compile(r"\bexternal\s+walls?\b", re.I), "External walls"),
    (re.compile(r"\binternal\s+(single\s+)?hinged\s+doors?\b", re.I), "Internal hinged doors"),
    (re.compile(r"\bcavity\s+sliders?\b", re.I), "Cavity sliders"),
    (re.compile(r"\bsliding\s+doors?\b|\bsliders?\b", re.I), "Sliding doors"),
    (re.compile(r"\bskirting\b|\barchitraves?\b|\btrim\b", re.I), "Skirting / trim"),
    (re.compile(r"\bceilings?\b", re.I), "Ceilings"),
    (re.compile(r"\bsoffits?\b", re.I), "Soffits"),
    (re.compile(r"\btextureboard\b", re.I), "Textureboard cladding"),
    (re.compile(r"\blineaboard\b", re.I), "Lineaboard cladding"),
    (re.compile(r"\beasylap\b", re.I), "Easylap cladding"),
    (re.compile(r"\bcladding\b", re.I), "Cladding"),
    (re.compile(r"\bfences?\b", re.I), "Fence"),
    (re.compile(r"\brendered\s+block\b|\bblockwork\b", re.I), "Rendered block"),
    (re.compile(r"\bdoors?\b", re.I), "Doors"),
    (re.compile(r"\bwindows?\b", re.I), "Windows"),
    (re.compile(r"\bwalls?\b", re.I), "Walls"),
    (re.compile(r"\bmetalwork\b|\bmetal\b|\bsteel\b|\bhandrails?\b|\brailing\b", re.I), "Metalwork"),
]


def _derive_element(location: Any) -> str:
    text = str(location or "").strip()
    stripped = _LOCATION_PREFIX.sub("", text).strip(" ,:;.-\u2013\u2014")
    search_in = stripped or text
    for pattern, label in _ELEMENT_RULES:
        if pattern.search(search_in):
            return label
    return ""


def _normalise_inclusion(raw: Any) -> str:
    low = str(raw or "").strip().casefold()
    if not low or low in {"base", "included", "include", "yes", "incl", "inc"}:
        return "INCLUSION"
    if low in {"optional", "opt", "option"}:
        return "PROVISIONAL"
    if low in {"excluded", "exclude", "exc", "no"}:
        return "EXCLUSION"
    if low in {"provisional", "prov"}:
        return "PROVISIONAL"
    if low in {"separate", "separate item"}:
        return "SEPARATE ITEM"
    if low in {"clarification", "clarify"}:
        return "CLARIFICATION"
    return str(raw or "").strip() or "INCLUSION"


def _to_float(app, value: Any) -> float:
    try:
        return float(app.to_float(value, 0.0))
    except Exception:
        try:
            return float(value or 0)
        except Exception:
            return 0.0


def _read_frames(upload: Any) -> List[Tuple[str, pd.DataFrame]]:
    name = str(getattr(upload, "name", "") or "takeoff")
    data = upload.getvalue()
    suffix = Path(name).suffix.lower()
    if suffix == ".csv":
        attempts = ["utf-8-sig", "utf-8", "cp1252", "latin1"]
        last_exc: Optional[Exception] = None
        for encoding in attempts:
            try:
                frame = pd.read_csv(io.BytesIO(data), header=None, encoding=encoding, dtype=object)
                return [("CSV", frame)]
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"Could not read CSV take-off: {last_exc}")

    if suffix not in {".xlsx", ".xls"}:
        raise RuntimeError("Take-off must be an Excel (.xlsx/.xls) or CSV file.")
    try:
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=object)
    except Exception as exc:
        raise RuntimeError(f"Could not read Excel take-off: {exc}")
    return [(str(name), frame) for name, frame in sheets.items() if frame is not None and not frame.empty]


def _make_matcher(app):
    old_matcher = app._match_takeoff_header

    def matcher(header: Any) -> Optional[str]:
        raw = str(header or "").strip()
        if not raw or raw.lower() in {"nan", "none"}:
            return None
        key = _key(raw)
        if key in _IGNORED_CALCULATED_HEADERS:
            return None
        exact = _EXACT_HEADER_TARGETS.get(key)
        if exact:
            return exact
        # Currency-per-area headers are rates, not quantities.
        low = raw.lower()
        if "$" in low and any(token in low for token in ["m2", "m²", "sqm", "lm", "each", "no"]):
            return "rate_per_unit"
        return old_matcher(header)

    return matcher


def _header_score(app, row: List[Any]) -> int:
    targets: List[str] = []
    unit_types = set()
    for cell in row:
        k = _key(cell)
        target = app._match_takeoff_header(cell)
        if target:
            targets.append(target)
        if k in _M2_HEADERS:
            unit_types.add("m2")
        elif k in _LM_HEADERS:
            unit_types.add("lm")
        elif k in _COUNT_HEADERS:
            unit_types.add("count")
    # Reward distinct useful columns, plus separate PB quantity channels.
    return len(set(targets)) * 3 + len(targets) + len(unit_types) * 2


def make_detect_takeoff_columns(app):
    def detect_takeoff_columns(upload: Any, header_row: Optional[int] = None) -> Tuple[List[str], List[List[Any]], int, int, int]:
        frames = _read_frames(upload)
        if not frames:
            raise RuntimeError("The take-off workbook contains no readable sheets.")

        best: Optional[Tuple[int, str, pd.DataFrame, int]] = None
        for sheet_name, df in frames:
            if df.empty or df.shape[1] == 0:
                continue
            if header_row is None:
                candidates = range(min(60, len(df)))
            else:
                candidates = [max(0, min(int(header_row), len(df) - 1))]
            for row_index in candidates:
                score = _header_score(app, df.iloc[row_index].tolist())
                candidate = (score, sheet_name, df, row_index)
                if best is None or candidate[0] > best[0]:
                    best = candidate

        if best is None:
            raise RuntimeError("The take-off file is empty.")

        score, sheet_name, df, detected = best
        # If no meaningful header is recognised, keep the first requested row so
        # the user can still use the manual mapping editor.
        if header_row is None and score < 5:
            sheet_name, df = frames[0]
            detected = 0

        raw_headers = []
        for idx, value in enumerate(df.iloc[detected].tolist()):
            text = str(value or "").strip()
            if text.lower() in {"nan", "none"}:
                text = ""
            raw_headers.append(text or f"Column {idx + 1}")
        body = [list(line) for line in df.iloc[detected + 1:].astype(object).values.tolist()]
        return raw_headers, body, int(detected), int(score), int(len(df))

    return detect_takeoff_columns


def _normalise_row(app, row: Dict[str, Any]) -> Dict[str, Any]:
    # v1.1 contains the PB estimator classification rules. Reuse them so a file
    # import and an AI take-off land in the same schedule structure.
    normaliser = getattr(v11, "normalise_takeoff_row", None)
    if callable(normaliser):
        try:
            return normaliser(row)
        except Exception:
            pass

    # Safe fallback if an older v1.1 module is deployed.
    row = dict(row)
    row["element"] = str(row.get("element") or "Paintable surface").strip()
    row["location"] = str(row.get("location") or "Unallocated / review").strip()
    row["substrate"] = str(row.get("substrate") or "Other").strip()
    row["finish_system"] = str(row.get("finish_system") or "To be confirmed").strip()
    row["quantity"] = max(0.0, _to_float(app, row.get("quantity")))
    row["quantity_status"] = str(row.get("quantity_status") or ("Measured" if row["quantity"] > 0 else "To measure"))
    row["inclusion_status"] = str(row.get("inclusion_status") or "INCLUSION")
    row["source_reference"] = str(row.get("source_reference") or "Spreadsheet import")
    return row


def _metric_columns(headers: List[str]) -> Dict[str, List[int]]:
    result = {"m²": [], "lm": [], "No.": []}
    for idx, header in enumerate(headers):
        k = _key(header)
        if k in _M2_HEADERS:
            result["m²"].append(idx)
        elif k in _LM_HEADERS:
            result["lm"].append(idx)
        elif k in _COUNT_HEADERS:
            result["No."].append(idx)
    return result


def _direct_indices(headers: List[str]) -> Dict[str, int]:
    found: Dict[str, int] = {}
    for idx, header in enumerate(headers):
        target = _JOBHUB_DIRECT.get(_key(header))
        if target and target not in found:
            found[target] = idx
    return found


def _extra_jobhub_notes(app, headers: List[str], line: List[Any]) -> str:
    values: List[str] = []
    lookup = {_key(h): i for i, h in enumerate(headers)}
    for key, label, suffix in [
        ("labourhours", "stored labour", "hrs"),
        ("laborhours", "stored labour", "hrs"),
        ("paintlitres", "stored paint", "L"),
        ("paintliters", "stored paint", "L"),
        ("valueexgst", "stored value", ""),
    ]:
        idx = lookup.get(key)
        if idx is None or idx >= len(line):
            continue
        val = _to_float(app, line[idx])
        if val <= 0:
            continue
        if key == "valueexgst":
            values.append(f"{label} ${val:,.2f}")
        else:
            values.append(f"{label} {val:g} {suffix}")
    return " · ".join(values)


def make_parse_takeoff_file(app):
    def parse_takeoff_file(
        upload: Any,
        mapping: Optional[Dict[int, str]] = None,
        raw_headers: Optional[List[str]] = None,
        body: Optional[List[List[Any]]] = None,
    ) -> Tuple[pd.DataFrame, List[str]]:
        name = str(getattr(upload, "name", "") or "takeoff")
        warnings: List[str] = []
        if raw_headers is None or body is None:
            raw_headers, body, _row, _score, _total = app.detect_takeoff_columns(upload)

        if mapping is None:
            mapping = {}
            used: List[str] = []
            for idx, header in enumerate(raw_headers):
                target = app._match_takeoff_header(header)
                # Allow the three quantity channels to repeat; other fields stay unique.
                if target == "quantity":
                    mapping[idx] = target
                elif target and target not in used:
                    mapping[idx] = target
                    used.append(target)

        metrics = _metric_columns(raw_headers)
        direct = _direct_indices(raw_headers)
        has_pb_metrics = any(metrics.values())
        unit_idx = next((i for i, t in mapping.items() if t == "unit"), None)
        has_notes_col = any(_key(h) in {"notes", "sourcenote", "comments", "comment", "remarks"} for h in raw_headers)
        rows: List[Dict[str, Any]] = []

        for source_row_no, line in enumerate(body, start=2):
            if not any(str(v or "").strip().lower() not in {"", "nan", "none"} for v in line):
                continue

            # Skip roll-up rows such as "BASE TOTALS" placed in the unit column.
            if unit_idx is not None and unit_idx < len(line):
                unit_cell = str(line[unit_idx] or "").strip()
                if _TOTAL_LABEL.search(unit_cell):
                    continue

            base: Dict[str, Any] = {c: "" for c in app.TAKEOFF_COLUMNS}
            base["row_role"] = ""
            floor_column_qty: Optional[float] = None

            # Generic/manual mappings first.
            for idx, target in mapping.items():
                if idx >= len(line):
                    continue
                value = line[idx]
                if target == "floor_area":
                    # A 'Floor area (m²)' / 'Floor m²' column records each level's
                    # internal floor area as a reference measurement row that can
                    # drive floor-m² pricing; it is never a painted quantity.
                    base["quantity"] = max(0.0, _to_float(app, value))
                    base["unit"] = "m²"
                    base["row_role"] = "floor_area"
                    floor_column_qty = max(0.0, _to_float(app, value))
                    continue
                if target not in app.TAKEOFF_COLUMNS:
                    continue
                if target == "quantity":
                    # PB metric columns are resolved below so qty_m2/lineal_m/count
                    # cannot overwrite one another.
                    if not has_pb_metrics:
                        base["quantity"] = app._parse_qty(value)
                elif target == "unit":
                    base["unit"] = app._normalise_unit(value)
                elif target in {"coats", "coverage_m2_per_litre", "productivity_m2_per_hour", "rate_per_unit"}:
                    base[target] = _to_float(app, value)
                else:
                    text = str(value or "").strip()
                    if text.lower() not in {"nan", "none"}:
                        base[target] = text

            # Exact JobHub names override fuzzy/manual mistakes.
            for target, idx in direct.items():
                if idx >= len(line):
                    continue
                value = line[idx]
                if target in {"coats", "rate_per_unit"}:
                    base[target] = _to_float(app, value)
                else:
                    text = str(value or "").strip()
                    if text.lower() not in {"nan", "none"}:
                        base[target] = text

            # PB take-off sheets keep the item description in the area/location
            # text. Derive a concise element label from it when none was given.
            if not str(base.get("element") or "").strip():
                base["element"] = _derive_element(base.get("location") or "")
            if str(base.get("inclusion_status") or "").strip():
                base["inclusion_status"] = _normalise_inclusion(base.get("inclusion_status"))

            if not base["row_role"]:
                element_text = str(base.get("element") or "").lower()
                location_text = str(base.get("location") or "").lower()
                if "floor area" in element_text or "internal floor" in element_text or "floor m2" in element_text or "floor area" in location_text:
                    base["row_role"] = "floor_area"

            source_note = str(base.get("notes") or "").strip()
            extra = "" if has_notes_col else _extra_jobhub_notes(app, raw_headers, line)
            if extra:
                source_note = f"{source_note} · {extra}".strip(" ·")
            base["notes"] = source_note
            base["source_reference"] = str(base.get("source_reference") or f"{name} · row {source_row_no}")

            metric_values: List[Tuple[float, str, str]] = []
            if has_pb_metrics:
                for unit, indices in metrics.items():
                    total = 0.0
                    header_names: List[str] = []
                    for idx in indices:
                        if idx >= len(line):
                            continue
                        val = max(0.0, _to_float(app, line[idx]))
                        if val > 0:
                            total += val
                            header_names.append(raw_headers[idx])
                    if total > 0:
                        metric_values.append((total, unit, ", ".join(header_names)))

            if not metric_values:
                q = max(0.0, _to_float(app, base.get("quantity")))
                unit = str(base.get("unit") or "").strip()
                if not unit:
                    # Infer unit from the mapped quantity header if possible.
                    qty_header = next((raw_headers[i] for i, t in mapping.items() if t == "quantity" and i < len(raw_headers)), "")
                    k = _key(qty_header)
                    if k in _LM_HEADERS:
                        unit = "lm"
                    elif k in _COUNT_HEADERS:
                        unit = "No."
                    else:
                        unit = "m²"
                metric_values = [(q, app._normalise_unit(unit) or unit, "")]

            # If a JobHub row legitimately contains multiple quantity channels,
            # preserve every channel as its own take-off line instead of dropping two.
            for quantity, unit, metric_source in metric_values:
                row = dict(base)
                if floor_column_qty is not None:
                    # Floor-area rows keep the quantity read from the floor-area
                    # column rather than any general quantity channel.
                    quantity = floor_column_qty
                    unit = "m²"
                    metric_source = ""
                row["quantity"] = quantity
                row["unit"] = unit
                if metric_source and len(metric_values) > 1:
                    suffix = f"Imported quantity channel: {metric_source}"
                    row["notes"] = f"{row.get('notes') or ''} · {suffix}".strip(" ·")
                row["quantity_status"] = str(row.get("quantity_status") or ("Measured" if quantity > 0 else "To measure"))
                row["inclusion_status"] = str(row.get("inclusion_status") or "INCLUSION")
                row_role = str(row.get("row_role") or "").strip()
                row = _normalise_row(app, row)
                row["row_role"] = row_role
                rows.append({c: row.get(c, "") for c in (app.TAKEOFF_COLUMNS + ["row_role"])})

        if not rows:
            raise RuntimeError(
                "No take-off lines were found. Check the selected header row and column mapping. "
                "For a JobHub export, columns such as Area Location, Labour Category and Qty m² / Lineal m / Count are recognised automatically."
            )

        if has_pb_metrics:
            warnings.append(
                "PB/JobHub quantity columns detected. Qty m², Lineal m and Count are imported independently so line types are not lost."
            )
        if len(mapping) < 3 and not has_pb_metrics:
            warnings.append("Only a few columns were recognised — review the mapping before importing.")

        frame = pd.DataFrame(rows, columns=app.TAKEOFF_COLUMNS + ["row_role"])
        return frame, warnings

    return parse_takeoff_file


def apply(app) -> None:
    """Install v1.2 import behaviour on the already-patched PlanReader module."""
    app.PB_IMPORT_VERSION = PB_IMPORT_VERSION
    app._match_takeoff_header = _make_matcher(app)
    app.detect_takeoff_columns = make_detect_takeoff_columns(app)
    app.parse_takeoff_file = make_parse_takeoff_file(app)
