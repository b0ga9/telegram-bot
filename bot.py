import asyncio
import hashlib
import html
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from visual_engine import (
    build_market_card,
    build_price_card,
    build_pulse_card,
    generate_news_image,
)

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-5.6-luna").strip()

ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

COINGECKO_API = "https://api.coingecko.com/api/v3"
OPENAI_API = "https://api.openai.com/v1/responses"

# TRD Visual Engine
VISUAL_ENABLED = os.getenv("TRD_VISUAL_ENABLED", "true").lower() not in {"0", "false", "no"}
VISUAL_DIR = Path(os.getenv("TRD_VISUAL_DIR", "/tmp/trd_visuals"))
IMAGE_MODEL = (os.getenv("OPENAI_IMAGE_MODEL") or "gpt-image-2").strip()

# Automatic monitoring
PRICE_CHECK_INTERVAL = 10 * 60       # 10 minutes
NEWS_CHECK_INTERVAL = 60 * 60        # 1 hour

PRICE_ALERT_COOLDOWN = 60 * 60       # 1 hour
NEWS_COOLDOWN = 30 * 60               # 30 minutes
PULSE_COOLDOWN = 60 * 60              # 1 hour

BTC_ALERT_1H = 2.0
ETH_ALERT_1H = 3.0
ALT_ALERT_1H = 5.0

MONITORED_COINS = [
    "bitcoin",
    "ethereum",
    "solana",
    "binancecoin",
    "ripple",
    "dogecoin",
    "cardano",
    "tron",
    "avalanche-2",
    "chainlink",
]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("trd-pulse")


# ============================================================
# CONFIG CHECK
# ============================================================

def check_config():
    missing = []

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHANNEL_ID:
        missing.append("TELEGRAM_CHANNEL_ID")

    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")

    if not ADMIN_USER_ID:
        missing.append("ADMIN_USER_ID")

    if missing:
        raise RuntimeError(
            "Не заданы переменные окружения: " + ", ".join(missing)
        )


# ============================================================
# ADMIN
# ============================================================

def is_admin(update: Update) -> bool:
    if not update.effective_user:
        return False

    try:
        return str(update.effective_user.id) == str(ADMIN_USER_ID)
    except Exception:
        return False


async def admin_only(update: Update) -> bool:
    if not is_admin(update):
        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ Команда доступна только администратору."
            )
        return False

    return True


# ============================================================
# HTTP
# ============================================================

async def get_json(url, params=None, timeout=30):
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, params=params)

        if response.status_code != 200:
            raise RuntimeError(
                f"HTTP {response.status_code}: {response.text[:500]}"
            )

        return response.json()


# ============================================================
# MARKET DATA
# ============================================================

async def get_market_data():
    global_data, coins = await asyncio.gather(
        get_json(f"{COINGECKO_API}/global"),
        get_json(
            f"{COINGECKO_API}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 100,
                "page": 1,
                "sparkline": "true",
                "price_change_percentage": "1h,24h,7d",
            },
        ),
    )
    add_derived_4h(coins)
    data = global_data.get("data", {})
    btc = next((coin for coin in coins if coin.get("id") == "bitcoin"), None)
    eth = next((coin for coin in coins if coin.get("id") == "ethereum"), None)

    blocked = {"usdt", "usdc", "usde", "dai", "fdusd", "usds", "usdd", "tusd"}
    candidates = [
        c for c in coins
        if str(c.get("symbol", "")).lower() not in blocked
        and (c.get("market_cap") or 0) >= 50_000_000
    ]

    def mover_score(c):
        vals = [price_change_1h(c), price_change_4h(c), price_change_24h(c)]
        return max(abs(v or 0) for v in vals)

    gainers = sorted(candidates, key=mover_score, reverse=True)
    gainers = [c for c in gainers if max(price_change_1h(c), price_change_4h(c) or 0, price_change_24h(c)) > 0][:4]
    losers = sorted(candidates, key=mover_score, reverse=True)
    losers = [c for c in losers if min(price_change_1h(c), price_change_4h(c) or 0, price_change_24h(c)) < 0][:4]

    return {
        "market_cap": data.get("total_market_cap", {}).get("usd"),
        "volume": data.get("total_volume", {}).get("usd"),
        "market_cap_change_24h": data.get("market_cap_change_percentage_24h_usd"),
        "btc_dominance": data.get("market_cap_percentage", {}).get("btc"),
        "active_cryptocurrencies": data.get("active_cryptocurrencies"),
        "btc": btc, "eth": eth, "coins": coins,
        "gainers": gainers, "losers": losers,
    }


def price_change_1h(coin):
    return (
        coin.get("price_change_percentage_1h_in_currency")
        or coin.get("price_change_percentage_1h")
        or 0
    )


def price_change_24h(coin):
    return (
        coin.get("price_change_percentage_24h_in_currency")
        or coin.get("price_change_percentage_24h")
        or 0
    )


def price_change_4h(coin):
    """Estimate 4h change from CoinGecko's hourly 7d sparkline."""
    direct = coin.get("price_change_percentage_4h_in_currency")
    if direct is not None:
        return direct
    prices = ((coin.get("sparkline_in_7d") or {}).get("price") or [])
    if len(prices) >= 5:
        try:
            old = float(prices[-5])
            current = float(prices[-1])
            if old:
                return (current - old) / old * 100
        except (TypeError, ValueError):
            pass
    return None


def add_derived_4h(coins):
    for coin in coins or []:
        value = price_change_4h(coin)
        if value is not None:
            coin["price_change_percentage_4h_in_currency"] = value
    return coins


def format_money(value):
    if value is None:
        return "—"

    if value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"

    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"

    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"

    return f"${value:,.0f}"


def format_price(value):
    if value is None:
        return "—"

    if value >= 1000:
        return f"${value:,.0f}"

    if value >= 1:
        return f"${value:,.2f}"

    return f"${value:.4f}"


def format_pct(value):
    if value is None:
        return "—"

    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def _best_mover_period(coin):
    values = {
        "1ч": price_change_1h(coin),
        "4ч": price_change_4h(coin),
        "24ч": price_change_24h(coin),
    }
    values = {k: v for k, v in values.items() if v is not None}
    return max(values.items(), key=lambda item: abs(item[1])) if values else ("24ч", 0)


