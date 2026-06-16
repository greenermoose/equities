from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import statistics
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "equities.sqlite"
COMPANIES_DIR = ROOT / "companies"
FORECASTS_DIR = ROOT / "forecasts"
HTTP_DIR = ROOT / "http"
SEED_TICKERS = ["ADBE", "AVGO", "BETA", "CRSP", "TSM"]
MODEL_VERSION = "transparent-ensemble-v1"
USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "equities-decision-support/1.0 contact@example.com",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    COMPANIES_DIR.mkdir(exist_ok=True)
    FORECASTS_DIR.mkdir(exist_ok=True)
    HTTP_DIR.mkdir(exist_ok=True)


def connect() -> sqlite3.Connection:
    ensure_dirs()
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            create table if not exists provider_cache (
                cache_key text primary key,
                fetched_at text not null,
                payload text not null
            );
            create table if not exists prices (
                ticker text not null,
                date text not null,
                open real,
                high real,
                low real,
                close real,
                adj_close real,
                volume integer,
                primary key (ticker, date)
            );
            create table if not exists splits (
                ticker text not null,
                date text not null,
                ratio real not null,
                primary key (ticker, date)
            );
            create table if not exists metrics (
                ticker text primary key,
                fetched_at text not null,
                source text not null,
                payload text not null
            );
            create table if not exists forecast_outcomes (
                forecast_id text not null,
                ticker text not null,
                horizon text not null,
                model_version text not null,
                forecast_created_at text not null,
                target_date text not null,
                evaluated_at text not null,
                actual_price real,
                inside_interval integer,
                distance_from_base_pct real,
                max_drawdown_pct real,
                max_upside_pct real,
                primary key (forecast_id, horizon)
            );
            """
        )


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite immutable file: {path}")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def company_path(ticker: str) -> Path:
    return COMPANIES_DIR / f"{ticker.upper()}.json"


def load_companies() -> list[dict[str, Any]]:
    companies = []
    for ticker in SEED_TICKERS:
        companies.append(read_json(company_path(ticker), {"ticker": ticker, "name": ticker}))
    for path in sorted(COMPANIES_DIR.glob("*.json")):
        ticker = path.stem.upper()
        if ticker not in SEED_TICKERS:
            companies.append(read_json(path, {"ticker": ticker, "name": ticker}))
    return companies


def ticker_list() -> list[str]:
    return [c["ticker"].upper() for c in load_companies()]


def fetch_json(url: str, cache_key: str, ttl_seconds: int = 3600) -> tuple[Any, str, str]:
    with connect() as db:
        cached = db.execute(
            "select fetched_at, payload from provider_cache where cache_key = ?",
            (cache_key,),
        ).fetchone()
        if cached:
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if (utc_now() - fetched).total_seconds() < ttl_seconds:
                return json.loads(cached["payload"]), cached["fetched_at"], "cache"

    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=25) as response:
            payload = response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError) as exc:
        if cached:
            return json.loads(cached["payload"]), cached["fetched_at"], f"stale_cache:{type(exc).__name__}"
        raise RuntimeError(f"Unable to fetch {url}: {exc}") from exc

    fetched_at = iso_now()
    with connect() as db:
        db.execute(
            "insert or replace into provider_cache(cache_key, fetched_at, payload) values (?, ?, ?)",
            (cache_key, fetched_at, payload),
        )
    return json.loads(payload), fetched_at, "live"


def yahoo_chart(ticker: str, range_: str = "10y") -> dict[str, Any]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range={range_}&interval=1d&events=div%2Csplits"
    )
    payload, fetched_at, source_state = fetch_json(url, f"yahoo-chart:{ticker}:{range_}", 12 * 3600)
    result = payload.get("chart", {}).get("result", [{}])[0]
    result["_fetched_at"] = fetched_at
    result["_source_state"] = source_state
    return result


def yahoo_quote_summary(ticker: str) -> dict[str, Any]:
    modules = "price,summaryDetail,defaultKeyStatistics,financialData,calendarEvents"
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules={modules}"
    try:
        payload, fetched_at, source_state = fetch_json(url, f"yahoo-summary:{ticker}", 12 * 3600)
        result = payload.get("quoteSummary", {}).get("result", [{}])[0] or {}
        result["_fetched_at"] = fetched_at
        result["_source_state"] = source_state
        return result
    except RuntimeError:
        return {"_fetched_at": None, "_source_state": "unavailable"}


def store_chart(ticker: str, chart: dict[str, Any]) -> int:
    timestamps = chart.get("timestamp") or []
    quote = (chart.get("indicators", {}).get("quote") or [{}])[0]
    adj = (chart.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    count = 0
    with connect() as db:
        for i, ts in enumerate(timestamps):
            day = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
            close = value_at(quote.get("close"), i)
            if close is None:
                continue
            db.execute(
                """
                insert or replace into prices(ticker, date, open, high, low, close, adj_close, volume)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker.upper(),
                    day,
                    value_at(quote.get("open"), i),
                    value_at(quote.get("high"), i),
                    value_at(quote.get("low"), i),
                    close,
                    value_at(adj, i),
                    value_at(quote.get("volume"), i),
                ),
            )
            count += 1
        splits = chart.get("events", {}).get("splits", {}) or {}
        for event in splits.values():
            split_date = datetime.fromtimestamp(event["date"], timezone.utc).date().isoformat()
            numerator = float(event.get("numerator") or 0)
            denominator = float(event.get("denominator") or 1)
            if numerator:
                db.execute(
                    "insert or replace into splits(ticker, date, ratio) values (?, ?, ?)",
                    (ticker.upper(), split_date, numerator / denominator),
                )
    return count


