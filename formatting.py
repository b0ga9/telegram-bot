from __future__ import annotations

import html
import re
from datetime import datetime, timezone


def num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def format_price(value) -> str:
    if value is None:
        return "—"
    value = num(value)
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.4f}"


def format_money(value) -> str:
    if value is None:
        return "—"
    value = num(value)
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    return f"${value:,.0f}"


def format_pct(value) -> str:
    if value is None:
        return "—"
    value = num(value)
    return f"{'+' if value > 0 else ''}{value:.2f}%"


def utc_time() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def telegram_html(text: str) -> str:
    """
    Minimal Markdown-like conversion used by TRD posts.
    The engine intentionally keeps formatting conservative and safe.
    """
    text = html.escape(text.strip())
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text
