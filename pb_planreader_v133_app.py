"""Production entry point for PlanReader commercial estimator workflow.

Keeps the reviewed reconstruction/accuracy/3D stack and adds the Phase 6A
read-only commercial workspace shell. Measurement, deduction, calibration and
producer authority remain unchanged.
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
from pb_quick_takeoff_v153 import apply as apply_quick_takeoff_v153
from pb_takeoff_colours_v153 import apply as apply_takeoff_colours_v153
from pb_editor_ux_v154 import install as install_editor_ux_v154
from pb_opening_production_v175 import apply as apply_opening_production_v175
from pb_opening_production_guard_v175 import verify as verify_opening_production_v175
from pb_3d_workspace_integration import apply as apply_3d_canonical_integration
from pb_commercial_workspace_v160 import apply as apply_commercial_workspace_v160

install_editor_ux_v154()

app = base.base.launcher.app
apply_upload_register_v147(app)
apply_sidebar_viewport_guard_v146(app)
apply_runtime_performance_v149(app)
apply_processing_fastpath_v150(app)
apply_material_preview_guard_v152(app)
apply_quick_takeoff_v153(app)
apply_takeoff_colours_v153(app)
# Must run after the full reconstruction/accuracy stack so the safety fence
# replaces the already-bound v145 app aliases as well as module globals.
apply_opening_production_v175(app)
# This is a safety-critical integration: refuse startup if later import-order
# changes leave any known legacy automatic-deduction alias live.
verify_opening_production_v175(app)
# Install reviewed Phase 5 canonical 3D WebGL viewer extension hook.
apply_3d_canonical_integration(app)
# Phase 6A is presentation/read-only and wraps the shared workspace hero only.
apply_commercial_workspace_v160(app)
app.APP_VERSION = "1.6.0"

if __name__ == "__main__":
    app.main()
