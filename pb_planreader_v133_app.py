"""Production entry point for PlanReader bright UI on full reconstruction stack."""

import pb_planreader_reconstruction_v139_app as base
from pb_simple_ui_v133 import apply as apply_simple_ui_v133

apply_simple_ui_v133(base.base.launcher.app)
base.base.launcher.app.APP_VERSION = "1.4.3"

if __name__ == "__main__":
    base.base.launcher.app.main()
