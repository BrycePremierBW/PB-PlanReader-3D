"""Production entry point for Premier Brushworks PlanReader v1.1."""
import pb_planreader_3d_app as app
from pb_takeoff_v11 import apply

apply(app)

if __name__ == "__main__":
    app.main()
