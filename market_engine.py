from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from formatting import num


MONITORED_COINS = [
    "bitcoin", "ethereum", "binancecoin", "ripple", "solana",
    "dogecoin", "cardano", "tron", "avalanche-2", "chainlink",
    "polkadot", "litecoin", "near", "aptos", "arbitrum",
    "optimism", "sui", "aave", "uniswap", "pepe",
    "render-token", "injective-protocol", "jupiter-exchange-solana",
    "celestia", "filecoin", "vechain", "the-graph", "maker",
]


@dataclass
class MarketSignal:
    regime: str
    score: int
    breadth: dict[str, float]
    btc_metrics: dict[str, float | None]
    eth_metrics: dict[str, float | None]
    btc_acceleration: float
    triggers: list[dict[str, Any]]


class MarketEngine:
    """
    Single source of truth for all numeric market data.

    NEWS never decides market facts.
    PULSE receives its context from this engine.
    MARKET posts are generated directly from these numbers.
    """

    def __init__(self, api_base: str):
        self.api_base = api_base.rstrip("/")
        self.history: dict[str, list[dict[str, float]]] = {}
        self.history_limit = 120

    async def fetch_market(self, client: httpx.AsyncClient) -> dict[str, Any]:
        global_task = client.get(f"{self.api_base}/global")
        coins_task = client.get(
            f"{self.api_base}/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": "bitcoin,ethereum",
                "order": "market_cap_desc",
                "per_page": 2,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "1h,24h",
            },
        )
        global_response, coins_response = await asyncio.gather(global_task, coins_task)
        global_response.raise_for_status()
        coins_response.raise_for_status()

        global_data = global_response.json().get("data", {})
        coins = coins_response.json()
        by_id = {coin.get("id"): coin for coin in coins}

        return {
            "market_cap": global_data.get("total_market_cap", {}).get("usd"),
            "volume": global_data.get("total_volume", {}).get("usd"),
            "market_cap_change_24h": global_data.get("market_cap_change_percentage_24h_usd"),
            "btc_dominance": global_data.get("market_cap_percentage", {}).get("btc"),
            "eth_dominance": global_data.get("market_cap_percentage", {}).get("eth"),
            "btc": by_id.get("bitcoin"),
            "eth": by_id.get("ethereum"),
        }

    async def fetch_monitored_coins(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        response = await client.get(
            f"{self.api_base}/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ",".join(MONITORED_COINS),
                "order": "market_cap_desc",
                "per_page": len(MONITORED_COINS),
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "1h,24h",
            },
        )
        response.raise_for_status()
        return response.json()

    def update_history(self, coins: list[dict[str, Any]], now: float) -> None:
        for coin in coins:
            coin_id = coin.get("id")
            price = coin.get("current_price")
            if not coin_id or price is None:
                continue
            history = self.history.setdefault(coin_id, [])
            history.append({"time": now, "price": float(price)})
            if len(history) > self.history_limit:
                del history[:-self.history_limit]

    def _change_from_history(self, coin_id: str, now: float, seconds: int) -> float | None:
        history = self.history.get(coin_id) or []
        if len(history) < 2:
            return None
        target = now - seconds
        candidates = [point for point in history if point["time"] <= target]
        if not candidates:
            return None
        old = candidates[-1]["price"]
        new = history[-1]["price"]
        if old == 0:
            return None
        return (new - old) / old * 100

    def metrics(self, coin: dict[str, Any], now: float) -> dict[str, float | None]:
        coin_id = coin.get("id") or ""
        return {
            "5m": self._change_from_history(coin_id, now, 5 * 60),
            "15m": self._change_from_history(coin_id, now, 15 * 60),
            "30m": self._change_from_history(coin_id, now, 30 * 60),
            "1h": self._change_from_history(coin_id, now, 60 * 60),
            "24h": coin.get("price_change_percentage_24h_in_currency")
                   if coin.get("price_change_percentage_24h_in_currency") is not None
                   else coin.get("price_change_percentage_24h"),
        }

    @staticmethod
    def _acceleration(metrics: dict[str, float | None]) -> float:
        change_5m = abs(num(metrics.get("5m")))
        change_1h = abs(num(metrics.get("1h")))
        expected = change_1h / 12
        if expected <= 0:
            return 0.0
        return change_5m / expected

    def breadth(self, coins: list[dict[str, Any]], now: float) -> dict[str, float]:
        positive = negative = neutral = 0
        changes: list[float] = []

        for coin in coins:
            change = self.metrics(coin, now).get("1h")
            if change is None:
                continue
            changes.append(float(change))
            if change > 0.15:
                positive += 1
            elif change < -0.15:
                negative += 1
            else:
                neutral += 1

        total = positive + negative + neutral
        if total == 0:
            return {
                "positive": 0, "negative": 0, "neutral": 0,
                "positive_pct": 0.0, "negative_pct": 0.0,
                "average_change": 0.0,
            }

        return {
            "positive": positive,
            "negative": negative,
            "neutral": neutral,
            "positive_pct": positive / total * 100,
            "negative_pct": negative / total * 100,
            "average_change": sum(changes) / len(changes) if changes else 0.0,
        }

    @staticmethod
    def _find(coins: list[dict[str, Any]], coin_id: str) -> dict[str, Any]:
        return next((coin for coin in coins if coin.get("id") == coin_id), {})

    def analyze(
        self,
        coins: list[dict[str, Any]],
        market: dict[str, Any],
        now: float,
    ) -> MarketSignal:
        breadth = self.breadth(coins, now)
        btc_metrics = self.metrics(self._find(coins, "bitcoin"), now)
        eth_metrics = self.metrics(self._find(coins, "ethereum"), now)

        btc_change = num(btc_metrics.get("1h"))
        eth_change = num(eth_metrics.get("1h"))
        pos = breadth["positive_pct"]
        neg = breadth["negative_pct"]

        if btc_change > 0.8 and eth_change > 0.8 and pos >= 70:
            regime = "BROAD_RALLY"
        elif btc_change < -0.8 and eth_change < -0.8 and neg >= 70:
            regime = "BROAD_SELLOFF"
        elif abs(btc_change) >= 1.5 and pos < 70 and neg < 70:
            regime = "BTC_LED_MOVE"
        elif abs(btc_change) < 0.8 and eth_change > 1.0 and pos >= 70:
            regime = "ALTCOIN_ROTATION"
        elif (btc_change > 0.5 and eth_change < -0.5) or (
            btc_change < -0.5 and eth_change > 0.5
        ):
            regime = "MARKET_DIVERGENCE"
        else:
            regime = "NEUTRAL"

        score = 0
        if abs(btc_change) >= 0.8:
            score += 2
        if abs(eth_change) >= 1.0:
            score += 2
        if pos >= 70 or neg >= 70:
            score += 3
        if abs(breadth["average_change"]) >= 0.7:
            score += 2
        if regime != "NEUTRAL":
            score += 2

        triggers = []
        for coin in coins:
            metrics = self.metrics(coin, now)
            change = metrics.get("1h")
            if change is not None and abs(change) >= 3:
                triggers.append({
                    "id": coin.get("id"),
                    "symbol": coin.get("symbol", "").upper(),
                    "1h": change,
                })

        return MarketSignal(
            regime=regime,
            score=score,
            breadth=breadth,
            btc_metrics=btc_metrics,
            eth_metrics=eth_metrics,
            btc_acceleration=self._acceleration(btc_metrics),
            triggers=sorted(triggers, key=lambda x: abs(num(x["1h"])), reverse=True),
        )


def should_publish_market(signal: MarketSignal) -> bool:
    return (
        signal.score >= 7
        or signal.breadth["positive_pct"] >= 80
        or signal.breadth["negative_pct"] >= 80
    )


def should_publish_pulse(signal: MarketSignal) -> bool:
    return (
        signal.score >= 10
        or signal.regime in {"BROAD_RALLY", "BROAD_SELLOFF", "MARKET_DIVERGENCE"}
        or signal.btc_acceleration >= 2
    )