def format_market_data(data):
    btc = data["btc"] or {}
    eth = data["eth"] or {}
    lines = [
        "🔴 <b>TRD MARKET</b>",
        "",
        "<b>Состояние рынка</b>",
        "",
        f"Капитализация: <b>{format_money(data['market_cap'])}</b>",
        f"Изменение за 24ч: <b>{format_pct(data['market_cap_change_24h'])}</b>",
        f"BTC: <b>{format_price(btc.get('current_price'))}</b> · {format_pct(price_change_24h(btc))}",
        f"ETH: <b>{format_price(eth.get('current_price'))}</b> · {format_pct(price_change_24h(eth))}",
        "",
        "<b>Сильные движения</b>",
    ]
    for coin in data.get("gainers", [])[:4]:
        p, v = _best_mover_period(coin)
        lines.append(f"🟢 {coin.get('symbol','').upper()} · {format_pct(v)} · {p}")
    for coin in data.get("losers", [])[:4]:
        p, v = _best_mover_period(coin)
        lines.append(f"🔴 {coin.get('symbol','').upper()} · {format_pct(v)} · {p}")
    return "\n".join(lines)


# ============================================================
# PRICES
# ============================================================

async def get_monitored_prices():
    coins = await get_json(
        f"{COINGECKO_API}/coins/markets",
        params={
            "vs_currency": "usd",
            "ids": ",".join(MONITORED_COINS),
            "order": "market_cap_desc",
            "per_page": len(MONITORED_COINS),
            "page": 1,
            "sparkline": "true",
            "price_change_percentage": "1h,24h,7d",
        },
    )
    return add_derived_4h(coins)


def format_prices(coins):
    lines = ["💰 <b>TRD PRICES</b>", ""]
    for coin in coins:
        symbol = coin.get("symbol", "").upper()
        lines.append(f"<b>{symbol}</b> · {format_price(coin.get('current_price'))}")
        lines.append(f"1ч {format_pct(price_change_1h(coin))} · 4ч {format_pct(price_change_4h(coin))} · 24ч {format_pct(price_change_24h(coin))}")
        lines.append("")
    return "\n".join(lines).strip()


async def prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    try:
        coins = await get_monitored_prices()
        image_path = make_price_visual(coins)
        caption = (
            "💰 <b>TRD PRICES</b>\n\n"
            "<blockquote>Короткий обзор цен и динамики отслеживаемых монет. "
            "Изменение показано за 1, 4 и 24 часа.</blockquote>\n\n"
            "<b>Мнение TRD:</b> следим не только за BTC и ETH — резкие движения альткоинов часто показывают, где сейчас концентрируется активность."
        )
        publish_caption = caption if image_path else format_prices(coins)
        context.user_data["publish_text"] = publish_caption
        context.user_data["publish_photo_path"] = str(image_path) if image_path else None
        await send_visual_post(context.bot, update.effective_chat.id, image_path, publish_caption)
    except Exception as e:
        logger.exception("Prices error")
        await update.message.reply_text(f"❌ Ошибка получения цен:\n{e}")


# ============================================================
# OPENAI
# ============================================================

async def openai_response(
    prompt,
    web_search=False,
    max_output_tokens=700,
    retries=0,
):
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    if not OPENAI_MODEL:
        raise RuntimeError("OPENAI_MODEL пуст. Используется модель по умолчанию gpt-5.6-luna.")

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "reasoning": {
            "effort": "low"
        },
        "max_output_tokens": max_output_tokens,
    }

    if web_search:
        payload["tools"] = [
            {
                "type": "web_search"
            }
        ]

    last_error = None

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(
                    OPENAI_API,
                    headers=headers,
                    json=payload,
                )

            if response.status_code == 429:
                data = response.json()

                message = (
                    data.get("error", {}).get("message")
                    or "Rate limit"
                )

                raise RuntimeError(
                    f"OpenAI API error 429: {message}"
                )

            if response.status_code >= 400:
                raise RuntimeError(
                    f"OpenAI API error {response.status_code}: "
                    f"{response.text[:1000]}"
                )

            data = response.json()

            text = data.get("output_text")

            if text:
                return text.strip()

            # Fallback for Responses API structures
            output = data.get("output", [])

            parts = []

            for item in output:
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        parts.append(
                            content.get("text", "")
                        )

            result = "\n".join(parts).strip()

            if result:
                return result

            raise RuntimeError(
                "OpenAI не вернул текстовый ответ."
            )

        except Exception as e:
            last_error = e

            if attempt < retries:
                await asyncio.sleep(3)

    raise last_error


# ============================================================
# TELEGRAM MARKDOWN → HTML
# ============================================================

def markdown_to_telegram_html(text):
    if not text:
        return ""

    placeholders = {}

    def save(value):
        key = f"___PLACEHOLDER_{len(placeholders)}___"
        placeholders[key] = value
        return key

    # Markdown links
    def link_repl(match):
        label = html.escape(match.group(1))
        url = html.escape(match.group(2), quote=True)

        return save(
            f'<a href="{url}">{label}</a>'
        )

    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        link_repl,
        text,
    )

    # Code
    def code_repl(match):
        return save(
            f"<code>{html.escape(match.group(1))}</code>"
        )

    text = re.sub(
        r"`([^`]+)`",
        code_repl,
        text,
    )

    text = html.escape(text)

    # Bold italic
    text = re.sub(
        r"\*\*\*(.+?)\*\*\*",
        r"<b><i>\1</i></b>",
        text,
        flags=re.DOTALL,
    )

    # Bold
    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL,
    )

    # Italic
    text = re.sub(
        r"(?<!\*)\*([^*\n]+?)\*(?!\*)",
        r"<i>\1</i>",
        text,
    )

    # Quote blocks
    lines = text.splitlines()
    result = []

    quote_buffer = []

    def flush_quote():
        nonlocal quote_buffer

        if quote_buffer:
            content = "<br>".join(
                quote_buffer
            )

            result.append(
                f"<blockquote>{content}</blockquote>"
            )

            quote_buffer = []

    for line in lines:
        if line.startswith("&gt; "):
            quote_buffer.append(
                line[5:]
            )
        else:
            flush_quote()
            result.append(line)

    flush_quote()

    text = "\n".join(result)

    for key, value in placeholders.items():
        text = text.replace(
            html.escape(key),
            value,
        )

    return text.strip()


# ============================================================
# TELEGRAM SENDING
# ============================================================

