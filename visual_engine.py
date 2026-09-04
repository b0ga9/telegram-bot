"""TRD Visual Engine: simple Russian-first cards, zero OpenAI for market visuals."""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

W, H = 1536, 1024
BG = (10, 12, 16)
PANEL = (20, 23, 29)
PANEL2 = (25, 29, 36)
TEXT = (246, 248, 251)
MUTED = (157, 166, 180)
WHITE = (255, 255, 255)
GREEN = (74, 218, 143)
RED = (247, 94, 111)
YELLOW = (244, 193, 75)
LINE = (48, 54, 64)


def _font(size: int, bold: bool = False):
    names = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ] if bold else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
    )
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def _n(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pct(v):
    if v is None:
        return "—"
    x = _n(v)
    return f"{'+' if x > 0 else ''}{x:.2f}%"


def _price(v):
    if v is None:
        return "—"
    x = _n(v)
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    return f"${x:.4f}"


def _money(v):
    if v is None:
        return "—"
    x = _n(v)
    if x >= 1e12:
        return f"${x/1e12:.2f} трлн"
    if x >= 1e9:
        return f"${x/1e9:.2f} млрд"
    return f"${x:,.0f}"


def _change(coin, period="24h"):
    return coin.get(f"price_change_percentage_{period}_in_currency") or coin.get(
        f"price_change_percentage_{period}"
    )


def _box(d, xy, radius=24, fill=PANEL):
    d.rounded_rectangle(xy, radius=radius, fill=fill)


def _header(d, section, title):
    d.text((72, 52), "TRD", font=_font(42, True), fill=WHITE)
    d.text((72, 112), section, font=_font(24, True), fill=MUTED)
    d.text((72, 155), title, font=_font(58, True), fill=TEXT)


def _footer(d):
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    d.text((72, 958), "TRD • ДАННЫЕ РЫНКА", font=_font(18, True), fill=MUTED)
    d.text((1325, 958), now, font=_font(18), fill=MUTED)


def _save(img, output_dir, prefix, seed):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    path = out / f"{prefix}_{digest}.jpg"
    img.save(path, "JPEG", quality=94, optimize=True)
    return path


def _fit_font(text, max_size, max_width, bold=True):
    size = max_size
    while size > 18:
        font = _font(size, bold)
        box = font.getbbox(str(text))
        if box[2] - box[0] <= max_width:
            return font
        size -= 2
    return _font(18, bold)


def _coin_4h(coin):
    direct = coin.get("price_change_percentage_4h_in_currency")
    if direct is not None:
        return direct
    prices = ((coin.get("sparkline_in_7d") or {}).get("price") or [])
    if len(prices) >= 5:
        try:
            old, cur = float(prices[-5]), float(prices[-1])
            return (cur - old) / old * 100 if old else None
        except (TypeError, ValueError):
            pass
    return None


def _best_mover(coin):
    vals = [("1ч", _change(coin, "1h")), ("4ч", _coin_4h(coin)), ("24ч", _change(coin, "24h"))]
    vals = [(p, v) for p, v in vals if v is not None]
    return max(vals, key=lambda x: abs(_n(x[1]))) if vals else ("24ч", 0)


def build_market_card(market: dict[str, Any], signal: Optional[dict[str, Any]] = None, output_dir="/tmp/trd_visuals"):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    change = _n(market.get("market_cap_change_24h"))
    if change > 1:
        state, accent, sub = "РОСТ", GREEN, "Покупатели усиливают движение"
    elif change < -1:
        state, accent, sub = "ДАВЛЕНИЕ", RED, "Рынок снижается"
    else:
        state, accent, sub = "СТАБИЛИЗАЦИЯ", YELLOW, "Сильного общего движения нет"

    _header(d, "РЫНОК", "Состояние рынка сейчас")
    _box(d, (72, 250, 1464, 405))
    d.text((108, 278), "СОСТОЯНИЕ", font=_font(18, True), fill=MUTED)
    d.text((108, 310), state, font=_font(45, True), fill=accent)
    d.text((420, 326), sub, font=_font(23), fill=TEXT)

    btc, eth = market.get("btc") or {}, market.get("eth") or {}
    cards = [("BTC", _price(btc.get("current_price")), _change(btc,"24h")), ("ETH", _price(eth.get("current_price")), _change(eth,"24h")), ("КАП. РЫНКА", _money(market.get("market_cap")), change)]
    x = 72
    for label, value, pct in cards:
        _box(d, (x, 440, x+430, 630), radius=20, fill=PANEL2)
        tx = x + 30
        d.text((tx, 468), label, font=_font(19, True), fill=MUTED)
        vf = _fit_font(value, 35, 365, True)
        d.text((tx, 510), value, font=vf, fill=TEXT)
        col = GREEN if _n(pct)>0 else RED if _n(pct)<0 else MUTED
        d.text((tx, 575), f"24ч  {_pct(pct)}", font=_font(25, True), fill=col)
        x += 455

    d.text((72, 685), "СИЛЬНЫЕ ДВИЖЕНИЯ", font=_font(20, True), fill=MUTED)
    rows = []
    for coin in (market.get("gainers") or [])[:3]: rows.append(("+", coin, GREEN))
    for coin in (market.get("losers") or [])[:3]: rows.append(("−", coin, RED))
    y = 725
    for sign, coin, col in rows:
        symbol = str(coin.get("symbol", "")).upper()
        period, pct = _best_mover(coin)
        d.text((82, y), sign, font=_font(23, True), fill=col)
        d.text((125, y), symbol, font=_font(23, True), fill=TEXT)
        d.text((360, y), _pct(pct), font=_font(22, True), fill=col)
        d.text((560, y), period, font=_font(19, True), fill=MUTED)
        y += 38

    d.text((900, 685), "BTC DOMINANCE", font=_font(18, True), fill=MUTED)
    d.text((900, 718), f"{_n(market.get('btc_dominance')):.2f}%", font=_font(34, True), fill=TEXT)
    d.text((900, 770), "Данные обновлены сейчас", font=_font(18), fill=MUTED)
    _footer(d)
    return _save(img, output_dir, "market", f"{change}|{market.get('market_cap')}|{[(c[1].get('id'),_best_mover(c[1])) for c in rows]}")


def build_price_card(coins: list[dict[str, Any]], output_dir="/tmp/trd_visuals"):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _header(d, "ЦЕНЫ", "Цены криптовалют сейчас")
    d.text((108, 235), "МОНЕТА", font=_font(18, True), fill=MUTED)
    d.text((430, 235), "ЦЕНА", font=_font(18, True), fill=MUTED)
    d.text((735, 235), "1 ЧАС", font=_font(18, True), fill=MUTED)
    d.text((980, 235), "4 ЧАСА", font=_font(18, True), fill=MUTED)
    d.text((1230, 235), "24 ЧАСА", font=_font(18, True), fill=MUTED)
    y = 275
    for i, coin in enumerate(coins[:9]):
        if i % 2 == 0: _box(d, (72, y-10, 1464, y+65), radius=14, fill=PANEL)
        symbol = str(coin.get("symbol", "")).upper()
        d.text((108, y+8), symbol, font=_font(24, True), fill=TEXT)
        d.text((430, y+8), _price(coin.get("current_price")), font=_font(24, True), fill=TEXT)
        vals=[_change(coin,"1h"),_coin_4h(coin),_change(coin,"24h")]
        for x,val in zip((735,980,1230),vals):
            col=GREEN if _n(val)>0 else RED if _n(val)<0 else MUTED
            d.text((x,y+8), _pct(val), font=_font(22, True), fill=col)
        y += 75
    d.text((72, 955), "1ч · 4ч · 24ч  |  TRD PRICES", font=_font(18, True), fill=MUTED)
    _footer(d)
    return _save(img, output_dir, "prices", "|".join(f"{c.get('id')}:{c.get('current_price')}:{_coin_4h(c)}" for c in coins[:9]))


def build_pulse_card(market: dict[str, Any], signal: Optional[dict[str, Any]] = None,
                     output_dir="/tmp/trd_visuals"):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _header(d, "ИМПУЛЬС", "Что происходит прямо сейчас")

    regime = (signal or {}).get("regime", "NEUTRAL")
    labels = {
        "BROAD_RALLY": ("ШИРОКИЙ РОСТ", GREEN),
        "BROAD_SELLOFF": ("ШИРОКОЕ СНИЖЕНИЕ", RED),
        "BTC_LED_MOVE": ("ДВИЖЕНИЕ ВОКРУГ BTC", YELLOW),
        "ALTCOIN_ROTATION": ("АКТИВНОСТЬ АЛЬТКОИНОВ", GREEN),
        "MARKET_DIVERGENCE": ("РАСХОЖДЕНИЕ", YELLOW),
        "NEUTRAL": ("СТАБИЛИЗАЦИЯ", MUTED),
    }
    state, accent = labels.get(regime, ("ДВИЖЕНИЕ РЫНКА", MUTED))
    d.text((72, 270), state, font=_font(45, True), fill=accent)
    d.text((72, 330), "Изменение за последний час", font=_font(22, True), fill=MUTED)

    btc, eth = market.get("btc") or {}, market.get("eth") or {}
    rows = [("BTC", btc, (signal or {}).get("btc_metrics", {}).get("1h")),
            ("ETH", eth, (signal or {}).get("eth_metrics", {}).get("1h"))]
    y = 400
    for name, coin, ch in rows:
        _box(d, (72, y, 930, y + 150), radius=22, fill=PANEL2)
        d.text((108, y + 28), name, font=_font(26, True), fill=MUTED)
        d.text((300, y + 22), _price(coin.get("current_price")), font=_font(39, True), fill=TEXT)
        d.text((675, y + 30), _pct(ch), font=_font(31, True), fill=GREEN if _n(ch) > 0 else RED if _n(ch) < 0 else MUTED)
        y += 175

    breadth = (signal or {}).get("breadth", {})
    _box(d, (970, 400, 1464, 725), radius=22)
    d.text((1010, 445), "ШИРИНА РЫНКА", font=_font(20, True), fill=MUTED)
    d.text((1010, 515), f"РАСТУТ  { _n(breadth.get('positive_pct')):.0f}%", font=_font(31, True), fill=GREEN)
    d.text((1010, 585), f"СНИЖАЮТСЯ  { _n(breadth.get('negative_pct')):.0f}%", font=_font(31, True), fill=RED)
    d.text((1010, 660), "Чем шире движение, тем сильнее сигнал", font=_font(18), fill=MUTED)

    _footer(d)
    return _save(img, output_dir, "pulse", f"{regime}|{rows}|{breadth}")


async def generate_news_image(api_key: str, api_url: str, model: str,
                              post_text: str, event_key: str,
                              output_dir="/tmp/trd_visuals"):
    """AI image only for high-importance NEWS; no text/figures are generated."""
    if not api_key:
        return None
    clean = re.sub(r"https?://\S+", "", post_text)
    clean = re.sub(r"\s+", " ", clean).strip()[:2200]
    prompt = f"""
Создай горизонтальную редакционную иллюстрацию для русского финансового медиа TRD.
Событие: {clean}

Требования: премиальная тёмная финансовая фотография/иллюстрация; один понятный главный сюжет;
визуально объяснить событие; без текста, букв, цифр, логотипов и водяных знаков;
не добавлять выдуманные котировки или даты; минимум деталей; реалистично.
"""
    payload = {"model": model, "input": prompt, "tools": [{"type": "image_generation"}]}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(api_url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    result = None
    for item in data.get("output", []):
        if item.get("type") == "image_generation_call":
            result = item.get("result")
            break
    if not result:
        raise RuntimeError("OpenAI не вернул изображение")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256((event_key + clean).encode()).hexdigest()[:12]
    path = out / f"news_{digest}.png"
    path.write_bytes(base64.b64decode(result))
    return path
