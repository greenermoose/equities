# Changelog

## 2026-06-16 — Valuation sanity checks and market-cap records

- Made market cap canonical as `current_share_price * shares_outstanding`, keeping any provider-reported market cap only as reference evidence.
- Added a centralized valuation sanity policy that flags impossible or suspect inputs: out-of-range share price or share count, a computed market cap above the world-largest-company ceiling ($5.0T anchor with a 10% buffer → $5.5T fail threshold), and a warning when provider and computed market cap differ by more than 25%.
- Suppressed P/S and P/E and set the usable market cap to unavailable when inputs fail sanity, while preserving the raw computed/provider values and flags in the forecast payload and surfacing each flag as a forecast warning.
- Added a `sanity_observations` SQLite table and recorded one observation per ticker per metrics refresh and policy version, deduped so board polling does not spam the table; exposed recent observations in company detail.
- Rendered the **Valuation** tab with a compact flagged state beside Current share price, Shares outstanding, and Market cap, plus a "Recent sanity observations" table.
- Confirmed the motivating case: `TSM` trips the ceiling at ~$11.10T because the NYSE ADR price (427.96) is multiplied by SEC ordinary shares (~25.93B) — a suspected ADR/share-unit mismatch that is flagged and quarantined, not auto-corrected, in this pass.
- Added unit tests for computed market cap, TSM-like failure with ratio suppression, invalid price/shares flags, provider mismatch warnings, sane-value pass-through, and observation insert/dedup.

## 2026-06-16 - Transparent valuation model

- Added a company **Valuation** tab that shows the explicit forecast formulas, horizon-by-horizon calculation values, fundamental inputs, technical indicators, confidence range inputs, and missing-data warnings.
- Upgraded the forecast model so revenue forecasts, share issuance or buyback assumptions, current shares outstanding, current price, margins, balance sheet strength, valuation ratios, analyst targets, and price-trend indicators are all exposed in the forecast payload.
- Added SEC-derived annual revenue and share-count histories, auto-derived revenue/share CAGRs, and `companies/{TICKER}.json` manual overrides for revenue growth, share-count change, and history inputs.
- Added `live_forecast`, `valuation_model`, and `calculation_trace` data to company detail responses, and preserved the same trace in recorded forecast payloads.
- Added unit tests for annual fact history extraction, CAGR calculation, manual overrides, buyback/share issuance effects, and horizon calculation traces.

## 2026-06-16 — UI/UX redesign

- Reworked the browser UI into a portfolio-wide **Signal Board** landing view that ranks every company by 13-week expected return, from strongest buy to strongest sell.
- Added a company **Snapshot** drill-in: a plain-language buy/sell verdict, a price-to-13-week forecast "cone", valuation/quality metric cards, and an auto-generated rationale.
- Added a `GET /api/board` endpoint that computes a live, non-persisted 13-week signal for each company (immutable forecast ledger files are still written only on explicit "Record forecast").
- Added search, a sort control (13-week return, conviction, price, ticker), and a data-driven filter dropdown (All / Buys / Sells) that replaces the old filter buttons and supports new categories without UI changes.
- Replaced the manual "Refresh Data" and "Evaluate Forecasts" buttons with automatic client-side polling (hourly during US market hours, less often otherwise) plus background evaluation, surfaced as an "updated … ago" freshness indicator.
- Made price-to-sales the primary valuation metric in the Snapshot, matching the forecast model's fundamental inputs.
- Added `.claude/launch.json` so the app can be launched for local preview.

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