async def send_long_message(
    bot,
    chat_id,
    text,
    disable_web_page_preview=True,
):
    html_text = text if re.search(r"<(?:b|i|blockquote|a|code|u|s)(?:\s|>)", text, re.I) else markdown_to_telegram_html(text)

    max_length = 3900

    chunks = []

    while len(html_text) > max_length:
        cut = html_text.rfind("\n", 0, max_length)

        if cut < 1000:
            cut = max_length

        chunks.append(html_text[:cut])
        html_text = html_text[cut:].lstrip()

    if html_text:
        chunks.append(html_text)

    for chunk in chunks:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=disable_web_page_preview,
            )
        except Exception:
            logger.exception(
                "Telegram HTML send failed, fallback"
            )

            await bot.send_message(
                chat_id=chat_id,
                text=re.sub(
                    r"<[^>]+>",
                    "",
                    chunk,
                ),
            )



# ============================================================
# TRD VISUAL ENGINE INTEGRATION
# ============================================================

async def make_news_visual(post, key="news", importance=0):
    """Generate an editorial image for a NEWS post."""
    if not VISUAL_ENABLED or importance < 9:
        return None
    try:
        VISUAL_DIR.mkdir(parents=True, exist_ok=True)
        return await generate_news_image(
            api_key=OPENAI_API_KEY,
            api_url=OPENAI_API,
            model=IMAGE_MODEL,
            post_text=post,
            event_key=key,
            output_dir=VISUAL_DIR,
        )
    except Exception:
        logger.exception("NEWS visual generation failed")
        return None


def make_market_visual(market, signal=None):
    """Render a deterministic MARKET data card."""
    if not VISUAL_ENABLED:
        return None
    try:
        VISUAL_DIR.mkdir(parents=True, exist_ok=True)
        return build_market_card(
            market,
            signal=signal,
            output_dir=VISUAL_DIR,
        )
    except Exception:
        logger.exception("MARKET visual generation failed")
        return None


def make_price_visual(coins):
    """Render a deterministic PRICES card."""
    if not VISUAL_ENABLED:
        return None
    try:
        VISUAL_DIR.mkdir(parents=True, exist_ok=True)
        return build_price_card(coins, output_dir=VISUAL_DIR)
    except Exception:
        logger.exception("PRICES visual generation failed")
        return None


def make_pulse_visual(market, signal=None):
    """Render a deterministic dynamic PULSE card."""
    if not VISUAL_ENABLED:
        return None
    try:
        VISUAL_DIR.mkdir(parents=True, exist_ok=True)
        return build_pulse_card(
            market,
            signal=signal,
            output_dir=VISUAL_DIR,
        )
    except Exception:
        logger.exception("PULSE visual generation failed")
        return None


