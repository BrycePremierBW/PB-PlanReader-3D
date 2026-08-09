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

def add_row(section, element, location, qty, unit, rate, ref="", notes="", status="Measured"):
    return app.lexecute(
        """INSERT INTO takeoff_rows(workspace_id,section,element,location,substrate,finish_system,quantity,unit,quantity_status,source_page,source_reference,inclusion_status,coats,coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,notes,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (wid, section, element, location, "Gyprock", "Dulux system", qty, unit, status, "",
         ref, "Included", 2, 12, 8, rate, "Measured", notes, app.now_stamp(), app.now_stamp()),
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

print("CI SMOKE TEST PASSED")
sys.exit(0)
