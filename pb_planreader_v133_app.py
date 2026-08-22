"""Production entry point for PlanReader using the original sidebar/navigation.

Keeps the v1.4.x reconstruction/accuracy stack, restores the familiar sidebar,
combines Upload + Drawing Register, and guards sidebar dropdowns on narrow screens.
"""

import pb_planreader_reconstruction_v139_app as base
from pb_sidebar_viewport_guard_v146 import apply as apply_sidebar_viewport_guard_v146
from pb_upload_register_v147 import apply as apply_upload_register_v147

app = base.base.launcher.app
# Keep the original PlanReader sidebar/navigation and colour scheme.
# Only simplify the work itself: upload + drawing register live on one page.
apply_upload_register_v147(app)
# Keep sidebar controls/dropdowns inside the visible browser viewport.
apply_sidebar_viewport_guard_v146(app)
app.APP_VERSION = "1.4.7"

if __name__ == "__main__":
    app.main()
