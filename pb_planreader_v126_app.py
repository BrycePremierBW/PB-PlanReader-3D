"""Production entry point for Premier Brushworks PlanReader v1.2.13."""

import pb_planreader_v11_app as launcher
from pb_gemini_v126 import apply as apply_gemini_v126
from pb_floor_mapper_v128 import apply as apply_floor_mapper_v128
from pb_processing_stability_v129 import apply as apply_processing_stability_v129
from pb_takeoff_studio_v1211 import apply as apply_takeoff_studio_v1211
from pb_3d_surface_editor_v1212 import apply as apply_3d_surface_editor_v1212
from pb_studio_path_guard_v1213 import apply as apply_studio_path_guard_v1213
from pb_3d_quickstart_v1213 import apply as apply_3d_quickstart_v1213


apply_gemini_v126(launcher.app)
apply_floor_mapper_v128(launcher.app)
apply_processing_stability_v129(launcher.app)
apply_takeoff_studio_v1211(launcher.app)
apply_3d_surface_editor_v1212(launcher.app)
apply_studio_path_guard_v1213(launcher.app)
apply_3d_quickstart_v1213(launcher.app)
launcher.app.APP_VERSION = "1.2.13"


if __name__ == "__main__":
    launcher.app.main()
