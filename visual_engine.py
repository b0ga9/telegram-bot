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


def build_market_card(market: dict[str, Any], signal: Optional[dict[str, Any]] = None,
                      output_dir="/tmp/trd_visuals"):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    change = _n(market.get("market_cap_change_24h"))
    regime = (signal or {}).get("regime", "NEUTRAL")

    if regime == "BROAD_SELLOFF" or change <= -1:
        state, accent, sub = "ДАВЛЕНИЕ", RED, "Рынок снижается"
    elif regime == "BROAD_RALLY" or change >= 1:
        state, accent, sub = "РОСТ", GREEN, "Рынок растёт"
    elif change > 0:
        state, accent, sub = "ВОССТАНОВЛЕНИЕ", GREEN, "Появляется спрос"
    else:
        state, accent, sub = "СТАБИЛЬНО", YELLOW, "Сильного общего движения нет"

    _header(d, "РЫНОК", "Состояние рынка сейчас")

    _box(d, (72, 255, 1464, 425))
    d.text((112, 288), "СОСТОЯНИЕ", font=_font(20, True), fill=MUTED)
    d.text((112, 326), state, font=_font(54, True), fill=accent)
    d.text((112, 392), sub, font=_font(23), fill=TEXT)

    btc, eth = market.get("btc") or {}, market.get("eth") or {}
    cards = [
        ("BTC", _price(btc.get("current_price")), _change(btc, "24h")),
        ("ETH", _price(eth.get("current_price")), _change(eth, "24h")),
        ("КАПИТАЛИЗАЦИЯ", _money(market.get("market_cap")), change),
    ]
    positions = [72, 547, 1022]
    for x, (label, value, pct) in zip(positions, cards):
        _box(d, (x, 460, x + 422, 700), radius=22, fill=PANEL2)
        d.text((x + 34, 493), label, font=_font(19, True), fill=MUTED)
        # Fit long values without overlapping neighbouring elements.
        value_font = _font(34 if len(value) > 12 else 40, True)
        d.text((x + 34, 552), value, font=value_font, fill=TEXT)
        col = GREEN if _n(pct) > 0 else RED if _n(pct) < 0 else MUTED
        d.text((x + 34, 625), f"24 ЧАСА  {_pct(pct)}", font=_font(25, True), fill=col)

    coins = market.get("coins") or []
    if signal and signal.get("breadth"):
        breadth = signal["breadth"]
        up = _n(breadth.get("positive_pct"))
        down = _n(breadth.get("negative_pct"))
    else:
        changes = [_n(_change(c, "24h")) for c in coins]
        if changes:
            up = 100 * sum(v > 0 for v in changes) / len(changes)
            down = 100 * sum(v < 0 for v in changes) / len(changes)
        else:
            up = down = 0

    d.text((72, 755), "ШИРИНА РЫНКА", font=_font(20, True), fill=MUTED)
    d.text((72, 792), f"РАСТУТ  {up:.0f}%", font=_font(26, True), fill=GREEN)
    d.text((330, 792), f"СНИЖАЮТСЯ  {down:.0f}%", font=_font(26, True), fill=RED)

    bar_x1, bar_x2, bar_y1, bar_y2 = 72, 1464, 842, 866
    d.rounded_rectangle((bar_x1, bar_y1, bar_x2, bar_y2), radius=10, fill=LINE)
    total = up + down
    if total > 0:
        split = bar_x1 + (bar_x2 - bar_x1) * (up / total)
        if split > bar_x1:
            d.rounded_rectangle((bar_x1, bar_y1, split, bar_y2), radius=10, fill=GREEN)

    d.text((72, 895), "BTC доминация", font=_font(19, True), fill=MUTED)
    d.text((250, 895), _pct(market.get("btc_dominance")), font=_font(19, True), fill=TEXT)
    _footer(d)
    return _save(img, output_dir, "market", f"{change}|{regime}|{up}|{down}|{market.get('market_cap')}|{market.get('btc_dominance')}")


def build_price_card(coins: list[dict[str, Any]], output_dir="/tmp/trd_visuals"):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _header(d, "ЦЕНЫ", "Криптовалюты сейчас")
    d.text((108, 245), "МОНЕТА", font=_font(19, True), fill=MUTED)
    d.text((430, 245), "ЦЕНА", font=_font(19, True), fill=MUTED)
    d.text((800, 245), "1 ЧАС", font=_font(19, True), fill=MUTED)
    d.text((1110, 245), "24 ЧАСА", font=_font(19, True), fill=MUTED)

    y = 285
    for i, coin in enumerate(coins[:8]):
        if i % 2 == 0:
            _box(d, (72, y - 12, 1464, y + 66), radius=16, fill=PANEL)
        symbol = str(coin.get("symbol", "")).upper()
        d.text((108, y + 10), symbol, font=_font(26, True), fill=TEXT)
        d.text((430, y + 10), _price(coin.get("current_price")), font=_font(26, True), fill=TEXT)
        c1, c24 = _change(coin, "1h"), _change(coin, "24h")
        d.text((800, y + 10), _pct(c1), font=_font(24, True), fill=GREEN if _n(c1) > 0 else RED if _n(c1) < 0 else MUTED)
        d.text((1110, y + 10), _pct(c24), font=_font(24, True), fill=GREEN if _n(c24) > 0 else RED if _n(c24) < 0 else MUTED)
        y += 82

    d.text((72, 925), "Изменение показано за 1 час и 24 часа", font=_font(19, True), fill=MUTED)
    _footer(d)
    return _save(img, output_dir, "prices", "|".join(f"{c.get('id')}:{c.get('current_price')}:{_change(c)}" for c in coins[:8]))


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
