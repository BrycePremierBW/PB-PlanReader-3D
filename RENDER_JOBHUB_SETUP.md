# Render JobHub database setup

PlanReader and JobHub are separate Render services, so environment variables are not shared automatically.

For the PlanReader Render service, set:

- `JOBHUB_DATABASE_URL` = the same PostgreSQL connection string used as `DATABASE_URL` on the PremierBWJobhub service.

Then redeploy PlanReader.

On PB PlanReader v1.2.1 the sidebar must show `JOBHUB CONNECTED`. If it shows `JOBHUB NOT CONNECTED`, expand `Connect JobHub` and use the session-only test field to verify the connection string before changing Render configuration.
