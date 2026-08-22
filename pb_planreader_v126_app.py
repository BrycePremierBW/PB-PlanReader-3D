"""Production entry point for Premier Brushworks PlanReader v1.3.2."""

import pb_planreader_v11_app as launcher
from pb_gemini_v126 import apply as apply_gemini_v126
from pb_floor_mapper_v128 import apply as apply_floor_mapper_v128
from pb_processing_stability_v129 import apply as apply_processing_stability_v129
from pb_takeoff_studio_v1211 import apply as apply_takeoff_studio_v1211
from pb_3d_surface_editor_v1212 import apply as apply_3d_surface_editor_v1212
from pb_studio_path_guard_v1213 import apply as apply_studio_path_guard_v1213
from pb_3d_quickstart_v1213 import apply as apply_3d_quickstart_v1213
from pb_3d_wrapper_guard_v1214 import apply as apply_3d_wrapper_guard_v1214
from pb_performance_v1215 import apply as apply_performance_v1215
from pb_db_init_guard_v1215 import apply as apply_db_init_guard_v1215
from pb_subscription_core_guard_v1218 import apply as apply_subscription_core_guard_v1218
from pb_no_ai_takeoff_v1216 import apply as apply_no_ai_takeoff_v1216
from pb_selected_pages_v1217 import apply as apply_selected_pages_v1217
from pb_auto_geometry_v1219 import apply as apply_auto_geometry_v1219
from pb_auto_geometry_guard_v1219 import apply as apply_auto_geometry_guard_v1219
from pb_memory_stability_v1220 import apply as apply_memory_stability_v1220
from pb_unit_floor_area_v1221 import apply as apply_unit_floor_area_v1221
from pb_unit_floor_area_gate_v1221 import apply as apply_unit_floor_area_gate_v1221
from pb_unit_floor_area_textfix_v1221 import apply as apply_unit_floor_area_textfix_v1221
from pb_material_schedule_v1222 import apply as apply_material_schedule_v1222
from pb_autopilot_v1223 import apply as apply_autopilot_v1223
from pb_autopilot_upload_batch_v1223 import apply as apply_autopilot_upload_batch_v1223
from pb_autopilot_accuracy_guard_v1223 import apply as apply_autopilot_accuracy_guard_v1223
from pb_context_floorarea_v1224 import apply as apply_context_floorarea_v1224
from pb_page_registration_v1225 import apply as apply_page_registration_v1225
from pb_registration_priority_guard_v1225 import apply as apply_registration_priority_guard_v1225
from pb_code_register_v1225 import apply as apply_code_register_v1225
from pb_premier_takeoff_v1225 import apply as apply_premier_takeoff_v1225
from pb_drawing_reading_v1226 import apply as apply_drawing_reading_v1226
from pb_selection_lock_v1226 import apply as apply_selection_lock_v1226
from pb_selected_evidence_floor_v1226 import apply as apply_selected_evidence_floor_v1226
from pb_elevation_regions_v1226 import apply as apply_elevation_regions_v1226
from pb_takeoff_review_v1226 import apply as apply_takeoff_review_v1226
from pb_legend_register_v1227 import apply as apply_legend_register_v1227
from pb_plan_read_engine_v1228 import apply as apply_plan_read_engine_v1228
from pb_mapper_hard_guard_v1228 import apply as apply_mapper_hard_guard_v1228
from pb_persistent_login_v1229 import apply as apply_persistent_login_v1229
from pb_vector_geometry_v130 import apply as apply_vector_geometry_v130
from pb_accuracy_benchmark_v130 import apply as apply_accuracy_benchmark_v130
from pb_accuracy_ui_v130 import apply as apply_accuracy_ui_v130
from pb_substrate_qa_v131 import apply as apply_substrate_qa_v131
from pb_precision_3d_v132 import apply as apply_precision_3d_v132


apply_gemini_v126(launcher.app)
apply_floor_mapper_v128(launcher.app)
apply_processing_stability_v129(launcher.app)
apply_takeoff_studio_v1211(launcher.app)
apply_3d_surface_editor_v1212(launcher.app)
apply_studio_path_guard_v1213(launcher.app)
apply_3d_quickstart_v1213(launcher.app)
apply_3d_wrapper_guard_v1214(launcher.app)
apply_performance_v1215(launcher.app)
apply_db_init_guard_v1215(launcher.app)
apply_subscription_core_guard_v1218(launcher.app)
apply_no_ai_takeoff_v1216(launcher.app)
apply_selected_pages_v1217(launcher.app)
apply_auto_geometry_v1219(launcher.app)
apply_auto_geometry_guard_v1219(launcher.app)
apply_memory_stability_v1220(launcher.app)
apply_unit_floor_area_v1221(launcher.app)
apply_unit_floor_area_gate_v1221(launcher.app)
apply_unit_floor_area_textfix_v1221(launcher.app)
apply_material_schedule_v1222(launcher.app)
apply_autopilot_v1223(launcher.app)
apply_autopilot_upload_batch_v1223(launcher.app)
apply_autopilot_accuracy_guard_v1223(launcher.app)
apply_context_floorarea_v1224(launcher.app)
apply_page_registration_v1225(launcher.app)
apply_registration_priority_guard_v1225(launcher.app)
apply_code_register_v1225(launcher.app)
apply_premier_takeoff_v1225(launcher.app)
apply_drawing_reading_v1226(launcher.app)
apply_selection_lock_v1226(launcher.app)
apply_selected_evidence_floor_v1226(launcher.app)
apply_elevation_regions_v1226(launcher.app)
apply_takeoff_review_v1226(launcher.app)
apply_legend_register_v1227(launcher.app)
apply_plan_read_engine_v1228(launcher.app)
apply_mapper_hard_guard_v1228(launcher.app)
apply_persistent_login_v1229(launcher.app)
# v1.3.0 introduces the measurable accuracy foundation. Native PDF vectors are
# converted into a geometry graph; scale is solved from independent evidence;
# estimator-verified ground truth is stored separately and scored by category.
apply_vector_geometry_v130(launcher.app)
apply_accuracy_benchmark_v130(launcher.app)
apply_accuracy_ui_v130(launcher.app)
# v1.3.1 adds close-up polygon editing and a whole-building substrate QA model.
# Elevations/finish schedules remain authoritative; artist impressions are only
# secondary visual evidence and conflicts remain flagged for estimator checking.
apply_substrate_qa_v131(launcher.app)
# v1.3.2 makes the visible 3D building use the same calibrated editable plan
# polygons as floor take-off, instead of reducing irregular plans to cuboids.
apply_precision_3d_v132(launcher.app)
launcher.app.APP_VERSION = "1.3.2"


if __name__ == "__main__":
    launcher.app.main()
