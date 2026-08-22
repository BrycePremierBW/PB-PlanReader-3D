"""Production entry point for PlanReader guided UI on full reconstruction stack."""

import pb_planreader_reconstruction_v139_app as base
from pb_simple_ui_v133 import apply as apply_simple_ui_v133
from pb_guided_workflow_v144 import apply as apply_guided_workflow_v144

app = base.base.launcher.app
apply_simple_ui_v133(app)
apply_guided_workflow_v144(app)
app.APP_VERSION = "1.4.4"

if __name__ == "__main__":
    app.main()