async def send_visual_post(bot, chat_id, image_path, text):
    """Send image + TRD caption; fall back to text if visual creation failed."""
    if image_path:
        caption = text if re.search(r"<(?:b|i|blockquote|a|code|u|s)(?:\s|>)", text, re.I) else markdown_to_telegram_html(text)
        # Telegram photo captions are shorter than normal messages.
        if len(caption) <= 1024:
            await bot.send_photo(
                chat_id=chat_id,
                photo=image_path,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            return

        await bot.send_photo(
            chat_id=chat_id,
            photo=image_path,
        )
        await send_long_message(
            bot,
            chat_id,
            text,
        )
        return

    await send_long_message(bot, chat_id, text)


# ============================================================
# NEWS
# ============================================================

NEWS_PROMPT = """
Ты — редактор Telegram-канала TRD Pulse.

Найди за последние 24 часа действительно важное событие,
которое может заметно повлиять на мировые рынки, Bitcoin,
Ethereum или риск-активы.

Приоритет:
1. ФРС / ЕЦБ / ставки
2. инфляция / занятость
3. облигации / доходности / доллар
4. акции США
5. Bitcoin / Ethereum / крупные криптособытия
6. нефть / золото
7. крупные геополитические события с рыночным эффектом

Не публикуй:
- мелкие новости;
- повтор уже известного события;
- слухи без подтверждения;
- обычные движения цены;
- рекламные материалы.

Несколько статей об одном событии объедини в ОДНУ новость.

ВАЖНО:
символ ">" используется НЕ как настоящая цитата.
Это просто визуальный блок с главной мыслью.

Верни строго в формате:

IMPORTANCE: число от 0 до 10
KEY: короткий уникальный ключ события

POST:
📰 **ЗАГОЛОВОК**

> ⚡ **Главное:** одна короткая мысль о событии.

2–3 коротких предложения с фактами.

**Почему важно**
Одно короткое предложение о влиянии на рынок.

🔗 [Источник](URL)

Если нет действительно важной новой новости:
IMPORTANCE: 0
KEY: none
POST:
NO_NEWS

Не выдумывай источники и факты.
Не давай торговых рекомендаций.
Пиши на русском.
Общий объём поста — примерно 100–150 слов.
"""


async def get_news_analysis():
    return await openai_response(
        NEWS_PROMPT,
        web_search=True,
        max_output_tokens=700,
        retries=0,
    )


def parse_news_result(text):
    importance_match = re.search(
        r"IMPORTANCE:\s*(\d+)",
        text,
        re.IGNORECASE,
    )

    key_match = re.search(
        r"KEY:\s*(.+)",
        text,
        re.IGNORECASE,
    )

    post_match = re.search(
        r"POST:\s*(.*)",
        text,
        re.IGNORECASE | re.DOTALL,
    )

    importance = (
        int(importance_match.group(1))
        if importance_match
        else 0
    )

    key = (
        key_match.group(1).strip()
        if key_match
        else "unknown"
    )

    post = (
        post_match.group(1).strip()
        if post_match
        else text.strip()
    )

    return importance, key, post


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    try:
        result = await get_news_analysis()

        importance, key, post = parse_news_result(
            result
        )

        if post == "NO_NEWS":
            await update.message.reply_text(
                "📰 Новостей, достаточно важных для TRD Pulse, сейчас нет."
            )
            return

        context.user_data["news_text"] = post
        context.user_data["publish_text"] = post
        image_path = await make_news_visual(post, key=key, importance=importance)
        context.user_data["publish_photo_path"] = str(image_path) if image_path else None
        await send_visual_post(
            context.bot,
            update.effective_chat.id,
            image_path,
            post,
        )

    except Exception as e:
        logger.exception("News error")

        if "429" in str(e):
            await update.message.reply_text(
                "⚠️ OpenAI временно ограничил запросы.\n\n"
                "Автоматический мониторинг цен при этом продолжает работать."
            )
        else:
            await update.message.reply_text(
                f"❌ Ошибка поиска новостей:\n{e}"
            )


# ============================================================
# PULSE
# ============================================================

def build_pulse_prompt(market, recent_news=""):
    btc = market["btc"] or {}
    eth = market["eth"] or {}
    change = market.get("market_cap_change_24h") or 0
    mood = "🟢" if change > 1 else "🔴" if change < -1 else "🟡"
    return f"""
Ты — редактор TRD. Создай короткий профессиональный PULSE на русском.

Данные:
BTC: {format_price(btc.get('current_price'))}; 1ч {format_pct(price_change_1h(btc))}; 24ч {format_pct(price_change_24h(btc))}
ETH: {format_price(eth.get('current_price'))}; 1ч {format_pct(price_change_1h(eth))}; 24ч {format_pct(price_change_24h(eth))}
Капитализация: {format_money(market.get('market_cap'))}; 24ч {format_pct(change)}
Контекст новости: {recent_news[:2500]}

Настроение: {mood}

ФОРМАТ:
{mood} <b>TRD PULSE</b>

<b>Короткий заголовок</b>

<blockquote>2–3 предложения о том, что происходит и почему.</blockquote>

<b>BTC</b> · цена · 1ч · 24ч
<b>ETH</b> · цена · 1ч · 24ч

<b>Мнение TRD:</b> 1–2 предложения анализа.

Только одна цитата. Без «Главное», «Что важно» и лишних эмодзи. 90–140 слов.
"""


async def generate_pulse_from_market(market, recent_news=""):
    prompt = build_pulse_prompt(market, recent_news)
    return await openai_response(
        prompt,
        web_search=False,
        max_output_tokens=700,
        retries=0,
    )


async def generate_pulse(recent_news=""):
    return await generate_pulse_from_market(
        await get_market_data(),
        recent_news,
    )


async def pulse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    try:
        market = await get_market_data()
        news = get_state(context.application).get("last_news_text", "")

        pulse = await generate_pulse_from_market(market, news)

        context.user_data["pulse_text"] = pulse
        context.user_data["publish_text"] = pulse

        image_path = make_pulse_visual(market=market)
        context.user_data["publish_photo_path"] = str(image_path) if image_path else None
        await send_visual_post(
            context.bot,
            update.effective_chat.id,
            image_path,
            pulse,
        )

    except Exception as e:
        logger.exception("Pulse error")

        if "429" in str(e):
            await update.message.reply_text(
                "⚠️ OpenAI временно ограничил запросы."
            )
        else:
            await update.message.reply_text(
                f"❌ Ошибка Pulse:\n{e}"
            )


# ============================================================
# AUTOMATIC MONITORING
# ============================================================


# Сколько измерений цены хранить для каждой монеты.
# При проверке раз в 5 минут это примерно 10 часов истории.
PRICE_HISTORY_LIMIT = 120


def get_state(application):
    state = application.bot_data

    state.setdefault("price_history", {})
    state.setdefault("last_prices", {})
    state.setdefault("last_price_alert", {})

    state.setdefault("last_market_alert_at", 0)
    state.setdefault("last_auto_pulse_at", 0)

    state.setdefault("last_news_key", None)
    state.setdefault("last_news_hash", None)
    state.setdefault("last_auto_news_at", 0)
    state.setdefault("last_news_text", "")

    state.setdefault(
        "monitor_started_at",
        datetime.now(timezone.utc),
    )

    return state


def update_price_history(coins, state):
    """Сохраняет текущие цены в историю."""
    now = asyncio.get_running_loop().time()

    for coin in coins:
        coin_id = coin.get("id")
        price = coin.get("current_price")

        if not coin_id or price is None:
            continue

        history = state["price_history"].setdefault(
            coin_id,
            [],
        )

        history.append(
            {
                "time": now,
                "price": price,
            }
        )

        if len(history) > PRICE_HISTORY_LIMIT:
            del history[:-PRICE_HISTORY_LIMIT]

        # Сохраняем также последнее состояние для совместимости.
        state["last_prices"][coin_id] = {
            "price": price,
            "1h": price_change_1h(coin),
            "24h": price_change_24h(coin),
        }


def get_price_change_from_history(history, seconds):
    """Изменение цены относительно ближайшей точки в прошлом."""
    if len(history) < 2:
        return None

    now = history[-1]["time"]
    current_price = history[-1]["price"]

    if not current_price:
        return None

    target_time = now - seconds

    candidates = [
        point
        for point in history
        if point["time"] <= target_time
    ]

    if not candidates:
        return None

    old_price = candidates[-1].get("price")

    if not old_price:
        return None

    return (
        (current_price - old_price)
        / old_price
        * 100
    )


def get_coin_history_metrics(coin, state):
    """Возвращает изменения за 5m, 15m, 30m, 1h и 24h."""
    if not coin:
        return {
            "5m": None,
            "15m": None,
            "30m": None,
            "1h": None,
            "24h": 0,
        }

    coin_id = coin.get("id")

    history = state["price_history"].get(
        coin_id,
        [],
    )

    metrics = {
        "5m": get_price_change_from_history(
            history,
            5 * 60,
        ),
        "15m": get_price_change_from_history(
            history,
            15 * 60,
        ),
        "30m": get_price_change_from_history(
            history,
            30 * 60,
        ),
        "1h": get_price_change_from_history(
            history,
            60 * 60,
        ),
        "24h": price_change_24h(coin),
    }

    # До накопления собственной часовой истории
    # используем значение CoinGecko.
    if metrics["1h"] is None:
        metrics["1h"] = price_change_1h(coin)

    return metrics


def calculate_acceleration(metrics):
    """
    Сравнивает скорость последних 5 минут
    со средней скоростью движения за 1 час.

    > 1.5 — заметное ускорение
    > 2.0 — сильное ускорение
    """
    change_5m = metrics.get("5m")
    change_1h = metrics.get("1h")

    if (
        change_5m is None
        or change_1h is None
        or abs(change_1h) < 0.01
    ):
        return 0

    expected_speed = abs(change_1h) / 12

    if expected_speed <= 0:
        return 0

    return abs(change_5m) / expected_speed


def calculate_market_breadth(coins, state):
    """Анализирует, насколько широко рынок движется вверх или вниз."""
    positive = 0
    negative = 0
    neutral = 0
    changes = []

    for coin in coins:
        metrics = get_coin_history_metrics(
            coin,
            state,
        )

        change = metrics.get("1h")

        if change is None:
            continue

        changes.append(change)

        if change > 0.15:
            positive += 1
        elif change < -0.15:
            negative += 1
        else:
            neutral += 1

    total = positive + negative + neutral

    if total == 0:
        return {
            "positive": 0,
            "negative": 0,
            "neutral": 0,
            "positive_pct": 0,
            "negative_pct": 0,
            "average_change": 0,
        }

    average_change = (
        sum(changes) / len(changes)
        if changes
        else 0
    )

    return {
        "positive": positive,
        "negative": negative,
        "neutral": neutral,
        "positive_pct": positive / total * 100,
        "negative_pct": negative / total * 100,
        "average_change": average_change,
    }


def find_coin(coins, coin_id):
    return next(
        (
            coin
            for coin in coins
            if coin.get("id") == coin_id
        ),
        None,
    )


def classify_market_regime(
    btc_metrics,
    eth_metrics,
    breadth,
    market,
):
    """Определяет текущий тип движения рынка."""
    btc_change = btc_metrics.get("1h") or 0
    eth_change = eth_metrics.get("1h") or 0

    positive_pct = breadth["positive_pct"]
    negative_pct = breadth["negative_pct"]

    if (
        btc_change > 0.8
        and eth_change > 0.8
        and positive_pct >= 70
    ):
        return "BROAD_RALLY"

    if (
        btc_change < -0.8
        and eth_change < -0.8
        and negative_pct >= 70
    ):
        return "BROAD_SELLOFF"

    if (
        abs(btc_change) >= 1.5
        and positive_pct < 70
        and negative_pct < 70
    ):
        return "BTC_LED_MOVE"

    if (
        abs(btc_change) < 0.8
        and eth_change > 1
        and positive_pct >= 70
    ):
        return "ALTCOIN_ROTATION"

    if (
        btc_change > 0.5
        and eth_change < -0.5
    ) or (
        btc_change < -0.5
        and eth_change > 0.5
    ):
        return "MARKET_DIVERGENCE"

    return "NEUTRAL"


def calculate_market_score(
    btc_metrics,
    eth_metrics,
    breadth,
    market,
):
    """
    Score:
    0-3  — шум
    4-6  — наблюдение
    7-9  — Market Alert
    10+  — сильное событие + Pulse
    """
    score = 0
    reasons = []

    btc_change = btc_metrics.get("1h") or 0
    eth_change = eth_metrics.get("1h") or 0

    btc_acceleration = calculate_acceleration(
        btc_metrics
    )

    eth_acceleration = calculate_acceleration(
        eth_metrics
    )

    # BTC
    if abs(btc_change) >= 1:
        score += 2
        reasons.append(
            f"BTC 1h {format_pct(btc_change)}"
        )

    if abs(btc_change) >= 2:
        score += 2

    # ETH
    if abs(eth_change) >= 1.5:
        score += 2
        reasons.append(
            f"ETH 1h {format_pct(eth_change)}"
        )

    # BTC + ETH подтверждают направление
    same_direction = (
        btc_change > 0
        and eth_change > 0
    ) or (
        btc_change < 0
        and eth_change < 0
    )

    if (
        same_direction
        and abs(btc_change) >= 0.5
        and abs(eth_change) >= 0.5
    ):
        score += 2
        reasons.append(
            "BTC и ETH подтверждают движение"
        )

    # Ширина рынка
    if breadth["positive_pct"] >= 70:
        score += 3
        reasons.append(
            f"{breadth['positive_pct']:.0f}% монет растут"
        )

    if breadth["negative_pct"] >= 70:
        score += 3
        reasons.append(
            f"{breadth['negative_pct']:.0f}% монет снижаются"
        )

    # Среднее изменение рынка
    average_change = breadth["average_change"]

    if abs(average_change) >= 1:
        score += 1

    if abs(average_change) >= 2:
        score += 1

    # Ускорение BTC
    if btc_acceleration >= 1.5:
        score += 2
        reasons.append(
            "BTC ускоряет движение"
        )

    # Ускорение ETH
    if eth_acceleration >= 1.5:
        score += 1

    # Общий Market Cap
    market_cap_change = (
        market.get(
            "market_cap_change_24h"
        )
        or 0
    )

    if abs(market_cap_change) >= 2:
        score += 1

    if abs(market_cap_change) >= 4:
        score += 1

    return {
        "score": score,
        "reasons": reasons,
        "btc_acceleration": btc_acceleration,
        "eth_acceleration": eth_acceleration,
    }


def analyze_market_signal(
    coins,
    market,
    state,
):
    """Главный анализатор текущего состояния рынка."""
    btc = find_coin(
        coins,
        "bitcoin",
    )

    eth = find_coin(
        coins,
        "ethereum",
    )

    btc_metrics = get_coin_history_metrics(
        btc,
        state,
    )

    eth_metrics = get_coin_history_metrics(
        eth,
        state,
    )

    breadth = calculate_market_breadth(
        coins,
        state,
    )

    score_data = calculate_market_score(
        btc_metrics,
        eth_metrics,
        breadth,
        market,
    )

    regime = classify_market_regime(
        btc_metrics,
        eth_metrics,
        breadth,
        market,
    )

    triggers = []

    for coin in coins:
        metrics = get_coin_history_metrics(
            coin,
            state,
        )

        change = metrics.get("1h") or 0
        coin_id = coin.get("id")

        if coin_id == "bitcoin":
            threshold = BTC_ALERT_1H
        elif coin_id == "ethereum":
            threshold = ETH_ALERT_1H
        else:
            threshold = ALT_ALERT_1H

        if abs(change) >= threshold:
            triggers.append(
                {
                    "id": coin_id,
                    "symbol": coin.get(
                        "symbol",
                        "",
                    ).upper(),
                    "name": coin.get(
                        "name",
                        "",
                    ),
                    "price": coin.get(
                        "current_price"
                    ),
                    "1h": change,
                    "24h": metrics.get(
                        "24h"
                    ),
                    "metrics": metrics,
                }
            )

    triggers = sorted(
        triggers,
        key=lambda x: abs(x["1h"]),
        reverse=True,
    )

    return {
        "score": score_data["score"],
        "reasons": score_data["reasons"],
        "btc_acceleration": (
            score_data["btc_acceleration"]
        ),
        "eth_acceleration": (
            score_data["eth_acceleration"]
        ),
        "regime": regime,
        "triggers": triggers,
        "breadth": breadth,
        "btc_metrics": btc_metrics,
        "eth_metrics": eth_metrics,
    }


def build_market_alert(signal):
    """Формирует текст автоматического рыночного алерта."""
    score = signal["score"]
    regime = signal["regime"]

    btc = signal["btc_metrics"]
    eth = signal["eth_metrics"]
    breadth = signal["breadth"]

    if regime == "BROAD_RALLY":
        headline = (
            "🚀 Широкое ускорение рынка вверх."
        )
    elif regime == "BROAD_SELLOFF":
        headline = (
            "🔻 Усилилось широкое снижение рынка."
        )
    elif regime == "BTC_LED_MOVE":
        headline = (
            "₿ BTC стал главным драйвером движения."
        )
    elif regime == "ALTCOIN_ROTATION":
        headline = (
            "🔥 Усилилась активность в альткоинах."
        )
    elif regime == "MARKET_DIVERGENCE":
        headline = (
            "⚠️ На рынке появилось заметное расхождение."
        )
    else:
        strongest = (
            signal["triggers"][0]
            if signal["triggers"]
            else None
        )

        if strongest and strongest["1h"] > 0:
            headline = (
                "📈 Рынок показывает заметное ускорение."
            )
        else:
            headline = (
                "📉 На рынке усилилось давление."
            )

    lines = [
        "🚨 <b>TRD MARKET ALERT</b>",
        "",
        headline,
        "",
        f"⚡ Market Strength: <b>{score}</b>",
        "",
    ]

    btc_change = btc.get("1h")

    if btc_change is not None:
        lines.append(
            "₿ <b>BTC</b> "
            f"{format_pct(btc_change)} за 1ч"
        )

    eth_change = eth.get("1h")

    if eth_change is not None:
        lines.append(
            "Ξ <b>ETH</b> "
            f"{format_pct(eth_change)} за 1ч"
        )

    lines.append("")

    lines.append(
        "<b>🌐 Market breadth</b>"
    )

    lines.append(
        f"🟢 Рост: {breadth['positive_pct']:.0f}%"
    )

    lines.append(
        f"🔴 Падение: {breadth['negative_pct']:.0f}%"
    )

    if signal["btc_acceleration"] >= 1.5:
        lines.append("")
        lines.append(
            "⚡ BTC ускоряет движение."
        )

    if signal["triggers"]:
        lines.append("")
        lines.append(
            "<b>Наиболее сильные движения</b>"
        )

        for item in signal["triggers"][:3]:
            emoji = (
                "🟢"
                if item["1h"] > 0
                else "🔴"
            )

            lines.append(
                f"{emoji} "
                f"<b>{item['symbol']}</b> "
                f"{format_price(item['price'])} "
                f"<b>{format_pct(item['1h'])}</b>"
            )

    return "\n".join(lines)


def should_publish_market_alert(signal):
    """Решает, публиковать ли Market Alert."""
    if signal["score"] >= 7:
        return True

    for item in signal["triggers"]:
        if (
            item["id"] == "bitcoin"
            and abs(item["1h"]) >= BTC_ALERT_1H
        ):
            return True

    breadth = signal["breadth"]

    if (
        breadth["positive_pct"] >= 80
        or breadth["negative_pct"] >= 80
    ):
        return True

    return False


def should_trigger_pulse(signal):
    """Решает, нужен ли AI Pulse."""
    if signal["score"] >= 10:
        return True

    if signal["regime"] in (
        "BROAD_RALLY",
        "BROAD_SELLOFF",
        "MARKET_DIVERGENCE",
    ):
        return True

    if signal["btc_acceleration"] >= 2:
        return True

    return False


async def automatic_price_check(application):
    """Главная автоматическая проверка рынка."""
    state = get_state(application)

    try:
        # 1. Получаем цены.
        coins = await get_monitored_prices()

        # 2. Сохраняем историю.
        update_price_history(
            coins,
            state,
        )

        # 3. Получаем общую информацию о рынке.
        market = await get_market_data()

        # 4. Анализируем рынок.
        signal = analyze_market_signal(
            coins,
            market,
            state,
        )

        logger.info(
            "Market analysis | "
            "score=%s regime=%s "
            "breadth_up=%.0f%% "
            "breadth_down=%.0f%% "
            "btc_1h=%s eth_1h=%s",
            signal["score"],
            signal["regime"],
            signal["breadth"][
                "positive_pct"
            ],
            signal["breadth"][
                "negative_pct"
            ],
            format_pct(
                signal["btc_metrics"].get("1h")
            ),
            format_pct(
                signal["eth_metrics"].get("1h")
            ),
        )

        # 5. Проверяем необходимость Alert.
        if not should_publish_market_alert(
            signal
        ):
            return

        now = asyncio.get_running_loop().time()

        last_alert = state[
            "last_market_alert_at"
        ]

        # Общий cooldown на автоматические алерты.
        if (
            now - last_alert
            < PRICE_ALERT_COOLDOWN
        ):
            logger.info(
                "Market alert skipped: cooldown"
            )
            return

        # 6. Публикуем Alert.
        alert = build_market_alert(
            signal
        )

        image_path = make_market_visual(market, signal=signal)
        if image_path:
            await application.bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=image_path,
                caption=alert[:1024],
                parse_mode=ParseMode.HTML,
            )
        else:
            await application.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=alert,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        state[
            "last_market_alert_at"
        ] = now

        # 7. При сильном событии запускаем Pulse.
        if should_trigger_pulse(signal):
            last_pulse = state[
                "last_auto_pulse_at"
            ]

            if (
                now - last_pulse
                >= PULSE_COOLDOWN
            ):
                await automatic_pulse(
                    application,
                    coins,
                    signal,
                    market=market,
                )

                state[
                    "last_auto_pulse_at"
                ] = now

        logger.info(
            "Automatic market alert published"
        )

    except Exception:
        logger.exception(
            "Automatic price monitoring failed"
        )


