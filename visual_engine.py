"""TRD Visual Engine: deterministic cards for numeric MARKET posts."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from formatting import format_money, format_pct, format_price, num
from market_engine import MarketSignal

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
    names = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ] if bold else [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def _box(draw, xy, radius=24, fill=PANEL):
    draw.rounded_rectangle(xy, radius=radius, fill=fill)


def _header(draw, section, title):
    draw.text((72, 52), "TRD", font=_font(42, True), fill=WHITE)
    draw.rounded_rectangle((205, 58, 430, 105), radius=14, fill=PANEL)
    draw.text((228, 68), section, font=_font(20, True), fill=MUTED)
    draw.text((72, 155), title, font=_font(58, True), fill=TEXT)


def _footer(draw):
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    draw.text((72, 970), "TRD • MARKET DATA", font=_font(17, True), fill=MUTED)
    draw.text((1325, 970), now, font=_font(17), fill=MUTED)


def _save(image, output_dir, seed):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(seed.encode()).hexdigest()[:12]
    path = output_dir / f"market_{digest}.jpg"
    image.save(path, "JPEG", quality=94, optimize=True)
    return path


def build_market_card(
    market: dict[str, Any],
    signal: MarketSignal | None = None,
    output_dir="/tmp/trd_visuals",
):
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)

    signal = signal or MarketSignal(
        regime="NEUTRAL", score=0,
        breadth={"positive_pct": 0, "negative_pct": 0, "average_change": 0},
        btc_metrics={"1h": None}, eth_metrics={"1h": None},
        btc_acceleration=0, triggers=[],
    )

    change = num(market.get("market_cap_change_24h"))
    if signal.regime == "BROAD_SELLOFF" or change <= -1:
        state, accent, sub = "ДАВЛЕНИЕ", RED, "Рынок снижается"
    elif signal.regime == "BROAD_RALLY" or change >= 1:
        state, accent, sub = "РОСТ", GREEN, "Рынок растёт"
    elif change > 0:
        state, accent, sub = "ВОССТАНОВЛЕНИЕ", GREEN, "Появляется спрос"
    else:
        state, accent, sub = "СТАБИЛЬНО", YELLOW, "Сильного общего движения нет"

    _header(draw, "MARKET", "Состояние рынка")

    _box(draw, (72, 265, 1464, 440))
    draw.text((112, 300), "СОСТОЯНИЕ", font=_font(21, True), fill=MUTED)
    draw.text((112, 342), state, font=_font(58, True), fill=accent)
    draw.text((112, 405), sub, font=_font(24), fill=TEXT)

    btc = market.get("btc") or {}
    eth = market.get("eth") or {}
    cards = [
        ("BTC", format_price(btc.get("current_price")), signal.btc_metrics.get("1h")),
        ("ETH", format_price(eth.get("current_price")), signal.eth_metrics.get("1h")),
        ("КАПИТАЛИЗАЦИЯ", format_money(market.get("market_cap")), change),
    ]

    x = 72
    for label, value, pct in cards:
        _box(draw, (x, 475, x + 430, 770), radius=22, fill=PANEL2)
        draw.text((108, 512), label, font=_font(21, True), fill=MUTED)
        draw.text((108, 575), value, font=_font(39, True), fill=TEXT)
        value_num = num(pct)
        color = GREEN if value_num > 0 else RED if value_num < 0 else MUTED
        draw.text((108, 650), f"1ч  {format_pct(pct)}", font=_font(29, True), fill=color)
        x += 455

    pos = signal.breadth.get("positive_pct", 0)
    neg = signal.breadth.get("negative_pct", 0)
    draw.text((72, 820), "ШИРИНА РЫНКА", font=_font(20, True), fill=MUTED)
    draw.text((72, 855), f"РАСТУТ  {pos:.0f}%", font=_font(27, True), fill=GREEN)
    draw.text((330, 855), f"СНИЖАЮТСЯ  {neg:.0f}%", font=_font(27, True), fill=RED)
    draw.rounded_rectangle((72, 905, 1464, 928), radius=10, fill=LINE)
    if pos + neg > 0:
        split = 72 + 1392 * (pos / (pos + neg))
        draw.rounded_rectangle((72, 905, split, 928), radius=10, fill=GREEN)

    _footer(draw)
    return _save(
        image,
        output_dir,
        f"{change}|{signal.regime}|{pos}|{neg}|{market.get('market_cap')}",
    )
