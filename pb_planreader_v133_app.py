"""Production entry point for PlanReader using the original sidebar/navigation.

Keeps the v1.4.x reconstruction/accuracy stack, the familiar sidebar, the combined
Upload + Drawing Register page, narrow-screen dropdown protection, the v1.4.9
runtime fast path, and the v1.5.0 document-processing/ETA fast path.
"""

import pb_planreader_reconstruction_v139_app as base
from pb_sidebar_viewport_guard_v146 import apply as apply_sidebar_viewport_guard_v146
from pb_upload_register_v147 import apply as apply_upload_register_v147
from pb_runtime_performance_v149 import apply as apply_runtime_performance_v149
from pb_processing_fastpath_v150 import apply as apply_processing_fastpath_v150

app = base.base.launcher.app
apply_upload_register_v147(app)
apply_sidebar_viewport_guard_v146(app)
apply_runtime_performance_v149(app)
apply_processing_fastpath_v150(app)
app.APP_VERSION = "1.5.0"

if __name__ == "__main__":
    app.main()
