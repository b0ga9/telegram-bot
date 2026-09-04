"""TRD Visual Engine — русские data cards, без AI для MARKET/PULSE/PRICES."""
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
PANEL2 = (26, 30, 38)
TEXT = (246, 248, 251)
MUTED = (157, 166, 180)
WHITE = (255, 255, 255)
GREEN = (74, 218, 143)
RED = (247, 94, 111)
YELLOW = (244, 193, 75)
LINE = (48, 54, 64)


def _font(size: int, bold: bool = False):
    names = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
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
    return coin.get(
        f"price_change_percentage_{period}_in_currency"
    ) or coin.get(f"price_change_percentage_{period}")


def _box(d, xy, radius=22, fill=PANEL):
    d.rounded_rectangle(xy, radius=radius, fill=fill)


def _header(d, section, title):
    d.text((72, 48), "TRD", font=_font(42, True), fill=WHITE)
    d.text((72, 105), section, font=_font(24, True), fill=MUTED)
    d.text((72, 148), title, font=_font(55, True), fill=TEXT)


def _footer(d):
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    d.text((72, 958), "TRD • ДАННЫЕ РЫНКА", font=_font(18, True), fill=MUTED)
    d.text((1320, 958), now, font=_font(18), fill=MUTED)


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
    d.text((112, 290), "СОСТОЯНИЕ", font=_font(20, True), fill=MUTED)
    d.text((112, 330), state, font=_font(54, True), fill=accent)
    d.text((112, 390), sub, font=_font(23), fill=TEXT)

    btc, eth = market.get("btc") or {}, market.get("eth") or {}
    cards = [
        ("BTC", _price(btc.get("current_price")), _change(btc)),
        ("ETH", _price(eth.get("current_price")), _change(eth)),
        ("КАПИТАЛИЗАЦИЯ", _money(market.get("market_cap")), change),
    ]

    x_positions = (72, 547, 1022)
    for x, (label, value, pct) in zip(x_positions, cards):
        _box(d, (x, 455, x + 442, 730), radius=22, fill=PANEL2)
        d.text((105, 492), label, font=_font(20, True), fill=MUTED)
        d.text((105, 555), value, font=_font(38, True), fill=TEXT)
        col = GREEN if _n(pct) > 0 else RED if _n(pct) < 0 else MUTED
        d.text((105, 630), f"24 ЧАСА   {_pct(pct)}", font=_font(27, True), fill=col)

    breadth = (signal or {}).get("breadth", {})
    up = _n(breadth.get("positive_pct"))
    down = _n(breadth.get("negative_pct"))
    d.text((72, 785), "ШИРИНА РЫНКА", font=_font(20, True), fill=MUTED)
    d.text((72, 820), f"РАСТУТ  {up:.0f}%", font=_font(26, True), fill=GREEN)
    d.text((335, 820), f"СНИЖАЮТСЯ  {down:.0f}%", font=_font(26, True), fill=RED)
    d.rounded_rectangle((72, 870, 1464, 896), radius=10, fill=LINE)
    if up + down:
        split = 72 + 1392 * up / (up + down)
        d.rounded_rectangle((72, 870, split, 896), radius=10, fill=GREEN)

    _footer(d)
    return _save(img, output_dir, "market",
                 f"{change}|{regime}|{up}|{down}|{market.get('market_cap')}")


def build_price_card(coins: list[dict[str, Any]], output_dir="/tmp/trd_visuals"):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _header(d, "ЦЕНЫ", "Криптовалюты сейчас")

    d.text((108, 245), "МОНЕТА", font=_font(18, True), fill=MUTED)
    d.text((430, 245), "ЦЕНА", font=_font(18, True), fill=MUTED)
    d.text((800, 245), "1 ЧАС", font=_font(18, True), fill=MUTED)
    d.text((1110, 245), "24 ЧАСА", font=_font(18, True), fill=MUTED)

    y = 285
    for i, coin in enumerate(coins[:8]):
        if i % 2 == 0:
            _box(d, (72, y - 12, 1464, y + 66), radius=16)
        symbol = str(coin.get("symbol", "")).upper()
        d.text((108, y + 10), symbol, font=_font(25, True), fill=TEXT)
        d.text((430, y + 10), _price(coin.get("current_price")), font=_font(25, True), fill=TEXT)
        c1, c24 = _change(coin, "1h"), _change(coin, "24h")
        d.text((800, y + 10), _pct(c1), font=_font(23, True),
               fill=GREEN if _n(c1) > 0 else RED if _n(c1) < 0 else MUTED)
        d.text((1110, y + 10), _pct(c24), font=_font(23, True),
               fill=GREEN if _n(c24) > 0 else RED if _n(c24) < 0 else MUTED)
        y += 82

    d.text((72, 925), "Движение показано за 1 час и 24 часа",
           font=_font(19, True), fill=MUTED)
    _footer(d)
    return _save(img, output_dir, "prices",
                 "|".join(f"{c.get('id')}:{c.get('current_price')}:{_change(c)}"
                          for c in coins[:8]))


