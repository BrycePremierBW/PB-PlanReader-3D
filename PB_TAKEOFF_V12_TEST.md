# PB PlanReader v1.2 import smoke test

Use a known Premier Brushworks / JobHub take-off spreadsheet.

1. Confirm the sidebar shows **PB TAKE-OFF v1.2 ACTIVE**.
2. Upload the Excel or CSV take-off in **Subscription Take-off > Import a take-off**.
3. Confirm the app selects the sheet/header containing the take-off even when it is not the first worksheet.
4. Confirm JobHub headers map as follows: Area Location -> location, Labour Category -> element, Substrate -> substrate, Rate ex GST -> rate, Source Note -> notes.
5. Confirm rows using **Qty m²** import as m².
6. Confirm rows using **Lineal m** import as lm.
7. Confirm rows using **Count** import as No.
8. Confirm a row is not discarded because Qty m² is zero when Lineal m or Count contains the quantity.
9. Confirm imported rows are classified into Premier Brushworks internal/external sections rather than generic spreadsheet categories.
10. Compare total imported m², lm and No. against the source take-off before approving the schedule.
