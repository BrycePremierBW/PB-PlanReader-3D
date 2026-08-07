# PB PlanReader v1.1 — take-off / JobHub fix

This release changes PlanReader's take-off behaviour without rewriting the large v1.0 application.

## What changes

- The AI is instructed to work as a Premier Brushworks painting estimator rather than return a generic trade summary.
- AI take-offs are quantity-only by default; `rate_per_unit` is forced to zero on AI import.
- Each measured line must explain its Premier Brushworks calculation in `notes` using Base → Factor → Gross → Deduction → Adjustment → Net, together with height, colour and access flags.
- Internal walls, ceilings/bulkheads, doors, trims, external walls/cladding, soffits/eaves and specialist coatings are separated where evidence supports them.
- Factory-finished / powder-coated / aluminium / Colorbond items are not silently treated as painter-applied work.
- Colour schedules, RFIs, assumptions, exclusions and access issues are explicitly requested from the AI.
- The selected drawing/specification set is analysed in batches of eight pages with a project-wide text basis, instead of effectively relying on only the first six pages.
- JobHub import now recognises both live `job_takeoff_rows` and the latest `painting_takeoff_packages` / `painting_takeoff_lines` snapshot, preferring live rows when duplicates exist.
- The existing v1.0 application remains intact. `pb_planreader_v11_app.py` applies the behaviour overlay at startup.

## Validation performed

- Python syntax compilation passed for the readable v1.1 overlay and launcher before packaging.
- Mock JobHub import test passed for live rows + package rows + duplicate suppression.
- Render and Windows launch commands now start `pb_planreader_v11_app.py`.

Production database and live AI calls still need a real-job smoke test after deployment.
