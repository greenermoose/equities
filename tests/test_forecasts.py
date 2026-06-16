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