async def automatic_pulse(
    application,
    coins,
    signal=None,
    market=None,
):
    """
    Генерирует AI Pulse.

    signal передаётся в prompt, чтобы AI понимал,
    какое событие вызвало публикацию.
    """
    try:
        if market is None:
            market = await get_market_data()

        recent_news = get_state(
            application
        ).get(
            "last_news_text",
            "",
        )

        signal_context = ""

        if signal:
            breadth = signal["breadth"]

            signal_context = (
                "\n\nАвтоматический сигнал рынка:\n"
                f"Режим: {signal['regime']}\n"
                f"Market Strength Score: {signal['score']}\n"
                f"Рост рынка: "
                f"{breadth['positive_pct']:.0f}%\n"
                f"Падение рынка: "
                f"{breadth['negative_pct']:.0f}%\n"
                f"Ускорение BTC: "
                f"{signal['btc_acceleration']:.2f}x\n"
            )

        prompt = build_pulse_prompt(
            market,
            recent_news + signal_context,
        )

        result = await openai_response(
            prompt,
            web_search=False,
            max_output_tokens=600,
            retries=0,
        )

        image_path = make_pulse_visual(market, signal=signal)
        await send_visual_post(
            application.bot,
            TELEGRAM_CHANNEL_ID,
            image_path,
            result,
        )

        logger.info(
            "Automatic PULSE published"
        )

    except Exception as e:
        logger.error(
            "Automatic PULSE failed: %s",
            e,
        )


