"""
TRD Visual Engine
=================
Four visual formats:
  NEWS   -> AI-generated editorial image
  PULSE  -> dynamic deterministic market card
  MARKET -> deterministic market-state card
  PRICES -> deterministic price card

All data cards are generated locally so prices and percentages are never
invented by an image model.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image, ImageDraw, ImageFont


W, H = 1536, 1024
BG = (10, 12, 16)
PANEL = (18, 21, 28)
TEXT = (242, 244, 247)
MUTED = (145, 153, 166)
LINE = (47, 54, 65)
UP = (91, 224, 154)
DOWN = (255, 103, 118)
ACCENT = (205, 214, 228)


def _font(size: int, bold: bool = False):
    candidates = []
    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
    else:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
    candidates += [
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    ]
    for path in candidates:
        p = Path(path)
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def _safe_num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _pct(value):
    if value is None:
        return "—"
    value = _safe_num(value)
    return f"{'+' if value > 0 else ''}{value:.2f}%"


def _price(value):
    if value is None:
        return "—"
    value = _safe_num(value)
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.4f}"


def _money(value):
    if value is None:
        return "—"
    value = _safe_num(value)
    if value >= 1_000_000_000_000:
        return f"${value/1_000_000_000_000:.2f}T"
    if value >= 1_000_000_000:
        return f"${value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    return f"${value:,.0f}"


def _rounded(draw, box, radius=28, fill=PANEL, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _header(draw, label, title):
    draw.text((80, 65), "TRD", font=_font(42, True), fill=TEXT)
    draw.text((80, 120), label.upper(), font=_font(23, True), fill=MUTED)
    draw.text((80, 165), title, font=_font(58, True), fill=TEXT)


def _footer(draw):
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    draw.text((80, H - 55), "TRD VISUAL ENGINE", font=_font(18, True), fill=MUTED)
    draw.text((W - 210, H - 55), now, font=_font(18), fill=MUTED)


def _save(img: Image.Image, output_dir: Path, prefix: str, seed: str):
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    path = output_dir / f"{prefix}_{digest}.jpg"
    img.save(path, "JPEG", quality=94, optimize=True)
    return path


def build_market_card(market: dict[str, Any], signal: Optional[dict[str, Any]] = None,
                      output_dir: Path = Path("/tmp/trd_visuals")):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _header(d, "MARKET STATE", "Рынок сейчас")

    btc = market.get("btc") or {}
    eth = market.get("eth") or {}
    mc_change = _safe_num(market.get("market_cap_change_24h"))
    regime = (signal or {}).get("regime", "NEUTRAL")
    score = (signal or {}).get("score")

    state = "PRESSURE" if mc_change < -0.5 else "RECOVERY" if mc_change > 0.5 else "NEUTRAL"
    if regime == "BROAD_SELLOFF":
        state = "PRESSURE"
    elif regime == "BROAD_RALLY":
        state = "RALLY"

    state_fill = DOWN if state == "PRESSURE" else UP if state in {"RECOVERY", "RALLY"} else ACCENT

    _rounded(d, (80, 255, 1456, 430))
    d.text((125, 292), "MARKET STATE", font=_font(22, True), fill=MUTED)
    d.text((125, 330), state, font=_font(68, True), fill=state_fill)
    if score is not None:
        d.text((1130, 315), f"SCORE {score}", font=_font(27, True), fill=MUTED)

    cards = [
        ("₿ BTC", _price(btc.get("current_price")), _pct(btc.get("price_change_percentage_24h_in_currency") or btc.get("price_change_percentage_24h"))),
        ("Ξ ETH", _price(eth.get("current_price")), _pct(eth.get("price_change_percentage_24h_in_currency") or eth.get("price_change_percentage_24h"))),
        ("MARKET CAP", _money(market.get("market_cap")), _pct(mc_change)),
    ]
    x = 80
    for label, value, change in cards:
        _rounded(d, (x, 475, x + 430, 790))
        d.text((x + 35, 515), label, font=_font(21, True), fill=MUTED)
        d.text((x + 35, 575), value, font=_font(45, True), fill=TEXT)
        c = UP if change.startswith("+") else DOWN if change.startswith("-") else ACCENT
        d.text((x + 35, 650), change, font=_font(31, True), fill=c)
        x += 455

    breadth = (signal or {}).get("breadth", {})
    d.text((80, 850), "BREADTH", font=_font(20, True), fill=MUTED)
    d.text((80, 890), f"UP {breadth.get('positive_pct', 0):.0f}%", font=_font(28, True), fill=UP)
    d.text((280, 890), f"DOWN {breadth.get('negative_pct', 0):.0f}%", font=_font(28, True), fill=DOWN)
    _footer(d)

    seed = f"market|{market.get('market_cap')}|{mc_change}|{regime}|{score}"
    return _save(img, output_dir, "market", seed)


def build_price_card(coins: list[dict[str, Any]], output_dir: Path = Path("/tmp/trd_visuals")):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _header(d, "PRICES", "Live market snapshot")

    rows = coins[:8]
    y = 270
    for i, coin in enumerate(rows):
        if i % 2 == 0:
            _rounded(d, (80, y - 12, 1456, y + 65), radius=18, fill=PANEL)
        symbol = coin.get("symbol", "").upper()
        price = _price(coin.get("current_price"))
        ch1 = coin.get("price_change_percentage_1h_in_currency") or coin.get("price_change_percentage_1h")
        ch24 = coin.get("price_change_percentage_24h_in_currency") or coin.get("price_change_percentage_24h")
        d.text((115, y + 10), symbol, font=_font(27, True), fill=TEXT)
        d.text((360, y + 10), price, font=_font(27, True), fill=TEXT)
        c1 = UP if _safe_num(ch1) > 0 else DOWN if _safe_num(ch1) < 0 else MUTED
        c24 = UP if _safe_num(ch24) > 0 else DOWN if _safe_num(ch24) < 0 else MUTED
        d.text((760, y + 10), f"1H {_pct(ch1)}", font=_font(24, True), fill=c1)
        d.text((1080, y + 10), f"24H {_pct(ch24)}", font=_font(24, True), fill=c24)
        y += 82

    d.text((80, 925), "FAST OVERVIEW • NO SIGNALS • MARKET DATA", font=_font(19, True), fill=MUTED)
    _footer(d)
    seed = "prices|" + "|".join(
        f"{c.get('id')}:{c.get('current_price')}:{c.get('price_change_percentage_24h')}"
        for c in rows
    )
    return _save(img, output_dir, "prices", seed)


def build_pulse_card(market: dict[str, Any], signal: Optional[dict[str, Any]] = None,
                     output_dir: Path = Path("/tmp/trd_visuals")):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _header(d, "PULSE", "Market momentum")

    btc = market.get("btc") or {}
    eth = market.get("eth") or {}
    b1 = _safe_num(btc.get("price_change_percentage_1h_in_currency") or btc.get("price_change_percentage_1h"))
    e1 = _safe_num(eth.get("price_change_percentage_1h_in_currency") or eth.get("price_change_percentage_1h"))
    regime = (signal or {}).get("regime", "NEUTRAL")
    breadth = (signal or {}).get("breadth", {})
    positive = _safe_num(breadth.get("positive_pct"))
    negative = _safe_num(breadth.get("negative_pct"))

    title = {
        "BROAD_RALLY": "RECOVERY ATTEMPT",
        "BROAD_SELLOFF": "SELLING PRESSURE",
        "BTC_LED_MOVE": "BTC-LED MOVE",
        "ALTCOIN_ROTATION": "ALTCOIN ROTATION",
        "MARKET_DIVERGENCE": "DIVERGENCE",
    }.get(regime, "STABILIZING")

    d.text((80, 265), title, font=_font(52, True),
           fill=UP if "RALLY" in regime or regime == "ALTCOIN_ROTATION" else DOWN if "SELLOFF" in regime else TEXT)

    # Momentum bars.
    for label, val, yy in [("BTC", b1, 390), ("ETH", e1, 520)]:
        d.text((90, yy), label, font=_font(24, True), fill=MUTED)
        d.text((90, yy + 42), _pct(val), font=_font(46, True),
               fill=UP if val > 0 else DOWN if val < 0 else TEXT)
        center = 430
        length = max(10, min(700, abs(val) * 140))
        if val >= 0:
            d.rounded_rectangle((center, yy + 53, center + length, yy + 80), radius=13, fill=UP)
        else:
            d.rounded_rectangle((center - length, yy + 53, center, yy + 80), radius=13, fill=DOWN)

    _rounded(d, (980, 360, 1456, 720))
    d.text((1020, 405), "BREADTH", font=_font(22, True), fill=MUTED)
    d.text((1020, 465), f"{positive:.0f}% UP", font=_font(46, True), fill=UP)
    d.text((1020, 535), f"{negative:.0f}% DOWN", font=_font(46, True), fill=DOWN)
    d.text((1020, 625), "momentum > narrative", font=_font(21, True), fill=MUTED)

    d.text((80, 850), "BTC", font=_font(20, True), fill=MUTED)
    d.text((80, 890), _price(btc.get("current_price")), font=_font(31, True), fill=TEXT)
    d.text((400, 850), "ETH", font=_font(20, True), fill=MUTED)
    d.text((400, 890), _price(eth.get("current_price")), font=_font(31, True), fill=TEXT)
    _footer(d)
    seed = f"pulse|{btc.get('current_price')}|{b1}|{eth.get('current_price')}|{e1}|{regime}|{positive}|{negative}"
    return _save(img, output_dir, "pulse", seed)


async def generate_news_image(api_key: str, api_url: str, model: str,
                              post_text: str, event_key: str,
                              output_dir: Path = Path("/tmp/trd_visuals")):
    """Generate an editorial image via the Responses API image_generation tool."""
    if not api_key:
        return None

    clean = re.sub(r"https?://\S+", "", post_text)
    clean = re.sub(r"\s+", " ", clean).strip()[:2500]

    prompt = f"""
