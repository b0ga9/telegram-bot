from __future__ import annotations

import json

import httpx

from formatting import escape, format_money, format_pct, format_price, post_html
from market_engine import MarketSignal


def build_pulse_context(market: dict, signal: MarketSignal, recent_news: str) -> str:
    btc = market.get("btc") or {}
    eth = market.get("eth") or {}

    return f"""
Ты — аналитик TRD Pulse.

Напиши ОЧЕНЬ короткий текст о текущем движении рынка.
Не повторяй просто цифры. Объясни только главный смысл.

ФАКТЫ:
BTC: {format_price(btc.get("current_price"))}
BTC 1ч: {format_pct(signal.btc_metrics.get("1h"))}
BTC 24ч: {format_pct(signal.btc_metrics.get("24h"))}

ETH: {format_price(eth.get("current_price"))}
ETH 1ч: {format_pct(signal.eth_metrics.get("1h"))}
ETH 24ч: {format_pct(signal.eth_metrics.get("24h"))}

Market Cap: {format_money(market.get("market_cap"))}
Market Cap 24ч: {format_pct(market.get("market_cap_change_24h"))}

BTC Dominance: {format_pct(market.get("btc_dominance"))}

Режим: {signal.regime}
Score: {signal.score}
Растут: {signal.breadth["positive_pct"]:.0f}%
Снижаются: {signal.breadth["negative_pct"]:.0f}%
Среднее движение: {signal.breadth["average_change"]:.2f}%
Ускорение BTC: {signal.btc_acceleration:.2f}x

Последняя важная новость:
{recent_news[:1200]}

Верни ТОЛЬКО JSON:
{{
  "headline": "до 8 слов",
  "main": "1-2 коротких предложения",
  "focus": "одна короткая фраза"
}}

Не давай торговых рекомендаций.
Не выдумывай причины, если их нет в данных.
"""


async def generate_pulse(client: httpx.AsyncClient, *, api_url: str, api_key: str, model: str,
                         market: dict, signal: MarketSignal, recent_news: str) -> dict[str, str]:
    response = await client.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "input": build_pulse_context(market, signal, recent_news),
            "max_output_tokens": 500,
        },
    )
    response.raise_for_status()
    data = response.json()

    chunks = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    chunks.append(content.get("text", ""))

    raw = "".join(chunks).strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()

    parsed = json.loads(raw)
    return {
        "headline": str(parsed.get("headline", "")).strip(),
        "main": str(parsed.get("main", "")).strip(),
        "focus": str(parsed.get("focus", "")).strip(),
    }


def format_pulse_post(pulse: dict[str, str], signal: MarketSignal) -> str:
    body = (
        f"<b>Сигнал</b><br>{escape(pulse.get('main') or 'Рынок показывает заметное движение.')}<br><br>"
        f"<b>Ширина рынка</b><br>"
        f"<u>{signal.breadth['positive_pct']:.0f}% растут</u> • "
        f"<u>{signal.breadth['negative_pct']:.0f}% снижаются</u><br><br>"
        f"<b>Фокус</b><br><i>{escape(pulse.get('focus') or signal.regime)}</i>"
    )
    return post_html("PULSE", pulse.get("headline") or "Движение рынка", body)