async def automatic_news_check(application):
    state = get_state(application)

    now = asyncio.get_running_loop().time()

    if (
        now - state["last_auto_news_at"]
        < NEWS_COOLDOWN
    ):
        return

    try:
        result = await get_news_analysis()

        state[
            "last_auto_news_at"
        ] = now

        importance, key, post = (
            parse_news_result(result)
        )

        logger.info(
            "Automatic news: "
            "importance=%s key=%s",
            importance,
            key,
        )

        if (
            importance < 8
            or key.lower() == "none"
            or post == "NO_NEWS"
        ):
            return

        fingerprint = hashlib.sha256(
            post[:1000]
            .lower()
            .encode("utf-8")
        ).hexdigest()

        if (
            fingerprint
            == state["last_news_hash"]
        ):
            logger.info(
                "Automatic news skipped: duplicate"
            )
            return

        if key == state["last_news_key"]:
            logger.info(
                "Automatic news skipped: same key"
            )
            return

        image_path = await make_news_visual(post, key=key, importance=importance)
        await send_visual_post(
            application.bot,
            TELEGRAM_CHANNEL_ID,
            image_path,
            post,
        )

        state[
            "last_news_key"
        ] = key

        state[
            "last_news_hash"
        ] = fingerprint

        state[
            "last_news_text"
        ] = post

        logger.info(
            "Automatic NEWS published"
        )

    except Exception as e:
        logger.error(
            "Automatic news failed: %s",
            e,
        )