def value_at(values: Any, i: int) -> Any:
    if not isinstance(values, list) or i >= len(values):
        return None
    return values[i]


def get_prices(ticker: str) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "select date, open, high, low, close, adj_close, volume from prices where ticker = ? order by date",
            (ticker.upper(),),
        ).fetchall()
    return [dict(row) for row in rows]


def current_price(prices: list[dict[str, Any]]) -> float | None:
    return prices[-1]["close"] if prices else None


def sec_ticker_map() -> dict[str, dict[str, Any]]:
    payload, _, _ = fetch_json(
        "https://www.sec.gov/files/company_tickers_exchange.json",
        "sec:company_tickers_exchange",
        24 * 3600,
    )
    fields = payload.get("fields", [])
    out = {}
    for row in payload.get("data", []):
        item = dict(zip(fields, row))
        ticker = str(item.get("ticker", "")).upper()
        if ticker:
            out[ticker] = item
    return out


def sec_companyfacts(cik: int) -> tuple[dict[str, Any], str, str]:
    return fetch_json(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
        f"sec:companyfacts:{cik:010d}",
        24 * 3600,
    )


def latest_fact(facts: dict[str, Any], taxonomy: str, tags: list[str], units: list[str]) -> dict[str, Any] | None:
    taxonomy_facts = facts.get("facts", {}).get(taxonomy, {})
    candidates = []
    for tag in tags:
        units_map = taxonomy_facts.get(tag, {}).get("units", {})
        for unit in units:
            for item in units_map.get(unit, []):
                val = item.get("val")
                end = item.get("end")
                if val is not None and end:
                    candidates.append({"tag": tag, **item})
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item.get("end", ""), item.get("filed", "")))[-1]


