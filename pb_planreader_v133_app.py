"""Production entry point for PlanReader using the original sidebar/navigation.

Keeps the v1.4.x reconstruction/accuracy stack, the familiar sidebar, the combined
Upload + Drawing Register page, narrow-screen dropdown protection, runtime caches,
document-processing/ETA fast paths, and the v1.5.1 cold-start bootstrap.
"""

import os

from pb_startup_bootstrap_v151 import install as install_startup_bootstrap_v151

# Avoid importing the optional PyMuPDF4LLM/ONNX offline-analysis stack until the
# user actually opens/runs Offline Plan Reader. This must happen before the main
# PlanReader module tree is imported.
install_startup_bootstrap_v151()

# Two simultaneous MuPDF worker processes can make constrained Render instances
# slower through CPU/memory contention. Keep production serial by default while
# retaining the useful caches and ETA UI.
os.environ.setdefault("PLANREADER_RENDER_WORKERS", "1")

import pb_planreader_reconstruction_v139_app as base
from pb_sidebar_viewport_guard_v146 import apply as apply_sidebar_viewport_guard_v146
from pb_upload_register_v147 import apply as apply_upload_register_v147
from pb_runtime_performance_v149 import apply as apply_runtime_performance_v149
from pb_processing_fastpath_v150 import apply as apply_processing_fastpath_v150
from pb_material_preview_guard_v152 import apply as apply_material_preview_guard_v152

app = base.base.launcher.app
apply_upload_register_v147(app)
apply_sidebar_viewport_guard_v146(app)
apply_runtime_performance_v149(app)
apply_processing_fastpath_v150(app)
apply_material_preview_guard_v152(app)
app.APP_VERSION = "1.5.1"

if __name__ == "__main__":
    app.main()
