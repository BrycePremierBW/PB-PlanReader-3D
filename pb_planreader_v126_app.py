"""Production entry point for Premier Brushworks PlanReader v1.2.6."""

import pb_planreader_v11_app as launcher
from pb_gemini_v126 import apply as apply_gemini_v126


apply_gemini_v126(launcher.app)
launcher.app.APP_VERSION = "1.2.6"


if __name__ == "__main__":
    launcher.app.main()
