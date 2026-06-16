import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import server


class ForecastMathTests(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertEqual(server.percentile([1, 2, 3], 0.5), 2)
        self.assertAlmostEqual(server.percentile([0, 10], 0.25), 2.5)

    def test_rolling_returns(self):
        prices = [
            {"date": "2026-01-01", "close": 10, "adj_close": 10},
            {"date": "2026-01-02", "close": 11, "adj_close": 11},
            {"date": "2026-01-03", "close": 12.1, "adj_close": 12.1},
        ]
        rows = server.rolling_returns(prices, 1)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["return"], 0.1)

    def test_horizon_forecast_has_bounds(self):
        start = date.today() - timedelta(days=500)
        prices = []
        value = 100.0
        for i in range(360):
            value *= 1.0007
            prices.append({"date": (start + timedelta(days=i)).isoformat(), "close": value, "adj_close": value})
        features = {
            "technical": {
                "return_20d": 0.02,
                "return_65d": 0.05,
                "return_252d": 0.18,
                "annualized_volatility": 0.22,
                "ma_50_position": 0.03,
                "ma_200_position": 0.08,
                "price_history_days": len(prices),
            },
            "fundamental": {
                "price_to_sales": 8,
                "net_margin": 0.22,
                "balance_sheet_strength": 0.45,
            },
            "analyst_targets": {"target_mean": 130},
            "event_risk": {"interval_widening": 0.0},
        }
        forecast = server.forecast_horizon(server.HORIZONS[0], 120, prices, features, [])
        self.assertLess(forecast["lower"], forecast["base"])
        self.assertLess(forecast["base"], forecast["upper"])
        self.assertIn(forecast["confidence_label"], {"low", "medium", "high"})

    def test_fact_history_extracts_annual_revenue(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {"val": 100, "end": "2024-12-31", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-01"},
                                {"val": 25, "end": "2025-03-31", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-05-01"},
                                {"val": 121, "end": "2025-12-31", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01"},
                            ]
                        }
                    }
                }
            }
        }
        rows = server.fact_history(facts, "us-gaap", ["Revenues"], ["USD"], "revenue")
        self.assertEqual([row["revenue"] for row in rows], [100.0, 121.0])

    def test_cagr_from_history(self):
        rows = [
            {"period_end": "2023-12-31", "revenue": 100},
            {"period_end": "2025-12-31", "revenue": 121},
        ]
        self.assertAlmostEqual(server.cagr_from_history(rows, "revenue"), 0.10, places=3)

    def test_manual_assumptions_override_derived_forecasts(self):
        fundamentals = {
            "metrics": {
                "revenue": {"value": 1000},
                "shares_outstanding": {"value": 100},
                "net_income": {"value": 100},
            },
            "revenue_history": [
                {"period_end": "2024-12-31", "revenue": 1000},
                {"period_end": "2025-12-31", "revenue": 1100},
            ],
            "share_history": [
                {"period_end": "2024-12-31", "shares": 100},
                {"period_end": "2025-12-31", "shares": 105},
            ],
        }
        company = {"manual_assumptions": {"revenue_forecast_cagr": 0.08, "share_change_cagr": -0.01}}
        features = server.valuation_features(10, fundamentals, {"market_cap": 1000}, company)
        self.assertEqual(features["revenue_forecast_cagr"], 0.08)
        self.assertEqual(features["revenue_forecast_cagr_source"], "manual_assumptions")
        self.assertEqual(features["share_change_cagr"], -0.01)
        self.assertEqual(features["share_change_cagr_source"], "manual_assumptions")

    def test_market_cap_is_calculated_from_price_and_shares(self):
        fundamentals = {
            "metrics": {
                "revenue": {"value": 1_000_000},
                "net_income": {"value": 100_000},
                "shares_outstanding": {"value": 1_000_000},
            }
        }
        features = server.valuation_features(10, fundamentals, {"market_cap": 5000}, {})
        self.assertEqual(features["computed_market_cap"], 10_000_000)
        self.assertEqual(features["provider_market_cap"], 5000)
        self.assertEqual(features["market_cap"], 10_000_000)
        self.assertEqual(features["price_to_sales"], 10)
        self.assertIn(
            "provider_market_cap_mismatch",
            [flag["code"] for flag in features["sanity"]["flags"]],
        )

    def test_tsm_like_market_cap_fails_sanity_and_suppresses_ratios(self):
        fundamentals = {
            "metrics": {
                "revenue": {"value": 100_000_000_000},
                "net_income": {"value": 40_000_000_000},
                "shares_outstanding": {"value": 25_932_524_521},
            }
        }
        features = server.valuation_features(427.9649963378906, fundamentals, {}, {})
        self.assertGreater(features["computed_market_cap"], server.MAX_EXPECTED_MARKET_CAP_USD)
        self.assertIsNone(features["market_cap"])
        self.assertIsNone(features["price_to_sales"])
        self.assertIsNone(features["price_to_earnings"])
        self.assertEqual(features["sanity"]["status"], "fail")
        self.assertIn(
            "market_cap_above_world_max",
            [flag["code"] for flag in features["sanity"]["flags"]],
        )

    def test_invalid_price_and_shares_create_specific_sanity_flags(self):
        sanity = server.valuation_sanity(-1, 10, -10, None)
        codes = [flag["code"] for flag in sanity["flags"]]
        self.assertEqual(sanity["status"], "fail")
        self.assertIn("current_price_out_of_range", codes)
        self.assertIn("shares_outstanding_out_of_range", codes)

    def test_provider_market_cap_mismatch_does_not_replace_computed_market_cap(self):
        fundamentals = {
            "metrics": {
                "revenue": {"value": 1_000_000},
                "net_income": {"value": 100_000},
                "shares_outstanding": {"value": 1_000_000},
            }
        }
        features = server.valuation_features(10, fundamentals, {"market_cap": 15_000_000}, {})
        self.assertEqual(features["market_cap"], 10_000_000)
        self.assertEqual(features["sanity"]["status"], "warn")
        self.assertIn(
            "provider_market_cap_mismatch",
            [flag["code"] for flag in features["sanity"]["flags"]],
        )

    def test_sanity_observations_are_deduped_by_refresh_and_policy(self):
        original_db_path = server.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                server.DB_PATH = Path(tmp) / "equities.sqlite"
                server.init_db()
                fundamentals = {
                    "metrics": {
                        "revenue": {"value": 1_000_000},
                        "net_income": {"value": 100_000},
                        "shares_outstanding": {"value": 1_000_000},
                    }
                }
                features = server.valuation_features(10, fundamentals, {}, {})
                source_timestamps = {"composite_metrics": "refresh-1", "latest_price_date": "2026-06-16"}
                server.record_sanity_observation("TEST", "refresh-1", "2026-06-16", features, source_timestamps)
                server.record_sanity_observation("TEST", "refresh-1", "2026-06-16", features, source_timestamps)
                self.assertEqual(len(server.recent_sanity_observations("TEST")), 1)
                server.record_sanity_observation("TEST", "refresh-2", "2026-06-17", features, source_timestamps)
                self.assertEqual(len(server.recent_sanity_observations("TEST")), 2)
        finally:
            server.DB_PATH = original_db_path

    def test_share_buyback_increases_fundamental_cagr(self):
        base = {
            "technical": {},
            "fundamental": {
                "revenue_forecast_cagr": 0.08,
                "share_change_cagr": 0.02,
                "net_margin": 0.10,
                "balance_sheet_strength": 0.20,
                "price_to_sales": 6,
            },
        }
        buyback = {
            "technical": {},
            "fundamental": {
                **base["fundamental"],
                "share_change_cagr": -0.02,
            },
        }
        self.assertGreater(
            server.estimate_fundamental_cagr(buyback),
            server.estimate_fundamental_cagr(base),
        )

    def test_horizon_exposes_calculation_trace(self):
        start = date.today() - timedelta(days=500)
        prices = []
        value = 100.0
        for i in range(360):
            value *= 1.0005
            prices.append({"date": (start + timedelta(days=i)).isoformat(), "close": value, "adj_close": value})
        features = {
            "technical": {
                "return_20d": 0.02,
                "return_65d": 0.04,
                "return_252d": 0.12,
                "annualized_volatility": 0.20,
                "ma_50_position": 0.03,
                "ma_200_position": 0.06,
                "price_history_days": len(prices),
            },
            "fundamental": {
                "latest_revenue": 1000,
                "shares_outstanding": 100,
                "revenue_forecast_cagr": 0.08,
                "share_change_cagr": -0.01,
                "price_to_sales": 8,
                "net_margin": 0.20,
                "balance_sheet_strength": 0.40,
            },
            "analyst_targets": {"target_mean": 130},
            "event_risk": {"interval_widening": 0.0},
        }
        forecast = server.forecast_horizon(server.HORIZONS[1], 100, prices, features, [])
        calc = forecast["calculation"]
        self.assertIn("technical_return", calc)
        self.assertIn("fundamental_components", calc)
        self.assertIn("revenue_forecast", calc)
        self.assertIn("forecast_shares", calc)
        self.assertAlmostEqual(calc["forecast_shares"], 99.0)


class ImmutableForecastTests(unittest.TestCase):
    def test_write_json_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forecast.json"
            server.write_json(path, {"a": 1})
            with self.assertRaises(FileExistsError):
                server.write_json(path, {"a": 2})

    def test_decision_references_forecast_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_root = server.ROOT
            original_forecasts = server.FORECASTS_DIR
            try:
                server.ROOT = Path(tmp)
                server.FORECASTS_DIR = Path(tmp) / "forecasts"
                server.FORECASTS_DIR.mkdir()
                server.write_json(
                    server.FORECASTS_DIR / "2026-06-16_TEST_abc123.json",
                    {
                        "forecast_id": "abc123",
                        "ticker": "TEST",
                        "created_at": "2026-06-16T00:00:00+00:00",
                    },
                )
                record = server.save_decision("TEST", {"action": "watch", "thesis": "check"})
                self.assertEqual(record["forecast_id"], "abc123")
                self.assertTrue((Path(tmp) / "decisions").exists())
            finally:
                server.ROOT = original_root
                server.FORECASTS_DIR = original_forecasts


if __name__ == "__main__":
    unittest.main()
