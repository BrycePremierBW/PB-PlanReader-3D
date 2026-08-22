"""Production entry point for PlanReader guided UI on full reconstruction stack."""

import pb_planreader_reconstruction_v139_app as base
from pb_guided_workflow_v144 import apply as apply_guided_workflow_v144

app = base.base.launcher.app
# Keep the original PlanReader colour/theme styling; only replace the navigation/process
# with the simpler guided workflow.
apply_guided_workflow_v144(app)
app.APP_VERSION = "1.4.5"

if __name__ == "__main__":
    app.main()
