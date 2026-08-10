"""CI smoke test for the PB PlanReader core batch.

Runs headless (no Streamlit server) against a temp data dir and a temp SQLite
JobHub DB. Covers the batch additions that landed with the scale gate,
per-level quoting, reconciliation, copy-across-levels and JobHub publishing.
"""
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

os.environ["PLANREADER_DATA_DIR"] = tempfile.mkdtemp(prefix="pr3d_ci_data_")
os.environ["JOBHUB_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="pr3d_ci_jobhub_"), "jobhub.db")
import sqlite3 as _sqlite3
_sqlite3.connect(os.environ["JOBHUB_DB_PATH"]).close()

import pb_planreader_3d_app as app
from PIL import Image, ImageDraw

app.init_local_db()
app._ensure_measurement_columns(app.local_connect())


def make_plan_png() -> str:
    img = Image.new("L", (1200, 800), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([200, 120, 1000, 700], outline=40, width=8)
    d.line([600, 120, 600, 700], fill=40, width=6)
    tmp = tempfile.mkdtemp(prefix="ci_plan_")
    p = os.path.join(tmp, "plan.png")
    img.save(p)
    return p


# 1. Envelope detection (pure-numpy core, no cv2 required)
det = app.auto_detect_building_envelope(make_plan_png())
assert det and det[0]["area_pct"] > 20, det
print(f"[ok] envelope detection: {len(det)} detection(s), area_pct={det[0]['area_pct']:.1f}")

# 2. Workspace + take-off rows
wid = app.create_standalone_workspace("PB25001", "CI Smoke Job", "Smoke Builder", "1 Test St")
assert wid, "workspace not created"
app.set_workspace_setting(wid, "pricing_margin_pct", 10.0)
app.set_workspace_setting(wid, "gst_rate_pct", 10.0)

def add_row(section, element, location, qty, unit, rate, ref="", notes="", status="Measured", row_role=""):
    return app.lexecute(
        """INSERT INTO takeoff_rows(workspace_id,section,element,location,substrate,finish_system,quantity,unit,quantity_status,source_page,source_reference,inclusion_status,coats,coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,notes,row_role,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (wid, section, element, location, "Gyprock", "Dulux system", qty, unit, status, "",
         ref, "Included", 2, 12, 8, rate, "Measured", notes, row_role, app.now_stamp(), app.now_stamp()),
    )

wall_ai = add_row("Internal walls", "Wall paint", "Level 1 · all walls", 100.0, "m²", 12.0, ref="AI plan review")
wall_ai2 = add_row("Internal walls", "Wall paint", "Level 1 · all walls", 95.0, "m²", 12.0, ref="AI plan review")
skirt_ground = add_row("Internal walls", "Skirting", "Ground · all skirting", 40.0, "lm", 6.0)
facade_ai = add_row("External", "Facade paint", "Level 1 · external walls", 60.0, "m²", 15.0, notes="AI draft")

# 3. Copy rows across levels
copied = app.copy_takeoff_rows_to_level(wid, [wall_ai, skirt_ground], "Level 2")
assert copied == 2, f"expected 2 copies, got {copied}"
level2 = app.ldf("SELECT * FROM takeoff_rows WHERE workspace_id=? AND location LIKE ?", (wid, "Level 2%"))
assert len(level2) == 2 and all(r.quantity == 0 for r in level2.itertuples()), "copied rows must reset quantities"
print("[ok] copy_takeoff_rows_to_level: 2 rows to Level 2, quantities reset")

# 4. Per-level summary + quote frames
levels = app.quote_summary_frame(wid)
assert not levels.empty and {"level", "value_ex_gst", "markup_ex_gst", "gst", "total_inc_gst"}.issubset(levels.columns), levels.columns
assert abs(levels["total_inc_gst"].sum() - levels["value_ex_gst"].sum() * 1.1 * 1.1) < 1e-6
print(f"[ok] per-level summary: {levels['level'].tolist()}, total inc GST ${levels['total_inc_gst'].sum():,.2f}")

csv_text = app.per_level_summary_csv(wid)
assert "level" in csv_text
print("[ok] per-level summary CSV")

# 5. Excel + PDF quotations
xlsx = app.quote_workbook_bytes(wid)
assert xlsx[:2] == b"PK", "workbook is not a zip/xlsx"
pdf = app.quote_pdf_bytes(wid)
assert pdf[:4] == b"%PDF", "quote is not a PDF"
print("[ok] quote_workbook_bytes + quote_pdf_bytes")

# 6. Reconciliation AI vs drawn
reco = app.reconcile_ai_vs_drawn(wid)
assert not reco.empty and "status" in reco.columns
ai_qty = float(reco.loc[reco["status"].eq("AI only (not drawn)"), "ai_qty"].sum())
assert ai_qty > 0, "expected an AI-only line for the external facade"
print(f"[ok] reconcile_ai_vs_drawn: {len(reco)} grouped line(s), AI-only m²={ai_qty:.1f}")

# 6b. Manual-only groups must not hit an unbound variance (first group is manual)
manual_wid = app.create_standalone_workspace("PB25002", "Manual-only Job", "b", "")
app.lexecute(
    """INSERT INTO takeoff_rows(workspace_id,section,element,location,substrate,finish_system,quantity,unit,quantity_status,source_page,source_reference,inclusion_status,coats,coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,notes,created_at,updated_at)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    (manual_wid, "Manual works", "Allowance", "Level 1 · nook", "Other", "", 25.0, "m²", "Manual",
     "", "", "Included", 2, 12, 8, 5.0, "Manual", "", app.now_stamp(), app.now_stamp()),
)
reco_manual = app.reconcile_ai_vs_drawn(manual_wid)
assert len(reco_manual) == 1, reco_manual
assert reco_manual.iloc[0]["status"] == "Manual / not yet measured" and reco_manual.iloc[0]["variance"] == 0.0
print("[ok] reconcile_ai_vs_drawn: manual-only group variance=0.0 (unbound fix)")

# 6c. Floor-m² internal pricing basis (project-level setting)
f_wid = app.create_standalone_workspace("PB25003", "Floor-basis Job", "b", "")
def add_row_w(workspace_id, section, element, location, qty, unit, rate, row_role=""):
    return app.lexecute(
        """INSERT INTO takeoff_rows(workspace_id,section,element,location,substrate,finish_system,quantity,unit,quantity_status,source_page,source_reference,inclusion_status,coats,coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,notes,row_role,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (workspace_id, section, element, location, "Gyprock", "Dulux system", qty, unit, "Measured", "",
         "", "Included", 2, 12, 8, rate, "Measured", "", row_role, app.now_stamp(), app.now_stamp()),
    )
add_row_w(f_wid, "Internal", "Wall paint", "Level 1 · all walls", 80.0, "m²", 10.0)
add_row_w(f_wid, "Internal", "Floor area", "Level 1 · all floors", 150.0, "m²", 0.0, row_role="floor_area")
app.set_workspace_setting(f_wid, "internal_pricing_basis", "floor_m2")

frame = app.dataframe_for_takeoff(f_wid)
wall_rows = frame.loc[frame["element"].eq("Wall paint")]
floor_rows = frame.loc[frame["row_role"].eq("floor_area")]
assert len(wall_rows) == 1 and float(wall_rows.iloc[0]["priced_quantity"]) == 150.0, wall_rows[["quantity", "priced_quantity"]]
assert wall_rows.iloc[0]["pricing_basis"] == "Floor m²" and abs(float(wall_rows.iloc[0]["value_ex_gst"]) - 1500.0) < 1e-6
assert len(floor_rows) == 1 and float(floor_rows.iloc[0]["value_ex_gst"]) == 0.0
assert floor_rows.iloc[0]["pricing_basis"] == "Floor area (reference)"
print("[ok] floor-m² pricing: internal wall row priced from 150 m² floor area at 10.00 = $1,500; floor row unpriced")

levels = app.per_level_summary(f_wid)
assert len(levels) == 1 and abs(float(levels.iloc[0]["floor_m2"]) - 150.0) < 1e-6, levels
assert int(levels.iloc[0]["rows"]) == 1 and abs(float(levels.iloc[0]["value_ex_gst"]) - 1500.0) < 1e-6, "floor rows must be excluded from priced rows/value"
assert abs(float(levels.iloc[0]["m2"]) - 80.0) < 1e-6, "painted m² is the measured wall m², not the priced floor area"
print("[ok] per_level_summary: floor_m2=150, value=$1,500, floor row excluded from rows/m²")

app.set_workspace_setting(f_wid, "internal_pricing_basis", "wall_m2")
frame_wall = app.dataframe_for_takeoff(f_wid)
assert abs(float(frame_wall.loc[frame_wall["element"].eq("Wall paint")].iloc[0]["value_ex_gst"]) - 800.0) < 1e-6
print("[ok] wall-m² fallback: internal wall row priced from measured 80 m² = $800")

# 6d. Import maps a 'Floor area (m²)' header to floor_area rows
parsed, warnings = app.parse_takeoff_file(None, raw_headers=["Element", "Floor area (m²)"], body=[["Internal", "150"], ["Ceiling", ""]])
assert len(parsed) == 2 and {"row_role", "quantity", "unit"}.issubset(parsed.columns), parsed.columns
assert (parsed["row_role"] == "floor_area").all() and parsed["unit"].eq("m²").all()
assert float(parsed.loc[parsed["element"].eq("Internal")].iloc[0]["quantity"]) == 150.0
print("[ok] parse_takeoff_file: 'Floor area (m²)' column maps rows to row_role='floor_area', m² and quantity")

parsed1, _w1 = app.parse_takeoff_file(None, raw_headers=["Element", "Qty (m²)"], body=[["Internal floor area", "150"]])
assert parsed1.iloc[0]["row_role"] == "floor_area" and float(parsed1.iloc[0]["quantity"]) == 150.0 and parsed1.iloc[0]["unit"] == "m²"
print("[ok] parse_takeoff_file: element text 'floor area' auto-tags a row as floor_area")

parsed2, _w2 = app.parse_takeoff_file(None, raw_headers=["Element", "Qty (m²)", "Unit"], body=[["Walls", "80", "m²"]])
assert parsed2.iloc[0]["row_role"] == "" and float(parsed2.iloc[0]["quantity"]) == 80.0 and parsed2.iloc[0]["unit"] == "m²"
print("[ok] parse_takeoff_file: plain 'Qty (m²)' row stays a priced row")

# 6e. Painted totals exclude floor-area rows (used by Dashboard / Quantity Schedule)
work_frame = app.takeoff_work_rows(frame)
assert float(work_frame.loc[work_frame["unit"].eq("m²"), "quantity"].sum()) == 80.0, "painted m² must exclude floor m²"
print("[ok] takeoff_work_rows: painted m² = 80 (floor 150 excluded)")

# 6f. v1.2 overlay (production importer) maps floor-area columns to floor_area rows
import pb_takeoff_v12
assert pb_takeoff_v12._EXACT_HEADER_TARGETS.get(pb_takeoff_v12._key("Floor area (m²)")) == "floor_area"
_orig_matcher = app._match_takeoff_header
try:
    app._match_takeoff_header = pb_takeoff_v12._make_matcher(app)
    v12_parse = pb_takeoff_v12.make_parse_takeoff_file(app)
    fv, _wv = v12_parse(None, raw_headers=["Element", "Floor area (m²)"], body=[["Internal", "150"], ["Ceiling", ""]])
    assert (fv["row_role"] == "floor_area").all() and fv["unit"].eq("m²").all()
    assert float(fv.loc[fv["element"].eq("Internal")].iloc[0]["quantity"]) == 150.0
    fv2, _wv2 = v12_parse(None, raw_headers=["Element", "Qty (m²)"], body=[["Internal floor area", "150"]])
    assert fv2.iloc[0]["row_role"] == "floor_area" and float(fv2.iloc[0]["quantity"]) == 150.0
    fv3, _wv3 = v12_parse(None, raw_headers=["Element", "Qty (m²)", "Unit"], body=[["Walls", "80", "m²"]])
    assert fv3.iloc[0]["row_role"] == ""
finally:
    app._match_takeoff_header = _orig_matcher
print("[ok] pb_takeoff_v12: 'Floor area (m²)' column and element text map to floor_area rows")

# 7. Publish to shared JobHub (SQLite bridge)
bridge = app.get_jobhub_bridge()
assert bridge is not None, "no JobHub bridge"
job_id = app.create_linked_jobhub_job(bridge, "PB25001", "CI Smoke Job", "1 Test St")
assert job_id, "job not created in shared DB"
app.lexecute("UPDATE workspaces SET jobhub_job_id=? WHERE id=?", (job_id, wid))
result = app.publish_job_to_jobhub(wid, bridge, "CI")
assert result["package_id"] and result["package_lines"] >= 3 and result["job_status"] == "Published"
assert result["quotation"].endswith(".xlsx") and result["progress_marker"].endswith(".zip")
jobs = bridge.query("SELECT status FROM jobs WHERE id=?", (job_id,))
assert jobs and jobs[0]["status"] == "Published", jobs
packages = bridge.query("SELECT status, total_labour_hours FROM painting_takeoff_packages WHERE job_id=?", (job_id,))
assert packages and packages[0]["status"] == "Published", packages
print(f"[ok] publish_job_to_jobhub: package #{result['package_id']}, {result['package_lines']} lines, job status {result['job_status']}")

# 7b. Floor-area rows never publish to JobHub
app.set_workspace_setting(f_wid, "internal_pricing_basis", "floor_m2")
f_job_id = app.create_linked_jobhub_job(bridge, "PB25003", "Floor-basis Job", "")
assert f_job_id, "floor-basis job not created in shared DB"
app.lexecute("UPDATE workspaces SET jobhub_job_id=? WHERE id=?", (f_job_id, f_wid))
f_result = app.publish_job_to_jobhub(f_wid, bridge, "CI")
assert f_result["package_lines"] == 1, f"expected only the wall line, got {f_result['package_lines']}"
f_rows = bridge.query("SELECT area_location, internal_external FROM job_takeoff_rows WHERE job_id=?", (f_job_id,))
assert len(f_rows) == 1 and "floor area" not in str(f_rows[0].get("area_location") or "").lower(), f_rows
print("[ok] publish_job_to_jobhub: floor-area row excluded from job_takeoff_rows and package lines")

print("CI SMOKE TEST PASSED")
sys.exit(0)
