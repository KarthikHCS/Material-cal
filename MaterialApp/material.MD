# Material data source (Inventory Dashboard v1.7)

Material details for the Material Usage Calculator come from the **Inventory Dashboard v1.7** Excel file. The app only *refers* to this file; it never edits it.

## Rules

- **Read-only.** No Excel file is overwritten, saved or updated while information is copied into the app. The dashboard is used for reference only.
- **Refresh before fetch.** The dashboard is saved empty and fills from the live database (GLOBAL_PLA, ODBC) when refreshed, so every fetch refreshes first to get the latest data.
- **Fail safe.** If the refresh fails or returns no rows, nothing is exported; stale data is never passed off as fresh.

## How it works

Script: `C:\Users\karthik\Documents\MaterialUsageCalculator\refresh_materials.ps1`

1. Copies `M:\Excel Dashboard\Inventory Dashboard v1.7.xlsx` to a temp folder (the only operation on the source file).
2. Opens the **copy** in Excel, refreshes all queries and waits for them to finish.
3. Exports each sheet to CSV, then closes the copy **without saving** and deletes it.
4. Compares the source file's size and modified time before and after, and warns if it changed.

Run: `powershell -File refresh_materials.ps1 [-Source <dashboard path>] [-OutDir <folder>]`

Requirements: Excel installed, network access to the database server (172.16.100.10).

## Output (`MaterialUsageCalculator\materials\`)

| File | Content |
|---|---|
| `Inventory_All.csv` | Part master: GSS_PART, PART_NUMBER, DESCRIPTION, BIN, UM_INV, UM_PUR, PUR_CONV, QTY_ONHAND / RESERVED / AVAILABLE / ONPO / ONWO / REQUIRED / SAFETY / ORDER / REORDER, PROD_LINE, PROJECT, INACTIVE, BKFLUSH, SORT_CODE, LEAD_TIME, DATE_CHG, CHG_BY, DATE_ADD, ADD_BY |
| `Customer_Part.csv` | Customer part cross-reference (GSS_INV_PART, INVENTORY_PART, CUSTOMER_PART, DESCRIPTION, USER_DEFINED_PART, COMMENT) |
| `Project_List.csv` | PROJECT_CODE, PROJECT_DESCRIPTION |
| `Product_Line.csv` | PROD_LN, SHORT, PROD_DESCRIPTION |
| `Unit_Measure.csv` | UOM, UOM_DESCRIPTION |
| `Inv_Sort_code.csv` | SORT_CODE, DESCRIPTION, USAGE |
| `refresh_info.json` | Refresh time, source path, row counts |

Last refresh recorded: 2026-10-28 13:58:53 (Inventory_All: 63,355 rows).

## Status

The app code (`app.py`, `engine.py`, ...) does not yet read these CSVs. Hooking it up means running the refresh script before a material lookup, then reading `Inventory_All.csv`.
