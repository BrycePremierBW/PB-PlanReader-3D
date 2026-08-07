## Summary

Fix PlanReader so it behaves like the Premier Brushworks take-off workflow rather than a generic painting scope reader.

### Key changes
- PB quantity take-off prompt with source-based net measurements.
- Base / factor / gross / deduction / adjustment / net audit trail in row notes.
- AI pricing forced to zero by default.
- Flags high ceilings, grooved/profiled doors, multiple/dark colours and access requirements.
- Separates internal, external, doors/trims and specialist scope.
- Treats factory-finished/powder-coated/aluminium/Colorbond items as exclusions/clarifications unless site painting is stated.
- Requests colour schedule, RFIs, assumptions, exclusions and access registers.
- Analyses the selected plan/spec set in eight-page batches with shared project text instead of effectively using only the first six pages.
- JobHub pull recognises both `job_takeoff_rows` and the latest `painting_takeoff_packages` / `painting_takeoff_lines` snapshot, with live rows taking precedence on duplicates.
- Render and Windows launchers now start the v1.1 wrapper.

## Validation
- Python syntax compilation passed for the readable overlay and launcher before packaging.
- Mock JobHub import test passed for live rows, package rows and duplicate suppression.
- Production DB / live AI smoke test still required after Render deploy.