Create a premium editorial financial-media image for TRD NEWS.

Event:
{clean}

Visual brief:
- 16:9 landscape composition, 1536x1024.
- Dark institutional finance aesthetic, cinematic but realistic.
- Visually explain the event before the reader opens the caption.
- Prioritize the main market object (Bitcoin/crypto/markets) and the macro or institutional force involved.
- Use symbolic visual storytelling: price pressure, liquidity, rates, bonds, central bank, employment, dollar, equities, etc. only when relevant.
- Strong depth, restrained lighting, premium newsroom/editorial photography.
- Minimal or no text inside the image. Never invent numbers, dates, quotes, tickers, logos or headlines.
- Do not create a collage, meme, infographic, or generic crypto wallpaper.
- No watermarks.
- Keep the composition clean so Telegram text below remains the main copy.
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
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI image API {r.status_code}: {r.text[:1000]}")
        data = r.json()

    image_b64 = None
    for item in data.get("output", []):
        if item.get("type") == "image_generation_call":
            image_b64 = item.get("result")
            if image_b64:
                break

    if not image_b64:
        # Some response shapes expose the result under output content.
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_image", "image_generation_call"}:
                    image_b64 = content.get("result") or content.get("b64_json")
                    if image_b64:
                        break
            if image_b64:
                break

    if not image_b64:
        raise RuntimeError("OpenAI image generation returned no image data")

    output_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256((event_key + clean).encode("utf-8")).hexdigest()[:12]
    path = output_dir / f"news_{digest}.png"
    path.write_bytes(base64.b64decode(image_b64))
    return path