def build_pulse_card(market: dict[str, Any], signal: Optional[dict[str, Any]] = None,
                     output_dir="/tmp/trd_visuals"):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _header(d, "ИМПУЛЬС", "Движение рынка сейчас")

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
    d.text((72, 265), state, font=_font(43, True), fill=accent)
    d.text((72, 325), "Изменение за последний час", font=_font(21, True), fill=MUTED)

    btc, eth = market.get("btc") or {}, market.get("eth") or {}
    rows = [
        ("BTC", btc, (signal or {}).get("btc_metrics", {}).get("1h")),
        ("ETH", eth, (signal or {}).get("eth_metrics", {}).get("1h")),
    ]
    y = 390
    for name, coin, ch in rows:
        _box(d, (72, y, 930, y + 145), radius=22, fill=PANEL2)
        d.text((108, y + 27), name, font=_font(25, True), fill=MUTED)
        d.text((300, y + 20), _price(coin.get("current_price")), font=_font(38, True), fill=TEXT)
        d.text((675, y + 28), _pct(ch), font=_font(30, True),
               fill=GREEN if _n(ch) > 0 else RED if _n(ch) < 0 else MUTED)
        y += 165

    breadth = (signal or {}).get("breadth", {})
    _box(d, (970, 390, 1464, 700), radius=22)
    d.text((1010, 435), "ШИРИНА РЫНКА", font=_font(20, True), fill=MUTED)
    d.text((1010, 505), f"РАСТУТ  {_n(breadth.get('positive_pct')):.0f}%",
           font=_font(30, True), fill=GREEN)
    d.text((1010, 575), f"СНИЖАЮТСЯ  {_n(breadth.get('negative_pct')):.0f}%",
           font=_font(30, True), fill=RED)
    d.text((1010, 645), "Чем шире движение, тем сильнее сигнал",
           font=_font(17), fill=MUTED)

    _footer(d)
    return _save(img, output_dir, "pulse", f"{regime}|{rows}|{breadth}")


async def generate_news_image(api_key: str, api_url: str, model: str,
                              post_text: str, event_key: str,
                              output_dir="/tmp/trd_visuals"):
    """Редакционный AI-визуал: модель не должна рисовать текст."""
    if not api_key:
        return None

    clean = re.sub(r"https?://\S+", "", post_text)
    clean = re.sub(r"\s+", " ", clean).strip()[:2200]

    prompt = f"""
Создай горизонтальную редакционную иллюстрацию для русского финансового медиа TRD.

Событие:
{clean}

КРИТИЧЕСКИ ВАЖНО:
- НЕ РИСУЙ НИКАКОЙ ТЕКСТ.
- НЕ РИСУЙ БУКВЫ.
- НЕ РИСУЙ ЦИФРЫ.
- НЕ РИСУЙ ГРАФИКИ С ПОДПИСЯМИ.
- НЕ РИСУЙ ЛОГОТИПЫ И ВОДЯНЫЕ ЗНАКИ.
- Не пытайся написать заголовок на изображении.
- Все слова и цифры будут добавлены отдельно Telegram-ботом.

Визуальный стиль:
премиальная тёмная финансовая редакционная фотография;
один понятный главный сюжет;
минимум объектов;
реалистичное освещение;
чёткий главный объект;
никакого коллажа;
никакой инфографики;
никакой визуальной перегрузки.

Изображение должно объяснять событие без единого слова.
"""
    payload = {
        "model": model,
        "input": prompt,
        "tools": [{"type": "image_generation"}],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

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
