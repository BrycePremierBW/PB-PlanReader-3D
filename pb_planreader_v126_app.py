"""Production entry point for Premier Brushworks PlanReader v1.2.11."""

import pb_planreader_v11_app as launcher
from pb_gemini_v126 import apply as apply_gemini_v126
from pb_floor_mapper_v128 import apply as apply_floor_mapper_v128
from pb_processing_stability_v129 import apply as apply_processing_stability_v129
from pb_takeoff_studio_v1211 import apply as apply_takeoff_studio_v1211


apply_gemini_v126(launcher.app)
apply_floor_mapper_v128(launcher.app)
apply_processing_stability_v129(launcher.app)
apply_takeoff_studio_v1211(launcher.app)
launcher.app.APP_VERSION = "1.2.11"


if __name__ == "__main__":
    launcher.app.main()
