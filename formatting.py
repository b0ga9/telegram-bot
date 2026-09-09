from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse


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


def escape(value: str) -> str:
    return html.escape(str(value).strip(), quote=False)


def link(url: str, label: str | None = None) -> str:
    label = label or "Источник"
    return f'<a href="{html.escape(url, quote=True)}">{escape(label)}</a>'


def domain_label(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        return host or "Источник"
    except Exception:
        return "Источник"


def quote_html(body: str) -> str:
    """Telegram HTML quote. Headings stay outside, all content stays inside."""
    return f"<blockquote>{body.strip()}</blockquote>"


def telegram_html(text: str) -> str:
    """
    Safe mini-markup converter:
      **bold**
      __underline__
      *italic*
      ~~strike~~
      `code`

    Existing Telegram HTML tags and links are intentionally not escaped.
    """
    text = text.strip()

    placeholders: list[str] = []

    def stash(match):
        placeholders.append(match.group(0))
        return f"\uFFF0{len(placeholders)-1}\uFFF1"

    text = re.sub(r"<[^>]+>", stash, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)

    for i, value in enumerate(placeholders):
        text = text.replace(f"\uFFF0{i}\uFFF1", value)

    return text


def post_html(section: str, headline: str, body_html: str, footer_html: str = "") -> str:
    """
    Unified TRD post layout:
      section label
      strong headline
      quoted body
      optional compact footer
    """
    parts = [
        f"<b>TRD / {escape(section)}</b>",
        f"<b>{escape(headline)}</b>",
        quote_html(body_html),
    ]
    if footer_html:
        parts.append(footer_html)
    return "\n\n".join(parts)
