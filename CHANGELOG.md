# Changelog

## 2026-06-16

- Added the first local equities decision-support app.
- Added a compact browser UI served from `http/`.
- Added a Python standard-library HTTP server with SQLite-backed market-data cache.
- Added seed company records for `ADBE`, `AVGO`, `BETA`, `CRSP`, and `TSM`.
- Added SEC/Yahoo data refresh, price history storage, rolling return histograms, and source freshness metadata.
- Added transparent forecast generation with 13-week, 1-year, 3-year, 5-year, and 10-year horizons.
- Added immutable forecast ledger files under `forecasts/`.
- Added forecast evaluation and model accuracy endpoints for comparing forecasts against actual price history.
- Added decision snapshot support that references forecast IDs.
- Added unit tests for forecast math, immutable writes, and decision snapshot linkage.
