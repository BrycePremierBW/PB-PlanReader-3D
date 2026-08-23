"""Production entry point for PlanReader using the original sidebar/navigation.

Keeps the v1.4.x reconstruction/accuracy stack, the familiar sidebar, the combined
Upload + Drawing Register page, narrow-screen dropdown protection, and the v1.4.9
runtime fast path for normal Streamlit reruns.
"""

import pb_planreader_reconstruction_v139_app as base
from pb_sidebar_viewport_guard_v146 import apply as apply_sidebar_viewport_guard_v146
from pb_upload_register_v147 import apply as apply_upload_register_v147
from pb_runtime_performance_v149 import apply as apply_runtime_performance_v149

app = base.base.launcher.app
apply_upload_register_v147(app)
apply_sidebar_viewport_guard_v146(app)
apply_runtime_performance_v149(app)
app.APP_VERSION = "1.4.9"

if __name__ == "__main__":
    app.main()