async def monitor_loop(application):
    logger.info(
        "TRD automatic monitoring started"
    )

    # Initial delay so bot can start normally.
    await asyncio.sleep(20)

    last_price_check = 0
    last_news_check = 0

    while True:
        try:
            now = asyncio.get_running_loop().time()

            if (
                now - last_price_check
                >= PRICE_CHECK_INTERVAL
            ):
                await automatic_price_check(
                    application
                )

                last_price_check = now

            if (
                now - last_news_check
                >= NEWS_CHECK_INTERVAL
            ):
                await automatic_news_check(
                    application
                )

                last_news_check = now

        except asyncio.CancelledError:
            logger.info(
                "TRD automatic monitoring stopped"
            )
            raise

        except Exception:
            logger.exception(
                "Monitor loop error"
            )

        await asyncio.sleep(30)




# ============================================================
# AUTO STATUS
# ============================================================

async def autostatus_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    state = get_state(context.application)

    started = state.get(
        "monitor_started_at"
    )

    if isinstance(started, datetime):
        started_text = started.strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    else:
        started_text = "—"

    text = (
        "🤖 <b>TRD AUTO STATUS</b>\n\n"
        "🟢 Автомониторинг: <b>ON</b>\n"
        f"📊 Проверка цен: каждые <b>{PRICE_CHECK_INTERVAL // 60} мин</b>\n"
        f"📰 Новости: каждые <b>{NEWS_CHECK_INTERVAL // 60} мин</b>\n"
        f"⚡ Pulse cooldown: <b>{PULSE_COOLDOWN // 60} мин</b>\n"
        f"🚨 Price alert cooldown: <b>{PRICE_ALERT_COOLDOWN // 60} мин</b>\n"
        f"⏱ Запущен: <b>{started_text}</b>\n\n"
        "AI вызывается только для NEWS/PULSE."
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# PUBLISH EDITOR
# ============================================================

async def publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open the publication editor using the latest generated TRD post."""
    if not await admin_only(update):
        return

    # Prefer an explicitly prepared publication, then the latest PULSE/NEWS.
    text = (
        context.user_data.get("publish_text")
        or context.user_data.get("pulse_text")
        or context.user_data.get("news_text")
        or ""
    ).strip()

    if not text:
        await update.message.reply_text(
            "📝 Нет подготовленного поста. Сначала используй /pulse или /news."
        )
        return

    context.user_data["publish_text"] = text
    context.user_data["publish_waiting"] = None

    # Generated image is already attached to the publication state.
    # Do not ask the user to choose 'with image / without image' again.
    await send_publish_preview(update.message, context)



def publish_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👀 Предпросмотр", callback_data="publish_preview")],
        [InlineKeyboardButton("✏️ Изменить текст", callback_data="edit_text")],
        [InlineKeyboardButton("🖼 Заменить фото", callback_data="edit_photo")],
        [InlineKeyboardButton("🔗 Источник", callback_data="edit_source")],
        [InlineKeyboardButton("🚫 Убрать фото", callback_data="publish_no_photo")],
        [InlineKeyboardButton("✅ Опубликовать", callback_data="publish_confirm"),
         InlineKeyboardButton("❌ Отмена", callback_data="publish_cancel")],
    ])


def editor_keyboard():
    return publish_keyboard()


async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(update):
        await query.edit_message_text(
            "⛔ Только для администратора."
        )
        return

    data = query.data

    if data == "publish_cancel":
        context.user_data.clear()

        await query.edit_message_text(
            "❌ Публикация отменена."
        )
        return

    if data == "publish_preview":
        await show_publish_preview(query, context)
        return

    if data == "publish_no_photo":
        context.user_data["publish_photo"] = None
        context.user_data["publish_photo_path"] = None
        await show_publish_preview(query, context)
        return

    if data == "edit_text":
        context.user_data["publish_waiting"] = "text"

        await query.edit_message_text(
            "✏️ Отправь новый текст поста."
        )
        return

    if data == "edit_photo":
        context.user_data["publish_waiting"] = "photo"

        await query.edit_message_text(
            "🖼 Отправь новую фотографию."
        )
        return

    if data == "edit_source":
        context.user_data["publish_waiting"] = "source"

        await query.edit_message_text(
            "🔗 Отправь URL источника."
        )
        return

    if data == "publish_confirm":
        await publish_to_channel(
            query,
            context,
        )
        return


async def send_publish_preview(message, context):
    text = context.user_data.get("publish_text", "")
    photo = context.user_data.get("publish_photo")
    photo_path = context.user_data.get("publish_photo_path")
    caption = text if re.search(r"<(?:b|i|blockquote|a|code|u|s)(?:\s|>)", text, re.I) else markdown_to_telegram_html(text)[:1024]
    markup = publish_keyboard()

    if photo:
        await message.reply_photo(photo=photo, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
    elif photo_path and Path(photo_path).exists():
        with open(photo_path, "rb") as f:
            await message.reply_photo(photo=f, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await message.reply_text(caption or "Пустой пост.", parse_mode=ParseMode.HTML, reply_markup=markup)


async def show_publish_preview(query, context):
    await send_publish_preview(query.message, context)



async def photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    if (
        context.user_data.get("publish_waiting")
        != "photo"
    ):
        return

    photo = update.message.photo[-1]

    context.user_data["publish_photo"] = photo.file_id
    context.user_data["publish_photo_path"] = None
    context.user_data["publish_waiting"] = None

    text = context.user_data.get(
        "publish_text",
        "",
    )

    await update.message.reply_photo(
        photo=photo.file_id,
        caption=(
            "👀 <b>Предпросмотр</b>\n\n"
            + markdown_to_telegram_html(
                text
            )[:800]
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=editor_keyboard(),
    )


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    waiting = context.user_data.get(
        "publish_waiting"
    )

    if waiting == "text":
        context.user_data["publish_text"] = (
            update.message.text
        )

        context.user_data["publish_waiting"] = None

        await send_publish_preview(update.message, context)
        return

    if waiting == "source":
        source = update.message.text.strip()

        if not re.match(
            r"^https?://",
            source,
        ):
            await update.message.reply_text(
                "❌ Нужна ссылка вида https://..."
            )
            return

        context.user_data["publish_source"] = source
        context.user_data["publish_waiting"] = None

        await send_publish_preview(update.message, context)
        return


async def publish_to_channel(
    query,
    context,
):
    text = context.user_data.get(
        "publish_text",
        "",
    )

    photo = context.user_data.get("publish_photo")
    photo_path = context.user_data.get("publish_photo_path")

    source = context.user_data.get(
        "publish_source"
    )

    if source:
        text += (
            f"\n\n🔗 <a href=\"{html.escape(source, quote=True)}\">Источник</a>"
        )

    html_text = text if re.search(r"<(?:b|i|blockquote|a|code|u|s)(?:\s|>)", text, re.I) else markdown_to_telegram_html(text)

    try:
        if photo or (photo_path and Path(photo_path).exists()):
            if photo:
                photo_source = photo
                photo_file = None
            else:
                photo_file = open(photo_path, "rb")
                photo_source = photo_file

            try:
                if len(html_text) <= 1024:
                    await context.bot.send_photo(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        photo=photo_source,
                        caption=html_text,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await context.bot.send_photo(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        photo=photo_source,
                    )
                    await send_long_message(
                        context.bot,
                        TELEGRAM_CHANNEL_ID,
                        text,
                    )
            finally:
                if photo_file:
                    photo_file.close()
        else:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=html_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        context.user_data.clear()

        await query.edit_message_text(
            "✅ <b>Опубликовано в TRD Pulse.</b>",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.exception(
            "Publish failed"
        )

        await query.edit_message_text(
            f"❌ Ошибка публикации:\n{e}"
        )


# ============================================================
# MARKET COMMAND
# ============================================================

async def market_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return
    try:
        data = await get_market_data()
        change = data.get("market_cap_change_24h") or 0
        color = "🟢" if change > 1 else "🔴" if change < -1 else "🟡"
        btc, eth = data["btc"] or {}, data["eth"] or {}
        quote = (
            f"{color} <b>TRD MARKET</b>\n\n"
            f"<b>Рынок остаётся под давлением.</b>\n\n"
            f"<blockquote>Капитализация и основные активы показывают текущее направление рынка. "
            f"Ниже — BTC, ETH и монеты с наиболее сильным движением за 1, 4 и 24 часа.</blockquote>\n\n"
            f"Капитализация: <b>{format_money(data['market_cap'])}</b>\n"
            f"BTC: <b>{format_price(btc.get('current_price'))}</b> · {format_pct(price_change_24h(btc))}\n"
            f"ETH: <b>{format_price(eth.get('current_price'))}</b> · {format_pct(price_change_24h(eth))}"
        )
        movers = []
        for coin in data.get("gainers", [])[:3] + data.get("losers", [])[:3]:
            p, v = _best_mover_period(coin)
            movers.append(f"{coin.get('symbol','').upper()} {format_pct(v)} ({p})")
        if movers:
            quote += "\n\n<b>Сильные движения</b>\n" + "\n".join(movers)
        quote += "\n\n<b>Мнение TRD:</b> ширина движения важнее одной монеты — если сильное снижение сохраняется у нескольких крупных активов, давление остаётся широким."

        image_path = make_market_visual(data)
        context.user_data["publish_text"] = quote
        context.user_data["publish_photo_path"] = str(image_path) if image_path else None
        await send_visual_post(context.bot, update.effective_chat.id, image_path, quote)
    except Exception as e:
        logger.exception("Market error")
        await update.message.reply_text(f"❌ Ошибка рынка:\n{e}")


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    text = (
        "⚡ <b>TRD PULSE</b>\n\n"
        "Бот запущен.\n\n"
        "<b>Быстрый доступ:</b> нажми / в поле ввода — Telegram покажет команды и их описание.\n\n"
        "🤖 Автоматический мониторинг включён."
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# APPLICATION LIFECYCLE
# ============================================================

async def post_init(application):
    check_config()

    await application.bot.set_my_commands([
        ("start", "панель TRD"),
        ("market", "состояние рынка"),
        ("prices", "цены криптовалют"),
        ("news", "важные новости"),
        ("pulse", "импульс рынка"),
        ("publish", "предпросмотр публикации"),
        ("autostatus", "статус автомониторинга"),
    ])

    state = get_state(application)

    state["monitor_task"] = asyncio.create_task(
        monitor_loop(application)
    )

    logger.info(
        "TRD Pulse application initialized"
    )


async def post_shutdown(application):
    task = application.bot_data.get(
        "monitor_task"
    )

    if task:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    logger.info(
        "TRD Pulse application stopped"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    check_config()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "market",
            market_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "prices",
            prices_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "news",
            news_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "pulse",
            pulse_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "publish",
            publish_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "autostatus",
            autostatus_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    logger.info(
        "Starting TRD Pulse Bot..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
