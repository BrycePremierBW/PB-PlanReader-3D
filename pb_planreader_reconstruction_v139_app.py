"""PlanReader v1.4.3 complete reconstruction stack with project scope gate and resilient 3D."""

import pb_planreader_v126_app as base
from pb_project_scope_v142 import apply as apply_project_scope_v142
from pb_scope_read_gate_v142 import apply as apply_scope_read_gate_v142
from pb_elevation_profile_v136 import apply as apply_elevation_profile_v136
from pb_opening_geometry_v137 import apply as apply_opening_geometry_v137
from pb_registered_substrates_v138 import apply as apply_registered_substrates_v138
from pb_unified_building_v139 import apply as apply_unified_building_v139
from pb_roof_envelope_v140 import apply as apply_roof_envelope_v140
from pb_full_reconstruction_v141 import apply as apply_full_reconstruction_v141
from pb_render_resilience_v143 import apply as apply_render_resilience_v143

# Scope is applied before reconstruction so non-tender buildings never enter geometry.
apply_project_scope_v142(base.launcher.app)
apply_scope_read_gate_v142(base.launcher.app)
apply_elevation_profile_v136(base.launcher.app)
apply_opening_geometry_v137(base.launcher.app)
apply_registered_substrates_v138(base.launcher.app)
apply_unified_building_v139(base.launcher.app)
apply_roof_envelope_v140(base.launcher.app)
apply_full_reconstruction_v141(base.launcher.app)
# Last wrapper: always expose the best truthful 3D preview and keep it visible even if advanced tools fail.
apply_render_resilience_v143(base.launcher.app)
base.launcher.app.APP_VERSION = "1.4.3"

if __name__ == "__main__":
    base.launcher.app.main()
