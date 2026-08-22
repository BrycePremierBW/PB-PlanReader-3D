"""PlanReader v1.3.0 benchmark framework.

Stores estimator-verified ground truth separately from predictions and reports
category-specific error instead of one misleading global accuracy percentage.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

VERSION = "1.3.0"
TARGETS = {
    "sheet_classification": {"metric": "accuracy", "target": 0.99},
    "scale": {"metric": "mape", "target": 0.005},
    "floor_area": {"metric": "mape", "target": 0.02},
    "ceiling_area": {"metric": "mape", "target": 0.02},
    "room_perimeter": {"metric": "mape", "target": 0.02},
    "door_count": {"metric": "accuracy", "target": 0.98},
    "window_count": {"metric": "accuracy", "target": 0.95},
    "external_area": {"metric": "mape", "target": 0.03},
    "substrate_allocation": {"metric": "accuracy", "target": 0.95},
    "finish_association": {"metric": "accuracy", "target": 0.97},
    "missed_scope": {"metric": "rate", "target": 0.02},
    "false_inclusion": {"metric": "rate", "target": 0.02},
}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def ensure_schema(app: Any) -> None:
    conn = app.local_connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accuracy_ground_truth (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                page_id INTEGER,
                category TEXT NOT NULL,
                item_key TEXT NOT NULL,
                expected_numeric REAL,
                expected_text TEXT,
                unit TEXT,
                source_reference TEXT,
                verified_by TEXT,
                verification_notes TEXT,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(workspace_id, category, item_key)
            );
            CREATE TABLE IF NOT EXISTS accuracy_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                page_id INTEGER,
                category TEXT NOT NULL,
                item_key TEXT NOT NULL,
                predicted_numeric REAL,
                predicted_text TEXT,
                unit TEXT,
                confidence REAL,
                method TEXT,
                evidence_json TEXT,
                engine_version TEXT,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_accuracy_truth_workspace ON accuracy_ground_truth(workspace_id, category);
            CREATE INDEX IF NOT EXISTS idx_accuracy_predictions_workspace ON accuracy_predictions(workspace_id, category, engine_version);
            """
        )
        conn.commit()
    finally:
        conn.close()


