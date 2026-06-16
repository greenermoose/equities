# Equities Decision Support

A small local app for researching public companies, recording transparent price forecasts, and comparing those forecasts with later price history.

This is decision-support software for personal research. It does not place trades, connect to a brokerage, or provide financial advice.

## Run

```powershell
python server.py
```

Then open:

```text
http://127.0.0.1:8765
```

The app uses only Python's standard library. Live data refreshes use public SEC EDGAR JSON APIs and Yahoo Finance chart endpoints where available. All live provider responses are cached in `data/equities.sqlite`, which is ignored by git because it is bulky and reproducible.

Browser-served files live in `http/`. Server code, model logic, repo-persisted knowledge, and local cache files live outside that folder.

## Repo-Persisted Knowledge

- `companies/{TICKER}.json`: durable company notes, assumptions, and metadata.
- `forecasts/{YYYY-MM-DD}_{TICKER}_{forecast_id}.json`: immutable forecast records that can be evaluated later.
- `decisions/`: reserved for immutable trade-review snapshots that reference forecast IDs.

## Seed Tickers

`ADBE`, `AVGO`, `BETA`, `CRSP`, and `TSM`.

## Tests

```powershell
python -m unittest discover -s tests
```
