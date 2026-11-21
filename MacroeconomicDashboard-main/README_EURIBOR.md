# Daily 12-month Euribor

The finance chart and overview use `euribor12_daily`, Bank of Spain table `ti_1_7`, series `D_DNBAF172` (daily, percent). The monthly `euribor12` remains available for tables and other comparisons. Daily data may arrive with a delay and are not the official monthly mortgage reference.

The initial daily observations come from the Bank of Spain CSV; the observation date shown for the series identifies the latest available value. The update workflow fetches the table on its scheduled runs; on failure it keeps the previous daily observations and records an error.

`template.html` is reconstructed from the latest `index.html` so rebuilding preserves the Pizarra, heat map and gallery. The existing update catalogue is older than the 107-series data snapshot; series absent from that catalogue are preserved and flagged in `meta.errors`, but are not automatically refreshed until the current catalogue is supplied.

Install the package contents in the repository root, including `.github/workflows/update-data.yml` (a hidden folder on macOS). Run the Update data workflow manually in GitHub Actions, then check its log and the observation date on the dashboard. The overall refresh timestamp refers to the whole data snapshot; each series has its own observation date.

Review Banco de España and EMMI terms for redistribution and professional use of Euribor data.
