# PB PlanReader v1.2.1 connection hotfix

- Makes Streamlit launcher wrapping idempotent so the PB TAKE-OFF badge renders once rather than multiplying on reruns.
- Shows an explicit JOBHUB CONNECTED / JOBHUB NOT CONNECTED state in the sidebar.
- Tests the bridge by actually listing database tables rather than treating the presence of a URL as a successful connection.
- Supports the same DATABASE_URL lookup pattern as JobHub via Streamlit secrets, while retaining JOBHUB_DATABASE_URL / DATABASE_URL environment support.
- Adds a session-only PostgreSQL URL test/connection field so the JobHub database can be verified before Render secrets are changed.
- Clearly identifies standalone workspaces versus JobHub-linked workspaces.

Permanent Render configuration still requires the PlanReader service's JOBHUB_DATABASE_URL secret to contain the same PostgreSQL connection string used by JobHub's DATABASE_URL.
