"""TRD Visual Engine: clean, data-first MARKET cards."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from formatting import format_money, format_pct, format_price, num
from market_engine import MarketSignal

W, H = 1600, 1100
BG = (8, 10, 14)
SURFACE = (17, 20, 27)
SURFACE_2 = (22, 26, 34)
TEXT = (242, 244, 248)
MUTED = (138, 145, 158)
LINE = (45, 50, 61)
UP = (86, 210, 151)
DOWN = (235, 104, 118)
NEUTRAL = (196, 166, 102)


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


def _card(draw, box, fill=SURFACE, radius=28):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=LINE, width=2)


def _change_color(value):
    value = num(value)
    return UP if value > 0 else DOWN if value < 0 else MUTED


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
        btc_metrics={"1h": None, "24h": None},
        eth_metrics={"1h": None, "24h": None},
        btc_acceleration=0, triggers=[],
    )

    btc = market.get("btc") or {}
    eth = market.get("eth") or {}
    cap_change = market.get("market_cap_change_24h")
    dominance = market.get("btc_dominance")
    pos = num(signal.breadth.get("positive_pct"))
    neg = num(signal.breadth.get("negative_pct"))
    avg = signal.breadth.get("average_change")

    # Minimal editorial header.
    draw.text((80, 70), "TRD", font=_font(34, True), fill=TEXT)
    draw.text((80, 118), "MARKET SNAPSHOT", font=_font(18, True), fill=MUTED)
    draw.text((80, 175), "Crypto market", font=_font(60, True), fill=TEXT)

    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    draw.text((1330, 86), now, font=_font(18), fill=MUTED)

    # Hero state card.
    state_change = num(cap_change)
    if state_change >= 1:
        state, accent, descriptor = "RISK ON", UP, "Broad market demand is improving"
    elif state_change <= -1:
        state, accent, descriptor = "RISK OFF", DOWN, "Selling pressure dominates the market"
    else:
        state, accent, descriptor = "BALANCED", NEUTRAL, "No decisive broad market impulse"

    _card(draw, (80, 285, 1520, 470), fill=SURFACE_2)
    draw.text((120, 325), "MARKET REGIME", font=_font(17, True), fill=MUTED)
    draw.text((120, 370), state, font=_font(48, True), fill=accent)
    draw.text((610, 385), descriptor, font=_font(24), fill=TEXT)
    draw.text((120, 435), signal.regime.replace("_", " "), font=_font(18, True), fill=MUTED)

    # Four equal metric cards. No text MARKET caption is needed in Telegram anymore.
    metrics = [
        ("BTC", format_price(btc.get("current_price")), "1H", signal.btc_metrics.get("1h")),
        ("ETH", format_price(eth.get("current_price")), "1H", signal.eth_metrics.get("1h")),
        ("TOTAL CAP", format_money(market.get("market_cap")), "24H", cap_change),
        ("BTC DOM", format_pct(dominance), "BREADTH", avg),
    ]
    gap, card_w = 24, 348
    y1, y2 = 510, 775
    x = 80
    for label, value, period, change in metrics:
        _card(draw, (x, y1, x + card_w, y2))
        draw.text((x + 32, y1 + 32), label, font=_font(17, True), fill=MUTED)
        draw.text((x + 32, y1 + 94), value, font=_font(37, True), fill=TEXT)
        draw.text((x + 32, y1 + 170), period, font=_font(15, True), fill=MUTED)
        draw.text((x + 32, y1 + 202), format_pct(change), font=_font(27, True), fill=_change_color(change))
        x += card_w + gap

    # Breadth card.
    _card(draw, (80, 815, 1520, 995), fill=SURFACE_2)
    draw.text((120, 855), "MARKET BREADTH", font=_font(17, True), fill=MUTED)
    draw.text((120, 900), f"{pos:.0f}%", font=_font(42, True), fill=UP)
    draw.text((280, 910), "ADVANCING", font=_font(17, True), fill=MUTED)
    draw.text((610, 900), f"{neg:.0f}%", font=_font(42, True), fill=DOWN)
    draw.text((770, 910), "DECLINING", font=_font(17, True), fill=MUTED)

    bar_x1, bar_x2 = 1120, 1480
    draw.rounded_rectangle((bar_x1, 880, bar_x2, 910), radius=15, fill=LINE)
    total = pos + neg
    if total > 0:
        split = int(bar_x1 + (bar_x2 - bar_x1) * pos / total)
        draw.rounded_rectangle((bar_x1, 880, split, 910), radius=15, fill=UP)
    draw.text((1120, 930), "ADVANCE / DECLINE", font=_font(14, True), fill=MUTED)

    draw.text((80, 1040), "TRD PULSE", font=_font(16, True), fill=MUTED)
    return _save(image, output_dir, f"{cap_change}|{signal.regime}|{pos}|{neg}|{market.get('market_cap')}")