def collect_fundamentals(ticker: str) -> dict[str, Any]:
    warnings = []
    company = read_json(company_path(ticker), {})
    result: dict[str, Any] = {
        "source": "SEC EDGAR companyfacts",
        "fetched_at": None,
        "cik": None,
        "company_name": company.get("name", ticker),
        "metrics": {},
        "warnings": warnings,
    }
    try:
        ticker_map = sec_ticker_map()
        item = ticker_map.get(ticker.upper())
        if not item:
            warnings.append("SEC CIK mapping unavailable for ticker.")
            return result
        cik = int(item["cik"])
        facts, fetched_at, state = sec_companyfacts(cik)
        result["fetched_at"] = fetched_at
        result["source_state"] = state
        result["cik"] = cik
        result["company_name"] = facts.get("entityName") or item.get("name") or result["company_name"]
        metric_defs = {
            "revenue": ("us-gaap", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"], ["USD"]),
            "gross_profit": ("us-gaap", ["GrossProfit"], ["USD"]),
            "operating_income": ("us-gaap", ["OperatingIncomeLoss"], ["USD"]),
            "net_income": ("us-gaap", ["NetIncomeLoss", "ProfitLoss"], ["USD"]),
            "eps_diluted": ("us-gaap", ["EarningsPerShareDiluted"], ["USD/shares", "USD/shares"]),
            "assets": ("us-gaap", ["Assets"], ["USD"]),
            "liabilities": ("us-gaap", ["Liabilities"], ["USD"]),
            "equity": ("us-gaap", ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], ["USD"]),
            "shares_outstanding": ("dei", ["EntityCommonStockSharesOutstanding"], ["shares"]),
            "weighted_diluted_shares": ("us-gaap", ["WeightedAverageNumberOfDilutedSharesOutstanding"], ["shares"]),
            "free_cash_flow_proxy": ("us-gaap", ["NetCashProvidedByUsedInOperatingActivities"], ["USD"]),
        }
        for name, (taxonomy, tags, units) in metric_defs.items():
            fact = latest_fact(facts, taxonomy, tags, units)
            if fact:
                result["metrics"][name] = {
                    "value": fact.get("val"),
                    "period_end": fact.get("end"),
                    "filed": fact.get("filed"),
                    "form": fact.get("form"),
                    "tag": fact.get("tag"),
                }
        if "revenue" in result["metrics"] and "gross_profit" in result["metrics"]:
            revenue = safe_float(result["metrics"]["revenue"]["value"])
            gross = safe_float(result["metrics"]["gross_profit"]["value"])
            if revenue:
                result["metrics"]["gross_margin"] = {"value": gross / revenue, "derived": True}
        if "revenue" in result["metrics"] and "net_income" in result["metrics"]:
            revenue = safe_float(result["metrics"]["revenue"]["value"])
            income = safe_float(result["metrics"]["net_income"]["value"])
            if revenue:
                result["metrics"]["net_margin"] = {"value": income / revenue, "derived": True}
        if "shares_outstanding" not in result["metrics"]:
            warnings.append("Current reported shares outstanding unavailable from SEC facts.")
    except Exception as exc:
        warnings.append(f"SEC fundamentals unavailable: {type(exc).__name__}")
    return result


def safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def flatten_raw_number(item: Any) -> float | None:
    if isinstance(item, dict):
        return safe_float(item.get("raw"))
    return safe_float(item)


def collect_market_metrics(ticker: str, prices: list[dict[str, Any]]) -> dict[str, Any]:
    summary = yahoo_quote_summary(ticker)
    financial = summary.get("financialData", {}) or {}
    price = summary.get("price", {}) or {}
    calendar = summary.get("calendarEvents", {}) or {}
    analyst = {
        "target_low": flatten_raw_number(financial.get("targetLowPrice")),
        "target_mean": flatten_raw_number(financial.get("targetMeanPrice")),
        "target_high": flatten_raw_number(financial.get("targetHighPrice")),
        "recommendation": financial.get("recommendationKey"),
        "number_of_analysts": flatten_raw_number(financial.get("numberOfAnalystOpinions")),
    }
    earnings_dates = calendar.get("earnings", {}).get("earningsDate") or []
    next_earnings = None
    if earnings_dates and isinstance(earnings_dates[0], dict):
        raw = earnings_dates[0].get("raw")
        if raw:
            next_earnings = datetime.fromtimestamp(raw, timezone.utc).date().isoformat()
    return {
        "source": "Yahoo Finance public endpoints",
        "fetched_at": summary.get("_fetched_at"),
        "source_state": summary.get("_source_state"),
        "current_price": flatten_raw_number(price.get("regularMarketPrice")) or current_price(prices),
        "market_cap": flatten_raw_number(price.get("marketCap")),
        "analyst_targets": analyst,
        "next_earnings_date": next_earnings,
    }


def refresh_ticker(ticker: str) -> dict[str, Any]:
    ticker = ticker.upper()
    chart = yahoo_chart(ticker)
    rows = store_chart(ticker, chart)
    prices = get_prices(ticker)
    fundamentals = collect_fundamentals(ticker)
    market = collect_market_metrics(ticker, prices)
    payload = {
        "ticker": ticker,
        "refreshed_at": iso_now(),
        "price_rows": rows,
        "fundamentals": fundamentals,
        "market": market,
    }
    with connect() as db:
        db.execute(
            "insert or replace into metrics(ticker, fetched_at, source, payload) values (?, ?, ?, ?)",
            (ticker, payload["refreshed_at"], "composite", json.dumps(payload)),
        )
    return payload


