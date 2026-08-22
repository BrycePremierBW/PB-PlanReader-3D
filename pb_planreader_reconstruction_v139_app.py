"""PlanReader v1.3.9 reconstruction stack.

Extends the calibrated plan/elevation registration path through vertical profile,
opening geometry, substrate evidence and unified wall render/take-off.
"""

import pb_planreader_v126_app as base
from pb_elevation_profile_v136 import apply as apply_elevation_profile_v136
from pb_opening_geometry_v137 import apply as apply_opening_geometry_v137
from pb_registered_substrates_v138 import apply as apply_registered_substrates_v138
from pb_unified_building_v139 import apply as apply_unified_building_v139

apply_elevation_profile_v136(base.launcher.app)
apply_opening_geometry_v137(base.launcher.app)
apply_registered_substrates_v138(base.launcher.app)
apply_unified_building_v139(base.launcher.app)
base.launcher.app.APP_VERSION = "1.3.9"

if __name__ == "__main__":
    base.launcher.app.main()