def upsert_truth(app: Any, workspace_id: int, category: str, item_key: str, *, expected_numeric: Optional[float] = None,
                 expected_text: str = "", unit: str = "", page_id: Optional[int] = None, source_reference: str = "",
                 verified_by: str = "", notes: str = "") -> None:
    ensure_schema(app)
    now = app.now_stamp()
    conn = app.local_connect()
    try:
        conn.execute(
            """INSERT INTO accuracy_ground_truth(workspace_id,page_id,category,item_key,expected_numeric,expected_text,unit,
                     source_reference,verified_by,verification_notes,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(workspace_id,category,item_key) DO UPDATE SET
                 page_id=excluded.page_id, expected_numeric=excluded.expected_numeric, expected_text=excluded.expected_text,
                 unit=excluded.unit, source_reference=excluded.source_reference, verified_by=excluded.verified_by,
                 verification_notes=excluded.verification_notes, updated_at=excluded.updated_at""",
            (int(workspace_id), page_id, str(category), str(item_key), expected_numeric, str(expected_text or ""), str(unit or ""),
             str(source_reference or ""), str(verified_by or ""), str(notes or ""), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def record_prediction(app: Any, workspace_id: int, category: str, item_key: str, *, predicted_numeric: Optional[float] = None,
                      predicted_text: str = "", unit: str = "", page_id: Optional[int] = None, confidence: float = 0.0,
                      method: str = "", evidence: Any = None, engine_version: str = VERSION) -> None:
    ensure_schema(app)
    conn = app.local_connect()
    try:
        conn.execute(
            """INSERT INTO accuracy_predictions(workspace_id,page_id,category,item_key,predicted_numeric,predicted_text,unit,
                     confidence,method,evidence_json,engine_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(workspace_id), page_id, str(category), str(item_key), predicted_numeric, str(predicted_text or ""), str(unit or ""),
             float(confidence or 0), str(method or ""), json.dumps(evidence or {}), str(engine_version), app.now_stamp()),
        )
        conn.commit()
    finally:
        conn.close()


def _latest_predictions(app: Any, workspace_id: int, engine_version: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_schema(app)
    params: List[Any] = [int(workspace_id)]
    where = "workspace_id=?"
    if engine_version:
        where += " AND engine_version=?"
        params.append(str(engine_version))
    rows = app.lquery(
        f"""SELECT p.* FROM accuracy_predictions p
             JOIN (SELECT category,item_key,MAX(id) AS max_id FROM accuracy_predictions WHERE {where} GROUP BY category,item_key) latest
             ON p.id=latest.max_id ORDER BY p.category,p.item_key""",
        tuple(params),
    )
    return rows


def evaluate_workspace(app: Any, workspace_id: int, engine_version: Optional[str] = None) -> Dict[str, Any]:
    ensure_schema(app)
    truth = app.lquery("SELECT * FROM accuracy_ground_truth WHERE workspace_id=? ORDER BY category,item_key", (int(workspace_id),))
    preds = _latest_predictions(app, workspace_id, engine_version)
    pred_map = {(str(r.get("category")), str(r.get("item_key"))): r for r in preds}
    categories: Dict[str, Dict[str, Any]] = {}
    details: List[Dict[str, Any]] = []
    for t in truth:
        category, key = str(t.get("category")), str(t.get("item_key"))
        p = pred_map.get((category, key))
        entry = categories.setdefault(category, {"count": 0, "matched": 0, "abs_pct_errors": [], "correct": 0})
        entry["count"] += 1
        detail = {"category": category, "item_key": key, "matched": bool(p), "expected": t.get("expected_numeric") if t.get("expected_numeric") is not None else t.get("expected_text")}
        if not p:
            detail["error"] = "missing_prediction"
            details.append(detail)
            continue
        entry["matched"] += 1
        if t.get("expected_numeric") is not None:
            expected = _num(t.get("expected_numeric"))
            predicted = _num(p.get("predicted_numeric"))
            abs_error = abs(predicted - expected)
            pct_error = abs_error / abs(expected) if abs(expected) > 1e-9 else (0.0 if abs(predicted) <= 1e-9 else 1.0)
            entry["abs_pct_errors"].append(pct_error)
            detail.update({"predicted": predicted, "absolute_error": abs_error, "percent_error": pct_error * 100.0})
        else:
            expected = str(t.get("expected_text") or "").strip().casefold()
            predicted = str(p.get("predicted_text") or "").strip().casefold()
            correct = expected == predicted
            entry["correct"] += 1 if correct else 0
            detail.update({"predicted": p.get("predicted_text"), "correct": correct})
        detail["confidence"] = _num(p.get("confidence"))
        detail["method"] = p.get("method")
        details.append(detail)

    summary = {}
    for category, raw in categories.items():
        count = max(1, int(raw["count"]))
        matched_rate = raw["matched"] / count
        mape = sum(raw["abs_pct_errors"]) / len(raw["abs_pct_errors"]) if raw["abs_pct_errors"] else None
        accuracy = raw["correct"] / len([d for d in details if d["category"] == category and "correct" in d]) if any(d["category"] == category and "correct" in d for d in details) else None
        target = TARGETS.get(category)
        passes = None
        if target:
            if target["metric"] == "mape" and mape is not None:
                passes = mape <= target["target"]
            elif target["metric"] == "accuracy" and accuracy is not None:
                passes = accuracy >= target["target"]
        summary[category] = {"count": raw["count"], "matched_rate": matched_rate, "mape": mape, "accuracy": accuracy, "target": target, "passes_target": passes}
    return {"workspace_id": int(workspace_id), "engine_version": engine_version or "latest", "categories": summary, "details": details, "ground_truth_count": len(truth)}


def record_vector_analysis(app: Any, workspace_id: int, page_id: int, analysis: Dict[str, Any]) -> None:
    scale = analysis.get("scale") or {}
    if _num(scale.get("px_per_m")) > 0:
        record_prediction(app, workspace_id, "scale", f"page:{page_id}", predicted_numeric=_num(scale.get("px_per_m")), unit="px/m",
                          page_id=page_id, confidence=_num(scale.get("confidence")), method="vector_scale_solver", evidence=scale, engine_version=VERSION)
    record_prediction(app, workspace_id, "wall_pair_count", f"page:{page_id}", predicted_numeric=float(analysis.get("wall_pair_count") or 0), unit="count",
                      page_id=page_id, confidence=85 if analysis.get("wall_pair_count") else 40, method="native_vector_wall_pairs", evidence=analysis.get("graph") or {}, engine_version=VERSION)


def export_truth(app: Any, workspace_id: int) -> Dict[str, Any]:
    ensure_schema(app)
    rows = app.lquery("SELECT page_id,category,item_key,expected_numeric,expected_text,unit,source_reference,verified_by,verification_notes FROM accuracy_ground_truth WHERE workspace_id=? ORDER BY category,item_key", (int(workspace_id),))
    return {"schema": "pb-planreader-ground-truth-v1", "workspace_id": int(workspace_id), "items": rows}


def import_truth(app: Any, workspace_id: int, payload: Dict[str, Any]) -> int:
    count = 0
    for item in payload.get("items") or []:
        upsert_truth(app, workspace_id, item.get("category") or "", item.get("item_key") or "", expected_numeric=item.get("expected_numeric"),
                     expected_text=item.get("expected_text") or "", unit=item.get("unit") or "", page_id=item.get("page_id"),
                     source_reference=item.get("source_reference") or "", verified_by=item.get("verified_by") or "", notes=item.get("verification_notes") or "")
        count += 1
    return count


def apply(app: Any) -> None:
    if getattr(app, "_pb_accuracy_benchmark_v130_applied", False):
        return
    app._pb_accuracy_benchmark_v130_applied = True
    ensure_schema(app)
    app.accuracy_upsert_truth_v130 = lambda *args, **kwargs: upsert_truth(app, *args, **kwargs)
    app.accuracy_record_prediction_v130 = lambda *args, **kwargs: record_prediction(app, *args, **kwargs)
    app.accuracy_evaluate_workspace_v130 = lambda *args, **kwargs: evaluate_workspace(app, *args, **kwargs)
    app.accuracy_record_vector_analysis_v130 = lambda *args, **kwargs: record_vector_analysis(app, *args, **kwargs)
    app.accuracy_export_truth_v130 = lambda workspace_id: export_truth(app, workspace_id)
    app.accuracy_import_truth_v130 = lambda workspace_id, payload: import_truth(app, workspace_id, payload)
