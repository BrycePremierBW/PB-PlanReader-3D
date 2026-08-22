"""Production entry point for PlanReader v1.3.3 simplified bright workflow."""

import pb_planreader_v126_app as base
from pb_simple_ui_v133 import apply as apply_simple_ui_v133


apply_simple_ui_v133(base.launcher.app)
base.launcher.app.APP_VERSION = "1.3.3"


if __name__ == "__main__":
    base.launcher.app.main()
