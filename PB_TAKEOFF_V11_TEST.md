# PB PlanReader v1.1 test checklist

After Render deploy:

1. Open a JobHub-linked job that already contains take-off rows.
2. In **Export / JobHub**, pull the take-off from JobHub.
3. Confirm live rows import; if live rows are absent, confirm the latest painting take-off package imports instead.
4. Open **Subscription Take-off** and confirm all currently selected drawing/specification pages are selected by default.
5. Run the AI take-off on a small known job.
6. Confirm AI rows have `rate_per_unit = 0` and notes contain a `PB CALC` Base/Factor/Gross/Deduction/Adj/Net audit trail.
7. Confirm high ceilings, grooved/profiled doors, multiple/dark colours and scaffold/EWP access are flagged when shown by the documents.
8. Confirm powder-coated/aluminium/Colorbond factory finishes are excluded or clarified unless site painting is explicitly required.
9. Confirm colour names are only populated where supported by plans/specifications.
10. Compare the result against an existing Premier Brushworks take-off before approving it back to JobHub.
