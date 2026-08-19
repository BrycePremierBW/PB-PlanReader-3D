"""Production entry point for Premier Brushworks PlanReader v1.2.19."""

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
# Capture the original Subscription Take-off function before v1.2.16/v1.2.17 wrap it.
apply_subscription_core_guard_v1218(launcher.app)
apply_no_ai_takeoff_v1216(launcher.app)
apply_selected_pages_v1217(launcher.app)
apply_auto_geometry_v1219(launcher.app)
launcher.app.APP_VERSION = "1.2.19"


if __name__ == "__main__":
    launcher.app.main()