def latest_metrics(ticker: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("select payload from metrics where ticker = ?", (ticker.upper(),)).fetchone()
    return json.loads(row["payload"]) if row else None


def pct_change(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start == 0:
        return None
    return end / start - 1


def daily_returns(prices: list[dict[str, Any]]) -> list[float]:
    returns = []
    previous = None
    for row in prices:
        close = safe_float(row.get("adj_close") or row.get("close"))
        if previous and close:
            returns.append(close / previous - 1)
        previous = close
    return returns


def rolling_returns(prices: list[dict[str, Any]], window: int) -> list[dict[str, Any]]:
    out = []
    for i in range(window, len(prices)):
        start = safe_float(prices[i - window].get("adj_close") or prices[i - window].get("close"))
        end = safe_float(prices[i].get("adj_close") or prices[i].get("close"))
        change = pct_change(start, end)
        if change is not None:
            out.append({"start": prices[i - window]["date"], "end": prices[i]["date"], "return": change})
    return out


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[int(index)]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def annualized_volatility(returns: list[float]) -> float | None:
    if len(returns) < 20:
        return None
    return statistics.stdev(returns[-252:]) * math.sqrt(252)


def moving_average_position(prices: list[dict[str, Any]], window: int) -> float | None:
    if len(prices) < window:
        return None
    closes = [safe_float(row.get("adj_close") or row.get("close")) for row in prices[-window:]]
    closes = [x for x in closes if x is not None]
    if not closes:
        return None
    current = closes[-1]
    avg = statistics.mean(closes)
    return current / avg - 1 if avg else None


def recent_return(prices: list[dict[str, Any]], days: int) -> float | None:
    if len(prices) <= days:
        return None
    start = safe_float(prices[-days - 1].get("adj_close") or prices[-days - 1].get("close"))
    end = safe_float(prices[-1].get("adj_close") or prices[-1].get("close"))
    return pct_change(start, end)


def estimate_event_risk(market: dict[str, Any]) -> float:
    earnings = market.get("next_earnings_date")
    if not earnings:
        return 0.0
    try:
        days = (date.fromisoformat(earnings) - date.today()).days
    except ValueError:
        return 0.0
    if 0 <= days <= 21:
        return 0.18
    if 0 <= days <= 65:
        return 0.08
    return 0.0


def valuation_features(current: float | None, fundamentals: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    metrics = fundamentals.get("metrics", {})
    revenue = safe_float((metrics.get("revenue") or {}).get("value"))
    net_income = safe_float((metrics.get("net_income") or {}).get("value"))
    shares = safe_float((metrics.get("shares_outstanding") or metrics.get("weighted_diluted_shares") or {}).get("value"))
    market_cap = safe_float(market.get("market_cap"))
    if not market_cap and current and shares:
        market_cap = current * shares
    return {
        "market_cap": market_cap,
        "shares_outstanding": shares,
        "price_to_sales": market_cap / revenue if market_cap and revenue and revenue > 0 else None,
        "price_to_earnings": market_cap / net_income if market_cap and net_income and net_income > 0 else None,
        "gross_margin": safe_float((metrics.get("gross_margin") or {}).get("value")),
        "net_margin": safe_float((metrics.get("net_margin") or {}).get("value")),
        "balance_sheet_strength": balance_sheet_strength(metrics),
    }


def balance_sheet_strength(metrics: dict[str, Any]) -> float | None:
    assets = safe_float((metrics.get("assets") or {}).get("value"))
    liabilities = safe_float((metrics.get("liabilities") or {}).get("value"))
    if not assets or liabilities is None:
        return None
    return max(-1.0, min(1.0, (assets - liabilities) / assets))


@dataclass(frozen=True)
class HorizonSpec:
    name: str
    trading_days: int
    years: float
    technical_weight: float


HORIZONS = [
    HorizonSpec("13w", 65, 0.25, 0.85),
    HorizonSpec("1y", 252, 1.0, 0.45),
    HorizonSpec("3y", 756, 3.0, 0.20),
    HorizonSpec("5y", 1260, 5.0, 0.15),
    HorizonSpec("10y", 2520, 10.0, 0.10),
]


def generate_forecast(ticker: str, persist: bool = True) -> dict[str, Any]:
    ticker = ticker.upper()
    prices = get_prices(ticker)
    if not prices:
        refresh_ticker(ticker)
        prices = get_prices(ticker)
    metrics = latest_metrics(ticker) or refresh_ticker(ticker)
    fundamentals = metrics.get("fundamentals", {})
    market = metrics.get("market", {})
    current = safe_float(market.get("current_price")) or current_price(prices)
    if not current:
        raise ValueError(f"No current price available for {ticker}")

    returns = daily_returns(prices)
    vol = annualized_volatility(returns)
    event_risk = estimate_event_risk(market)
    valuation = valuation_features(current, fundamentals, market)
    analyst = market.get("analyst_targets", {})
    features = {
        "technical": {
            "return_20d": recent_return(prices, 20),
            "return_65d": recent_return(prices, 65),
            "return_252d": recent_return(prices, 252),
            "annualized_volatility": vol,
            "ma_50_position": moving_average_position(prices, 50),
            "ma_200_position": moving_average_position(prices, 200),
            "price_history_days": len(prices),
        },
        "fundamental": valuation,
        "analyst_targets": analyst,
        "event_risk": {
            "next_earnings_date": market.get("next_earnings_date"),
            "interval_widening": event_risk,
        },
    }
    warnings = []
    warnings.extend(fundamentals.get("warnings", []))
    if len(prices) < 252:
        warnings.append("Insufficient one-year price history; confidence reduced.")
    if analyst.get("target_mean") is None:
        warnings.append("Analyst target data unavailable.")
    if valuation["price_to_sales"] is None:
        warnings.append("Price-to-sales unavailable due to missing revenue or market cap.")

    horizons = {}
    for spec in HORIZONS:
        horizons[spec.name] = forecast_horizon(spec, current, prices, features, warnings)

    forecast_id = uuid.uuid4().hex[:12]
    payload = {
        "forecast_id": forecast_id,
        "ticker": ticker,
        "created_at": iso_now(),
        "current_date": today_iso(),
        "model_version": MODEL_VERSION,
        "source_timestamps": {
            "composite_metrics": metrics.get("refreshed_at"),
            "fundamentals": fundamentals.get("fetched_at"),
            "market": market.get("fetched_at"),
            "latest_price_date": prices[-1]["date"] if prices else None,
        },
        "current_price": current,
        "horizons": horizons,
        "features": features,
        "assumptions": [
            "Intervals are scenario ranges for personal decision support, not trading instructions.",
            "Short horizons emphasize empirical price action; long horizons emphasize fundamentals and valuation.",
            "Missing inputs reduce confidence rather than being silently filled as zero.",
        ],
        "warnings": warnings,
    }
    if persist:
        write_json(FORECASTS_DIR / f"{today_iso()}_{ticker}_{forecast_id}.json", payload)
    return payload


def forecast_horizon(
    spec: HorizonSpec,
    current: float,
    prices: list[dict[str, Any]],
    features: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    rolling = rolling_returns(prices, min(spec.trading_days, max(20, len(prices) // 4)))
    realized = [row["return"] for row in rolling]
    p05 = percentile(realized, 0.05)
    p50 = percentile(realized, 0.50)
    p95 = percentile(realized, 0.95)

    tech = features["technical"]
    momentum = average_present([tech.get("return_20d"), tech.get("return_65d"), tech.get("return_252d")])
    ma_bias = average_present([tech.get("ma_50_position"), tech.get("ma_200_position")])
    technical_base = average_present([p50, momentum * min(1.0, spec.years) if momentum is not None else None, ma_bias])
    if technical_base is None:
        technical_base = 0.0

    fundamental_cagr = estimate_fundamental_cagr(features)
    fundamental_return = (1 + fundamental_cagr) ** spec.years - 1
    analyst_return = analyst_expected_return(current, features.get("analyst_targets", {}), spec)
    blend_items = [
        (technical_base, spec.technical_weight),
        (fundamental_return, 1 - spec.technical_weight),
    ]
    if analyst_return is not None and spec.name in {"13w", "1y"}:
        blend_items.append((analyst_return, 0.20 if spec.name == "1y" else 0.08))
    base_return = weighted_average(blend_items)

    volatility = features["technical"].get("annualized_volatility")
    vol_band = (volatility or 0.45) * math.sqrt(spec.years) * 1.65
    empirical_low = p05 if p05 is not None else base_return - vol_band
    empirical_high = p95 if p95 is not None else base_return + vol_band
    lower_return = min(empirical_low, base_return - vol_band * (1 + features["event_risk"]["interval_widening"]))
    upper_return = max(empirical_high, base_return + vol_band * (1 + features["event_risk"]["interval_widening"]))
    if spec.name in {"3y", "5y", "10y"}:
        long_uncertainty = 0.20 * spec.years
        lower_return = min(lower_return, fundamental_return - long_uncertainty)
        upper_return = max(upper_return, fundamental_return + long_uncertainty)

    confidence = confidence_score(spec, len(prices), features, warnings)
    target_date = add_trading_day_approx(date.today(), spec.trading_days).isoformat()
    return {
        "target_date": target_date,
        "lower": round(max(0.01, current * (1 + lower_return)), 2),
        "base": round(max(0.01, current * (1 + base_return)), 2),
        "upper": round(max(0.01, current * (1 + upper_return)), 2),
        "confidence": confidence,
        "confidence_label": confidence_label(confidence),
        "drivers": {
            "technical_weight": spec.technical_weight,
            "fundamental_weight": round(1 - spec.technical_weight, 2),
            "technical_base_return": round(technical_base, 4),
            "fundamental_return": round(fundamental_return, 4),
            "analyst_return": round(analyst_return, 4) if analyst_return is not None else None,
            "event_risk_widening": features["event_risk"]["interval_widening"],
        },
    }


def average_present(values: list[float | None]) -> float | None:
    present = [x for x in values if x is not None and math.isfinite(x)]
    return statistics.mean(present) if present else None


def weighted_average(items: list[tuple[float | None, float]]) -> float:
    present = [(value, weight) for value, weight in items if value is not None and math.isfinite(value)]
    total = sum(weight for _, weight in present)
    if not total:
        return 0.0
    return sum(value * weight for value, weight in present) / total


def estimate_fundamental_cagr(features: dict[str, Any]) -> float:
    fundamental = features["fundamental"]
    margin = fundamental.get("net_margin")
    balance = fundamental.get("balance_sheet_strength")
    ps = fundamental.get("price_to_sales")
    cagr = 0.06
    if margin is not None:
        cagr += max(-0.04, min(0.08, margin * 0.25))
    if balance is not None:
        cagr += max(-0.03, min(0.03, balance * 0.05))
    if ps is not None:
        if ps > 20:
            cagr -= 0.04
        elif ps < 5:
            cagr += 0.02
    return max(-0.20, min(0.25, cagr))


def analyst_expected_return(current: float, analyst: dict[str, Any], spec: HorizonSpec) -> float | None:
    target = analyst.get("target_mean")
    if not target or not current:
        return None
    one_year = target / current - 1
    if spec.name == "13w":
        return one_year * 0.25
    if spec.name == "1y":
        return one_year
    return None


def confidence_score(spec: HorizonSpec, history_days: int, features: dict[str, Any], warnings: list[str]) -> int:
    score = 72
    if history_days < spec.trading_days * 2:
        score -= 22
    if history_days < 252:
        score -= 18
    if spec.name in {"5y", "10y"}:
        score -= 12
    if features["technical"].get("annualized_volatility") and features["technical"]["annualized_volatility"] > 0.65:
        score -= 10
    if features["event_risk"].get("interval_widening", 0) > 0:
        score -= 6
    score -= min(18, len(warnings) * 4)
    return max(10, min(90, score))


def confidence_label(score: int) -> str:
    if score >= 70:
        return "high"
    if score >= 45:
        return "medium"
    return "low"


def add_trading_day_approx(start: date, trading_days: int) -> date:
    days = int(math.ceil(trading_days * 7 / 5))
    return start + timedelta(days=days)


def list_forecast_files() -> list[Path]:
    return sorted(FORECASTS_DIR.glob("*.json"))


def load_forecasts(ticker: str | None = None) -> list[dict[str, Any]]:
    out = []
    for path in list_forecast_files():
        forecast = read_json(path, {})
        if ticker and forecast.get("ticker") != ticker.upper():
            continue
        forecast["_file"] = path.name
        out.append(forecast)
    return sorted(out, key=lambda item: item.get("created_at", ""), reverse=True)


def save_decision(ticker: str, payload: dict[str, Any]) -> dict[str, Any]:
    ticker = ticker.upper()
    action = re.sub(r"[^A-Za-z0-9_-]+", "-", str(payload.get("action", "review")).lower()).strip("-") or "review"
    forecast_id = payload.get("forecast_id")
    if not forecast_id:
        forecasts = load_forecasts(ticker)
        forecast_id = forecasts[0]["forecast_id"] if forecasts else None
    record = {
        "decision_id": uuid.uuid4().hex[:12],
        "ticker": ticker,
        "created_at": iso_now(),
        "current_date": today_iso(),
        "action": action,
        "forecast_id": forecast_id,
        "conviction": payload.get("conviction"),
        "time_horizon": payload.get("time_horizon"),
        "thesis": payload.get("thesis", ""),
        "risks": payload.get("risks", []),
        "invalidation_condition": payload.get("invalidation_condition", ""),
        "notes": payload.get("notes", ""),
    }
    path = ROOT / "decisions" / f"{today_iso()}_{ticker}_{action}_{record['decision_id']}.json"
    write_json(path, record)
    return record


def evaluate_forecasts() -> dict[str, Any]:
    evaluated = []
    skipped = []
    with connect() as db:
        for forecast in load_forecasts():
            ticker = forecast["ticker"]
            prices = get_prices(ticker)
            if not prices:
                skipped.append({"forecast_id": forecast["forecast_id"], "reason": "no price history"})
                continue
            price_by_date = {row["date"]: row for row in prices}
            for horizon, estimate in forecast.get("horizons", {}).items():
                target_date = estimate.get("target_date")
                target_row = price_on_or_after(prices, target_date)
                if not target_row:
                    skipped.append({"forecast_id": forecast["forecast_id"], "horizon": horizon, "reason": "horizon not mature"})
                    continue
                window = price_window(prices, forecast["current_date"], target_row["date"])
                actual = safe_float(target_row.get("close"))
                base = safe_float(estimate.get("base"))
                lower = safe_float(estimate.get("lower"))
                upper = safe_float(estimate.get("upper"))
                start = safe_float(price_by_date.get(forecast["source_timestamps"].get("latest_price_date", ""), {}).get("close"))
                if start is None:
                    start = safe_float(forecast.get("current_price"))
                max_upside, max_drawdown = window_extremes(window, start)
                inside = actual is not None and lower is not None and upper is not None and lower <= actual <= upper
                distance = (actual / base - 1) if actual and base else None
                db.execute(
                    """
                    insert or replace into forecast_outcomes(
                        forecast_id, ticker, horizon, model_version, forecast_created_at, target_date,
                        evaluated_at, actual_price, inside_interval, distance_from_base_pct,
                        max_drawdown_pct, max_upside_pct
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        forecast["forecast_id"],
                        ticker,
                        horizon,
                        forecast["model_version"],
                        forecast["created_at"],
                        target_row["date"],
                        iso_now(),
                        actual,
                        1 if inside else 0,
                        distance,
                        max_drawdown,
                        max_upside,
                    ),
                )
                evaluated.append({"forecast_id": forecast["forecast_id"], "ticker": ticker, "horizon": horizon, "inside_interval": inside})
    return {"evaluated": evaluated, "skipped": skipped}


def price_on_or_after(prices: list[dict[str, Any]], target_date: str) -> dict[str, Any] | None:
    for row in prices:
        if row["date"] >= target_date:
            return row
    return None


def price_window(prices: list[dict[str, Any]], start_date: str, end_date: str) -> list[dict[str, Any]]:
    return [row for row in prices if start_date <= row["date"] <= end_date]


def window_extremes(window: list[dict[str, Any]], start: float | None) -> tuple[float | None, float | None]:
    if not window or not start:
        return None, None
    closes = [safe_float(row.get("close")) for row in window]
    closes = [x for x in closes if x is not None]
    if not closes:
        return None, None
    return max(closes) / start - 1, min(closes) / start - 1


def accuracy_summary() -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            """
            select ticker, horizon, model_version, count(*) as n, avg(inside_interval) as interval_accuracy,
                   avg(abs(distance_from_base_pct)) as mean_abs_base_error
            from forecast_outcomes
            group by ticker, horizon, model_version
            order by ticker, horizon
            """
        ).fetchall()
        details = db.execute("select * from forecast_outcomes order by evaluated_at desc").fetchall()
    return {
        "summary": [dict(row) for row in rows],
        "outcomes": [dict(row) for row in details],
    }


def histogram(ticker: str, window: int = 65, years: int = 2) -> dict[str, Any]:
    prices = get_prices(ticker)
    cutoff = date.today() - timedelta(days=365 * years)
    scoped = [row for row in prices if row["date"] >= cutoff.isoformat()]
    returns = [row["return"] for row in rolling_returns(scoped, window)]
    if not returns:
        return {"ticker": ticker, "window": window, "years": years, "bins": [], "count": 0}
    low = min(returns)
    high = max(returns)
    if low == high:
        bins = [{"low": low, "high": high, "count": len(returns)}]
    else:
        bucket_count = 12
        width = (high - low) / bucket_count
        bins = [{"low": low + i * width, "high": low + (i + 1) * width, "count": 0} for i in range(bucket_count)]
        for value in returns:
            idx = min(bucket_count - 1, int((value - low) / width))
            bins[idx]["count"] += 1
    return {"ticker": ticker, "window": window, "years": years, "bins": bins, "count": len(returns)}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.serve_file(HTTP_DIR / "index.html", "text/html")
            elif parsed.path.startswith("/api/summary"):
                self.json_response(app_summary())
            elif parsed.path.startswith("/api/company/"):
                ticker = unquote(parsed.path.rsplit("/", 1)[-1]).upper()
                self.json_response(company_detail(ticker))
            elif parsed.path.startswith("/api/forecasts"):
                ticker = parse_qs(parsed.query).get("ticker", [None])[0]
                self.json_response({"forecasts": load_forecasts(ticker)})
            elif parsed.path.startswith("/api/accuracy"):
                self.json_response(accuracy_summary())
            elif parsed.path.startswith("/api/histogram/"):
                ticker = unquote(parsed.path.rsplit("/", 1)[-1]).upper()
                query = parse_qs(parsed.query)
                self.json_response(histogram(ticker, int(query.get("window", [65])[0]), int(query.get("years", [2])[0])))
            elif parsed.path.startswith("/http/"):
                self.serve_file(ROOT / parsed.path.lstrip("/"), guess_content_type(parsed.path))
            else:
                self.error_response(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc:
            self.error_response(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/refresh"):
                tickers = parse_qs(parsed.query).get("ticker", ticker_list())
                self.json_response({"refreshed": [refresh_ticker(t.upper()) for t in tickers]})
            elif parsed.path.startswith("/api/forecast/"):
                ticker = unquote(parsed.path.rsplit("/", 1)[-1]).upper()
                self.json_response(generate_forecast(ticker, persist=True))
            elif parsed.path.startswith("/api/evaluate"):
                self.json_response(evaluate_forecasts())
            elif parsed.path.startswith("/api/decision/"):
                ticker = unquote(parsed.path.rsplit("/", 1)[-1]).upper()
                self.json_response(save_decision(ticker, self.read_body_json()))
            else:
                self.error_response(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc:
            self.error_response(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def read_body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.resolve().is_relative_to(ROOT):
            self.error_response(HTTPStatus.NOT_FOUND, "Not found")
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error_response(self, status: HTTPStatus, message: str) -> None:
        self.json_response({"error": message}, status)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def app_summary() -> dict[str, Any]:
    companies = []
    for company in load_companies():
        ticker = company["ticker"].upper()
        prices = get_prices(ticker)
        metrics = latest_metrics(ticker)
        forecasts = load_forecasts(ticker)
        companies.append(
            {
                "ticker": ticker,
                "name": company.get("name", ticker),
                "watch_status": company.get("watch_status", "watch"),
                "latest_price": current_price(prices),
                "latest_price_date": prices[-1]["date"] if prices else None,
                "metrics_refreshed_at": metrics.get("refreshed_at") if metrics else None,
                "forecast_count": len(forecasts),
                "latest_forecast": forecasts[0] if forecasts else None,
            }
        )
    return {"current_date": today_iso(), "model_version": MODEL_VERSION, "companies": companies}


def company_detail(ticker: str) -> dict[str, Any]:
    company = read_json(company_path(ticker), {"ticker": ticker})
    prices = get_prices(ticker)
    metrics = latest_metrics(ticker)
    return {
        "company": company,
        "prices": prices[-400:],
        "metrics": metrics,
        "forecasts": load_forecasts(ticker),
        "histogram": histogram(ticker),
    }


def guess_content_type(path: str) -> str:
    if path.endswith(".css"):
        return "text/css"
    if path.endswith(".js"):
        return "text/javascript"
    return "text/plain"


def run() -> None:
    ensure_dirs()
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Equities app running at http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    run()
