import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Minimal formatting dependency needed to load market_engine.py in isolation.
formatting = type(sys)("formatting")
formatting.num = lambda value: float(value or 0)
sys.modules["formatting"] = formatting

spec = importlib.util.spec_from_file_location("market_engine", ROOT / "market_engine.py")
module = importlib.util.module_from_spec(spec)
sys.modules["market_engine"] = module
spec.loader.exec_module(module)
MarketEngine = module.MarketEngine


class MarketBreadthTests(unittest.TestCase):
    def test_first_run_uses_coingecko_1h_change(self):
        engine = MarketEngine("https://example.invalid")
        coins = [
            {"id": "a", "price_change_percentage_1h_in_currency": 1.2},
            {"id": "b", "price_change_percentage_1h_in_currency": -0.8},
            {"id": "c", "price_change_percentage_1h_in_currency": 0.05},
            {"id": "d", "price_change_percentage_1h_in_currency": 2.1},
        ]

        breadth = engine.breadth(coins, now=1000.0)

        self.assertEqual(breadth["positive"], 2)
        self.assertEqual(breadth["negative"], 1)
        self.assertEqual(breadth["neutral"], 1)
        self.assertAlmostEqual(breadth["positive_pct"], 50.0)
        self.assertAlmostEqual(breadth["negative_pct"], 25.0)
        self.assertAlmostEqual(breadth["average_change"], 0.6375)

    def test_fallback_supports_alternate_1h_field(self):
        engine = MarketEngine("https://example.invalid")
        coin = {"id": "a", "price_change_percentage_1h": 1.7}
        self.assertEqual(engine.metrics(coin, 1000.0)["1h"], 1.7)

    def test_local_history_still_wins_after_full_hour(self):
        engine = MarketEngine("https://example.invalid")
        coin = {"id": "a", "current_price": 100.0, "price_change_percentage_1h_in_currency": 9.9}
        engine.update_history([coin], 0.0)
        coin2 = {"id": "a", "current_price": 102.0, "price_change_percentage_1h_in_currency": 9.9}
        engine.update_history([coin2], 3600.0)
        self.assertAlmostEqual(engine.metrics(coin2, 3600.0)["1h"], 2.0)


if __name__ == "__main__":
    unittest.main()
