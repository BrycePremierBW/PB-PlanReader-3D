# PB PlanReader v1.2 — PB / JobHub take-off import

This release fixes the spreadsheet import path that could silently lose Premier Brushworks take-off lines.

## Root cause

The generic importer treated the source as if it had one quantity column. PB / JobHub take-offs can carry separate `Qty m²`, `Lineal m` and `Count` columns, so rows whose quantity lived outside the first recognised quantity column could import with zero or the wrong unit. Excel import also inspected only the first worksheet.

## Changes

- Scan all Excel worksheets and the first 60 rows to locate the most likely take-off header.
- Recognise JobHub-native headings including Internal/External, Area Location, Labour Category, Substrate, Rate ex GST and Source Note.
- Preserve Qty m², Lineal m and Count as separate quantity channels.
- If more than one quantity channel is populated on a source row, preserve each as a separate take-off line rather than overwriting it.
- Reuse the Premier Brushworks v1.1 normalisation rules after spreadsheet import so imported lines land in the same estimating sections as AI-generated lines.
- Keep manual header-row and column mapping available for non-standard files.
- Show `PB TAKE-OFF v1.2 ACTIVE` in the sidebar for deployment verification.
