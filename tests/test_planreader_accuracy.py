from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ["PLANREADER_DATA_DIR"] = tempfile.mkdtemp(prefix="planreader_accuracy_")
os.environ["JOBHUB_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="planreader_accuracy_jobhub_")) / "jobhub.db")

import fitz
import pb_planreader_3d_app as app
from pb_takeoff_v11 import apply as apply_v11
from pb_takeoff_v12 import apply as apply_v12
from pb_takeoff_accuracy_v125 import apply as apply_accuracy

apply_v11(app)
apply_v12(app)
apply_accuracy(app)
app.init_local_db()


class Upload:
    def __init__(self, name: str = "takeoff.xlsx"):
        self.name = name


def add_row(wid: int, *, section="Internal", element="Walls", location="Level 1", qty=0.0,
            unit="m²", rate=10.0, status="Measured", row_role="", confidence="Manual verified",
            notes="", source_reference="Manual", inclusion="INCLUSION") -> int:
    return app.lexecute(
        """INSERT INTO takeoff_rows(
               workspace_id,section,element,location,substrate,finish_system,quantity,unit,
               quantity_status,source_page,source_reference,inclusion_status,coats,
               coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,notes,row_role,
               created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (wid, section, element, location, "Plasterboard", "Low sheen wall system", qty, unit,
         status, "A01", source_reference, inclusion, 2, 12, 8, rate, confidence, notes, row_role,
         app.now_stamp(), app.now_stamp()),
    )


def add_page(wid: int, label: str, px_per_m: float = 100.0, path: str = "", width=1000, height=700,
             extracted_text: str = "") -> int:
    did = app.lexecute(
        """INSERT INTO documents(workspace_id,source_type,file_name,mime_type,path,sha256,category,page_count,uploaded_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (wid, "Test", f"{label}.pdf", "application/pdf", path, f"hash-{label}-{wid}", "Plans", 1, app.now_stamp()),
    )
    return app.lexecute(
        """INSERT INTO pages(document_id,workspace_id,page_no,page_label,page_type,scale_text,px_per_m,image_path,
                              width_px,height_px,extracted_text,selected,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (did, wid, 1, label, "Floor Plan", "", px_per_m, "", width, height, extracted_text, 1, app.now_stamp()),
    )


def line(row_id: int, length: float, *, page_width_px: float = 1000.0, px_per_m: float = 100.0):
    pct = length * px_per_m / page_width_px * 100.0
    return {
        "takeoff_row_id": row_id, "label": "wall", "unit": "lm", "kind": "line",
        "x1": 0.0, "y1": 10.0, "x2": pct, "y2": 10.0,
        "length_m": length, "area_m2": 0, "perimeter_m": 0, "moved": 1,
    }


def rect_poly(row_id: int, width_m: float, height_m: float, *, page_width_px: float = 1000.0,
              page_height_px: float = 700.0, px_per_m: float = 100.0, basis: str = "direct_area"):
    x = width_m * px_per_m / page_width_px * 100.0
    y = height_m * px_per_m / page_height_px * 100.0
    return {
        "takeoff_row_id": row_id, "label": "area", "unit": "m2", "kind": "polygon",
        "points": [[0, 0], [x, 0], [x, y], [0, y]],
        "length_m": 0, "area_m2": width_m * height_m, "perimeter_m": 2*(width_m+height_m), "moved": 1,
        "measurement_basis": basis,
    }


class MeasurementAggregationTests(unittest.TestCase):
    def setUp(self):
        self.wid = app.create_standalone_workspace("ACC", "Accuracy", "PB", "1 Test St")
        self.p1 = add_page(self.wid, "A01", 100.0)
        self.p2 = add_page(self.wid, "A02", 100.0)

    def test_multiple_shapes_and_pages_sum_then_restore(self):
        rid = add_row(self.wid, element="Skirting", qty=11, unit="lm", rate=5)
        app.save_measurement_lines(self.wid, self.p1, [line(rid, 3), line(rid, 4)])
        row = app.lquery("SELECT quantity,pre_map_quantity FROM takeoff_rows WHERE id=?", (rid,))[0]
        self.assertAlmostEqual(float(row["quantity"]), 7.0)
        self.assertAlmostEqual(float(row["pre_map_quantity"]), 11.0)
        app.save_measurement_lines(self.wid, self.p2, [line(rid, 5)])
        self.assertAlmostEqual(float(app.lquery("SELECT quantity FROM takeoff_rows WHERE id=?", (rid,))[0]["quantity"]), 12.0)
        app.save_measurement_lines(self.wid, self.p1, [line(rid, 2)])
        self.assertAlmostEqual(float(app.lquery("SELECT quantity FROM takeoff_rows WHERE id=?", (rid,))[0]["quantity"]), 7.0)
        app.lexecute("DELETE FROM measurement_lines WHERE page_id=?", (self.p2,))
        self.assertAlmostEqual(float(app.lquery("SELECT quantity FROM takeoff_rows WHERE id=?", (rid,))[0]["quantity"]), 2.0)
        app.lexecute("DELETE FROM measurement_lines WHERE page_id=?", (self.p1,))
        row = app.lquery("SELECT quantity,quantity_status FROM takeoff_rows WHERE id=?", (rid,))[0]
        self.assertAlmostEqual(float(row["quantity"]), 11.0)
        self.assertEqual(str(row["quantity_status"]), "Measured")

    def test_changing_page_scale_recomputes_saved_geometry_server_side(self):
        rid = add_row(self.wid, element="Skirting", qty=0, unit="lm", rate=5, status="To measure")
        app.save_measurement_lines(self.wid, self.p1, [line(rid, 3)])
        self.assertAlmostEqual(float(app.lquery("SELECT quantity FROM takeoff_rows WHERE id=?", (rid,))[0]["quantity"]), 3.0)
        app.lexecute("UPDATE pages SET px_per_m=? WHERE id=?", (50.0, self.p1))
        self.assertAlmostEqual(float(app.lquery("SELECT quantity FROM takeoff_rows WHERE id=?", (rid,))[0]["quantity"]), 6.0)
        self.assertAlmostEqual(float(app.lquery("SELECT length_m FROM measurement_lines WHERE page_id=?", (self.p1,))[0]["length_m"]), 6.0)

    def test_area_polygons_sum_and_horizontal_external_area_stays_direct(self):
        rid = add_row(self.wid, section="External", element="Soffit", qty=1, unit="m²", rate=30)
        app.save_measurement_lines(self.wid, self.p1, [rect_poly(rid, 2, 5), rect_poly(rid, 3, 5)])
        self.assertAlmostEqual(float(app.lquery("SELECT quantity FROM takeoff_rows WHERE id=?", (rid,))[0]["quantity"]), 25.0)

    def test_auto_envelope_basis_uses_project_wall_height_only_when_explicit(self):
        app.set_workspace_setting(self.wid, "default_wall_height_m", 3.2)
        rid = add_row(self.wid, section="External", element="External walls / cladding", qty=0, unit="m²", rate=42)
        app.save_measurement_lines(self.wid, self.p1, [rect_poly(rid, 2, 3, basis="footprint_perimeter_height")])
        self.assertAlmostEqual(float(app.lquery("SELECT quantity FROM takeoff_rows WHERE id=?", (rid,))[0]["quantity"]), 32.0)


class ScaleTests(unittest.TestCase):
    def test_auto_scale_uses_actual_capped_render_zoom(self):
        wid = app.create_standalone_workspace("SCALE", "Scale", "PB", "")
        tmp = Path(tempfile.mkdtemp(prefix="planreader_pdf_")) / "large.pdf"
        doc = fitz.open(); page = doc.new_page(width=3000, height=2000); page.insert_text((72, 72), "DRAWING SCALE 1:100"); doc.save(tmp); doc.close()
        zoom = min(1.7, app._PDF_RENDER_LONG_EDGE_PX / 3000.0)
        pid = add_page(wid, "A100", 0.0, str(tmp), int(round(3000*zoom)), int(round(2000*zoom)), "DRAWING SCALE 1:100")
        page_row = app.lquery("SELECT p.*,d.path FROM pages p JOIN documents d ON d.id=p.document_id WHERE p.id=?", (pid,))[0]
        detected = app.auto_detect_scale(page_row)
        self.assertIsNotNone(detected)
        self.assertAlmostEqual(float(detected["px_per_m"]), zoom * 1000.0 / (0.352778 * 100), places=2)
        app.lexecute("UPDATE pages SET px_per_m=? WHERE id=?", (float(detected["px_per_m"]), pid))
        meta = app.lquery("SELECT scale_method,scale_verified FROM pages WHERE id=?", (pid,))[0]
        self.assertEqual(meta["scale_method"], "auto_detected"); self.assertEqual(int(meta["scale_verified"]), 0)
        rid = add_row(wid, element="Skirting", qty=0, unit="lm", rate=5, status="To measure", source_reference="A100")
        app.save_measurement_lines(wid, pid, [line(rid, 2, page_width_px=2400, px_per_m=float(detected["px_per_m"]))])
        self.assertTrue(any(i["code"] == "UNVERIFIED_SCALE" for i in app.takeoff_accuracy_issues(wid)))
        app.lexecute("UPDATE pages SET px_per_m=? WHERE id=?", (float(detected["px_per_m"]), pid))
        meta = app.lquery("SELECT scale_method,scale_verified FROM pages WHERE id=?", (pid,))[0]
        self.assertEqual(meta["scale_method"], "manual_calibration"); self.assertEqual(int(meta["scale_verified"]), 1)
        self.assertFalse(any(i["code"] == "UNVERIFIED_SCALE" for i in app.takeoff_accuracy_issues(wid)))


class ImportAccuracyTests(unittest.TestCase):
    def test_floor_area_column_does_not_replace_work_quantity(self):
        frame, _ = app.parse_takeoff_file(Upload(), raw_headers=["Element", "Area / Location", "Qty m²", "Floor area (m²)", "Unit"], body=[["Walls", "Unit 1 · Level 1", 80, 150, "m²"]])
        self.assertEqual(len(frame), 2)
        work, floor = frame.loc[frame["row_role"].eq("")], frame.loc[frame["row_role"].eq("floor_area")]
        self.assertAlmostEqual(float(work.iloc[0]["quantity"]), 80.0); self.assertAlmostEqual(float(floor.iloc[0]["quantity"]), 150.0); self.assertEqual(float(floor.iloc[0]["rate_per_unit"]), 0.0)

    def test_blank_floor_column_leaves_work_row_alone_and_total_row_is_skipped(self):
        frame, _ = app.parse_takeoff_file(Upload(), raw_headers=["Element", "Area / Location", "Qty m²", "Floor area (m²)", "Unit"], body=[["Walls", "Level 1", 80, "", "m²"], ["TOTAL", "Level 1", 80, 150, "m²"]])
        self.assertEqual(len(frame), 1); self.assertEqual(frame.iloc[0]["row_role"], ""); self.assertAlmostEqual(float(frame.iloc[0]["quantity"]), 80.0)


class FloorPricingTests(unittest.TestCase):
    def test_floor_pricing_isolated_by_unit_and_not_multiplied_by_wall_rows(self):
        wid = app.create_standalone_workspace("FLOOR", "Floor pricing", "PB", ""); app.set_workspace_setting(wid, "internal_pricing_basis", "floor_m2")
        add_row(wid, element="Floor area", location="Unit 1 · Level 1", qty=100, rate=0, row_role="floor_area"); add_row(wid, element="Floor area", location="Unit 2 · Level 1", qty=200, rate=0, row_role="floor_area")
        r11=add_row(wid,element="Internal walls",location="Unit 1 · Level 1 · bedrooms",qty=80,rate=10); r12=add_row(wid,element="Internal walls",location="Unit 1 · Level 1 · living",qty=20,rate=10); r2=add_row(wid,element="Internal walls",location="Unit 2 · Level 1",qty=300,rate=10)
        frame=app.dataframe_for_takeoff(wid).set_index("id")
        self.assertAlmostEqual(float(frame.loc[r11,"priced_quantity"]),80.0); self.assertAlmostEqual(float(frame.loc[r12,"priced_quantity"]),20.0); self.assertAlmostEqual(float(frame.loc[r2,"priced_quantity"]),200.0)

    def test_duplicate_exact_floor_reference_does_not_double_price(self):
        wid=app.create_standalone_workspace("FDUP","Floor dup","PB",""); app.set_workspace_setting(wid,"internal_pricing_basis","floor_m2")
        add_row(wid,element="Floor area",location="Unit 1 · Level 1",qty=100,rate=0,row_role="floor_area"); add_row(wid,element="Floor area",location="Unit 1 · Level 1",qty=100,rate=0,row_role="floor_area"); wall=add_row(wid,element="Internal walls",location="Unit 1 · Level 1",qty=80,rate=10)
        self.assertAlmostEqual(float(app.dataframe_for_takeoff(wid).set_index("id").loc[wall,"priced_quantity"]),100.0)


class AutoMapperTests(unittest.TestCase):
    def test_floor_mapper_row_is_reference_and_unit_is_canonical(self):
        wid=app.create_standalone_workspace("AUTO","Auto","PB",""); rid=app._ensure_mapper_row(wid,"Internal","Floor area","Unit 1 · Level 1 · floor area","Concrete floor","","m²","A01")
        row=app.lquery("SELECT element,row_role,unit,rate_per_unit FROM takeoff_rows WHERE id=?",(rid,))[0]
        self.assertEqual(row["element"],"Floor area"); self.assertEqual(row["row_role"],"floor_area"); self.assertEqual(row["unit"],"m²"); self.assertAlmostEqual(float(row["rate_per_unit"]),0.0)

    def test_placeholder_generator_excludes_count_rows(self):
        wid=app.create_standalone_workspace("AUTO2","Auto2","PB",""); pid=add_page(wid,"A01"); add_row(wid,element="Walls",unit="m²",qty=1); add_row(wid,element="Doors",unit="No.",qty=3)
        lines=app.auto_map_measurements(wid,pid,100.0); self.assertEqual(len(lines),1); self.assertEqual(lines[0]["unit"],"m2")


class AIBaselineAndGateTests(unittest.TestCase):
    def test_ai_baseline_survives_mapping_and_reconciles_against_drawn(self):
        wid=app.create_standalone_workspace("AI","AI","PB",""); pid=add_page(wid,"A01",100.0,width=2000,height=1000)
        data={"executive_summary":"","drawing_issue":"","takeoff_rows":[{"section":"Internal","element":"Internal walls","location":"Level 1","substrate":"Plasterboard","finish_system":"Low sheen wall system","quantity":100,"unit":"m²","quantity_status":"Measured","source_page":"A01","source_reference":"A01 wall plan","inclusion_status":"INCLUSION","coats":2,"coverage_m2_per_litre":12,"productivity_m2_per_hour":8,"rate_per_unit":0,"confidence":"Derived","notes":"","row_role":""}],"register_items":[],"model_masses":[],"model_openings":[],"unknowns":[]}
        counts=app.import_ai_result(wid,data); self.assertEqual(counts["takeoff"],1); row=app.lquery("SELECT * FROM takeoff_rows WHERE workspace_id=?",(wid,))[0]; rid=int(row["id"])
        self.assertEqual(row["origin"],"AI"); self.assertAlmostEqual(float(row["ai_baseline_quantity"]),100.0); self.assertEqual(row["quantity_status"],"Provisional measured")
        app.save_measurement_lines(wid,pid,[rect_poly(rid,6,10,page_width_px=2000,page_height_px=1000)]); reco=app.reconcile_ai_vs_drawn(wid)
        self.assertAlmostEqual(float(reco.iloc[0]["ai_qty"]),100.0); self.assertAlmostEqual(float(reco.iloc[0]["drawn_qty"]),60.0); self.assertEqual(reco.iloc[0]["status"],"Difference")

    def test_accuracy_gate_blocks_unverified_ai_before_publish(self):
        wid=app.create_standalone_workspace("BLOCK","Block","PB",""); add_row(wid,element="Walls",qty=50,unit="m²",rate=10,confidence="Derived",notes="AI draft",source_reference="A01")
        issues=app.takeoff_accuracy_issues(wid); self.assertTrue(any(i["code"]=="AI_UNVERIFIED" and i["severity"]=="Critical" for i in issues))
        with self.assertRaisesRegex(RuntimeError,"accuracy gate blocked"): app.publish_job_to_jobhub(wid,object(),"CI")


if __name__ == "__main__":
    unittest.main()
