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
PRICE_CHECK_INTERVAL = 10 * 60       # market/price checks are cheap HTTP calls
NEWS_CHECK_INTERVAL = 60 * 60        # news candidate scan
LIVE_BOARD_INTERVAL = 60              # pinned board refresh

MARKET_AUTO_COOLDOWN = 2 * 60 * 60
PRICES_AUTO_COOLDOWN = 3 * 60 * 60
NEWS_AUTO_COOLDOWN = 45 * 60
PULSE_COOLDOWN = 60 * 60
LIVE_BOARD_INTERVAL = 60


BTC_ALERT_1H = 2.0
ETH_ALERT_1H = 3.0
ALT_ALERT_1H = 5.0

MONITORED_COINS = [
    "bitcoin", "ethereum", "binancecoin", "ripple", "solana",
    "dogecoin", "cardano", "tron", "avalanche-2", "chainlink",
    "polkadot", "litecoin", "near", "aptos", "arbitrum",
    "optimism", "sui", "aave", "uniswap", "pepe",
    "render-token", "injective-protocol", "jupiter-exchange-solana",
    "celestia", "filecoin", "vechain", "the-graph", "maker",
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
    global_data = await get_json(
        f"{COINGECKO_API}/global"
    )

    coins = await get_json(
        f"{COINGECKO_API}/coins/markets",
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 100,
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "1h,24h,7d",
        },
    )

    data = global_data.get("data", {})

    btc = next(
        (coin for coin in coins if coin.get("id") == "bitcoin"),
        None,
    )

    eth = next(
        (coin for coin in coins if coin.get("id") == "ethereum"),
        None,
    )

    gainers = sorted(
        coins,
        key=lambda x: x.get("price_change_percentage_24h", 0) or 0,
        reverse=True,
    )[:5]

    losers = sorted(
        coins,
        key=lambda x: x.get("price_change_percentage_24h", 0) or 0,
    )[:5]

    return {
        "market_cap": data.get("total_market_cap", {}).get("usd"),
        "volume": data.get("total_volume", {}).get("usd"),
        "market_cap_change_24h": data.get(
            "market_cap_change_percentage_24h_usd"
        ),
        "btc_dominance": data.get(
            "market_cap_percentage", {}
        ).get("btc"),
        "active_cryptocurrencies": data.get(
            "active_cryptocurrencies"
        ),
        "btc": btc,
        "eth": eth,
        "coins": coins,
        "gainers": gainers,
        "losers": losers,
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


def format_market_data(data):
    btc = data["btc"] or {}
    eth = data["eth"] or {}

    text = (
        "📊 <b>TRD MARKET</b>\n\n"
        f"🌐 Market Cap: <b>{format_money(data['market_cap'])}</b>\n"
        f"💵 Volume 24h: <b>{format_money(data['volume'])}</b>\n"
        f"📈 Market Cap 24h: <b>{format_pct(data['market_cap_change_24h'])}</b>\n"
        f"₿ BTC Dominance: <b>{format_pct(data['btc_dominance'])}</b>\n\n"
        f"₿ BTC: <b>{format_price(btc.get('current_price'))}</b> "
        f"{format_pct(price_change_24h(btc))}\n"
        f"Ξ ETH: <b>{format_price(eth.get('current_price'))}</b> "
        f"{format_pct(price_change_24h(eth))}\n\n"
        "<b>🚀 Gainers</b>\n"
    )

    for coin in data["gainers"][:5]:
        text += (
            f"• {coin.get('symbol', '').upper()} "
            f"{format_pct(price_change_24h(coin))}\n"
        )

    text += "\n<b>🔻 Losers</b>\n"

    for coin in data["losers"][:5]:
        text += (
            f"• {coin.get('symbol', '').upper()} "
            f"{format_pct(price_change_24h(coin))}\n"
        )

    return text


# ============================================================
# PRICES
# ============================================================

async def get_monitored_prices():
    return await get_json(
        f"{COINGECKO_API}/coins/markets",
        params={
            "vs_currency": "usd",
            "ids": ",".join(MONITORED_COINS),
            "order": "market_cap_desc",
            "per_page": len(MONITORED_COINS),
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "1h,24h",
        },
    )


def format_prices(coins):
    lines = ["💹 <b>TRD PRICES</b>\n"]

    for coin in coins:
        symbol = coin.get("symbol", "").upper()
        price = format_price(coin.get("current_price"))
        one_hour = price_change_1h(coin)
        day = price_change_24h(coin)

        lines.append(
            f"<b>{symbol}</b>  {price}  "
            f"<code>1h {format_pct(one_hour)}</code>  "
            f"<code>24h {format_pct(day)}</code>"
        )

    return "\n".join(lines)


async def prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    try:
        coins = await get_monitored_prices()
        image_path = make_price_visual(coins)
        strongest = sorted(
            coins,
            key=lambda c: abs(price_change_1h(c)),
            reverse=True,
        )[:1]
        strongest_text = (
            f"{strongest[0].get('symbol','').upper()} "
            f"{format_pct(price_change_1h(strongest[0]))}"
            if strongest else "без сильного движения"
        )
        caption = (
            "💰 <b>TRD PRICES</b>\n\n"
            "<blockquote><b>Главное:</b> цены отслеживаемых монет "
            "и самые заметные движения за час.</blockquote>\n\n"
            f"Сильнейшее движение сейчас: <b>{strongest_text}</b>\n\n"
            "🕒 Обновлено сейчас"
        )
        if image_path:
            await update.message.reply_photo(
                photo=image_path,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                format_prices(coins),
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.exception("Prices error")
        await update.message.reply_text(
            f"❌ Ошибка получения цен:\n{e}"
        )


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
    html_text = markdown_to_telegram_html(text)

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
        base = await generate_news_image(
            api_key=OPENAI_API_KEY,
            api_url=OPENAI_API,
            model=IMAGE_MODEL,
            post_text=post,
            event_key=key,
            output_dir=VISUAL_DIR,
        )
        if not base:
            return None
        from visual_engine import build_news_card
        return build_news_card(base, post, output_dir=VISUAL_DIR)
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
        caption = markdown_to_telegram_html(text)
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

Проверь событие минимум по 2 независимым источникам, а для важности 9–10 желательно по 3.
Используй разные типы источников: официальные данные/ведомства + крупное деловое или финансовое СМИ.
Не выдумывай URL. В конце укажи до 3 реальных источников.

Добавь короткое «Мнение TRD» — это именно редакционная оценка на основе подтверждённых фактов,
без торгового совета и без выдуманных причин.

Верни строго в формате:

IMPORTANCE: число от 0 до 10
KEY: короткий уникальный ключ события

POST:
📰 **ЗАГОЛОВОК**

> ⚡ **Главное:** одна короткая мысль о событии.

2–3 коротких предложения с фактами.

**Почему важно**
Одно короткое предложение о влиянии на рынок.

**Мнение TRD**
Одно короткое предложение: как TRD оценивает значение события.

**Источники**
🔗 URL
🔗 URL
🔗 URL

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
        image_path = await make_news_visual(post, key=key, importance=importance)
        context.user_data["prices_image_path"] = str(image_path) if image_path else None
        context.user_data["news_image_path"] = str(image_path) if image_path else None
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

    return f"""
Ты — аналитик Telegram-канала TRD Pulse.

Сделай ОЧЕНЬ короткий рыночный Pulse.

Данные рынка:

BTC:
цена: {format_price(btc.get("current_price"))}
1h: {format_pct(price_change_1h(btc))}
24h: {format_pct(price_change_24h(btc))}

ETH:
цена: {format_price(eth.get("current_price"))}
1h: {format_pct(price_change_1h(eth))}
24h: {format_pct(price_change_24h(eth))}

Market Cap:
{format_money(market.get("market_cap"))}

Изменение Market Cap:
{format_pct(market.get("market_cap_change_24h"))}

BTC dominance:
{format_pct(market.get("btc_dominance"))}

Последняя важная новость:
{recent_news[:3000]}

Формат:

⚡ **TRD PULSE**

**Короткий заголовок**

> ⚡ **Главное:** одна главная мысль движения рынка.

Коротко опиши ситуацию в 2–3 предложениях.

**Главное**
• BTC — цена и изменение
• ETH — цена и изменение
• один важный фактор

**Что важно**
Одно короткое предложение о ближайшем факторе.

Не давай торговых рекомендаций.
Не используй длинные объяснения.
Символ ">" — это визуальный блок, НЕ настоящая цитата.
Объём: примерно 100–180 слов.
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

        image_path = make_pulse_visual(market=market)
        context.user_data["pulse_image_path"] = str(image_path) if image_path else None
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
    state.setdefault("last_price_post_at", 0)
    state.setdefault("last_auto_pulse_at", 0)

    state.setdefault("last_news_key", None)
    state.setdefault("last_news_hash", None)
    state.setdefault("last_auto_news_at", 0)
    state.setdefault("last_news_text", "")

    # Individual automation switches.
    state.setdefault("auto_news", True)
    state.setdefault("auto_pulse", True)
    state.setdefault("auto_market", True)
    state.setdefault("auto_prices", True)
    state.setdefault("live_board", True)

    state.setdefault("live_board_message_id", None)
    state.setdefault("monitor_started_at", datetime.now(timezone.utc))
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
    """Дешёвый анализ цен; публикация происходит только при сильном событии."""
    state = get_state(application)
    try:
        coins = await get_monitored_prices()
        update_price_history(coins, state)
        market = await get_market_data()
        signal = analyze_market_signal(coins, market, state)
        now = asyncio.get_running_loop().time()

        # MARKET: only on a meaningful broad/regime change.
        if state["auto_market"] and should_publish_market_alert(signal):
            if now - state["last_market_alert_at"] >= MARKET_AUTO_COOLDOWN:
                alert = build_market_alert(signal)
                image_path = make_market_visual(market, signal=signal)
                await send_visual_post(application.bot, TELEGRAM_CHANNEL_ID, image_path, alert)
                state["last_market_alert_at"] = now
                logger.info("Automatic MARKET published")

        # PRICES: publish only when a monitored coin makes a real move.
        strongest = max(coins, key=lambda c: abs(price_change_1h(c)), default=None)
        strongest_1h = abs(price_change_1h(strongest)) if strongest else 0
        strongest_24h = abs(price_change_24h(strongest)) if strongest else 0
        if (
            state["auto_prices"]
            and strongest
            and (strongest_1h >= 4.0 or strongest_24h >= 8.0)
            and now - state["last_price_post_at"] >= PRICES_AUTO_COOLDOWN
        ):
            image_path = make_price_visual(coins)
            text = (
                "💰 <b>TRD PRICES</b>\n\n"
                "<blockquote><b>Главное:</b> на рынке появилось заметное движение.</blockquote>\n\n"
                f"Сильнейшее сейчас: <b>{strongest.get('symbol','').upper()}</b> "
                f"{format_pct(price_change_1h(strongest))} за 1 час.\n\n"
                "Данные обновлены автоматически."
            )
            await send_visual_post(application.bot, TELEGRAM_CHANNEL_ID, image_path, text)
            state["last_price_post_at"] = now
            logger.info("Automatic PRICES published")

        # PULSE: only after a significant signal, not on a timer.
        if state["auto_pulse"] and should_trigger_pulse(signal):
            if now - state["last_auto_pulse_at"] >= PULSE_COOLDOWN:
                await automatic_pulse(application, coins, signal, market=market)
                state["last_auto_pulse_at"] = now

    except Exception:
        logger.exception("Automatic market monitoring failed")


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
        < NEWS_AUTO_COOLDOWN
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
            importance < 9
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


async def update_live_board(application):
    state = get_state(application)
    if not state["live_board"]:
        return

    try:
        coins = await get_json(
            f"{COINGECKO_API}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 10,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "1h,24h",
            },
            timeout=20,
        )
        lines = ["📌 <b>TRD • РЫНОК СЕЙЧАС</b>", ""]
        for i, coin in enumerate(coins[:10], 1):
            symbol = coin.get("symbol", "").upper()
            price = format_price(coin.get("current_price"))
            c1 = price_change_1h(coin)
            c24 = price_change_24h(coin)
            e1 = "🟢" if c1 > 0 else "🔴" if c1 < 0 else "⚪"
            lines.append(
                f"<b>{i}. {symbol}</b>  {price}  "
                f"{e1} {format_pct(c1)} / {format_pct(c24)}"
            )
        lines.append("")
        lines.append("1ч / 24ч • обновление каждую минуту")
        text = "\n".join(lines)

        message_id = state.get("live_board_message_id")
        if message_id:
            try:
                await application.bot.edit_message_text(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return
            except Exception as exc:
                logger.warning("Live board edit failed, recreating: %s", exc)

        msg = await application.bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        state["live_board_message_id"] = msg.message_id
        try:
            await application.bot.pin_chat_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                message_id=msg.message_id,
                disable_notification=True,
            )
        except Exception as exc:
            logger.warning("Could not pin live board: %s", exc)
    except Exception:
        logger.exception("Live board update failed")


async def monitor_loop(application):
    logger.info("TRD automatic monitoring started")
    await asyncio.sleep(10)
    last_price_check = 0
    last_news_check = 0
    last_live_board = 0

    while True:
        try:
            state = get_state(application)
            now = asyncio.get_running_loop().time()

            if now - last_live_board >= LIVE_BOARD_INTERVAL:
                await update_live_board(application)
                last_live_board = now

            if now - last_price_check >= PRICE_CHECK_INTERVAL:
                await automatic_price_check(application)
                last_price_check = now

            if state["auto_news"] and now - last_news_check >= NEWS_CHECK_INTERVAL:
                await automatic_news_check(application)
                last_news_check = now

        except asyncio.CancelledError:
            logger.info("TRD automatic monitoring stopped")
            raise
        except Exception:
            logger.exception("Monitor loop error")

        await asyncio.sleep(15)



# ============================================================
# AUTO STATUS
# ============================================================

def automation_keyboard(state):
    def mark(key):
        return "🟢" if state[key] else "⚪"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{mark('auto_news')} Новости", callback_data="auto_news"),
            InlineKeyboardButton(f"{mark('auto_pulse')} Пульс", callback_data="auto_pulse"),
        ],
        [
            InlineKeyboardButton(f"{mark('auto_market')} Рынок", callback_data="auto_market"),
            InlineKeyboardButton(f"{mark('auto_prices')} Цены", callback_data="auto_prices"),
        ],
        [
            InlineKeyboardButton(f"{mark('live_board')} Закреп", callback_data="live_board"),
        ],
    ])


def automation_status_text(state):
    return (
        "🤖 <b>TRD АВТОМАТИКА</b>\n\n"
        f"🟢 Новости: <b>{'ВКЛ' if state['auto_news'] else 'ВЫКЛ'}</b> — только важные события (9–10/10)\n"
        f"🟢 Пульс: <b>{'ВКЛ' if state['auto_pulse'] else 'ВЫКЛ'}</b> — только сильное движение\n"
        f"🟢 Рынок: <b>{'ВКЛ' if state['auto_market'] else 'ВЫКЛ'}</b> — при широком/резком движении\n"
        f"🟢 Цены: <b>{'ВКЛ' if state['auto_prices'] else 'ВЫКЛ'}</b> — при сильном движении монеты\n"
        f"📌 Закреп: <b>{'ВКЛ' if state['live_board'] else 'ВЫКЛ'}</b> — топ-10, обновление раз в минуту\n\n"
        "AI используется для NEWS и PULSE. MARKET, PRICES и закреп — без AI."
    )


async def autostatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    state = get_state(context.application)
    await update.message.reply_text(
        automation_status_text(state),
        parse_mode=ParseMode.HTML,
        reply_markup=automation_keyboard(state),
    )


# ============================================================
# PUBLISH EDITOR
# ============================================================

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


async def publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    # If a prepared post exists, open it. Otherwise switch to manual compose mode.
    candidates = [
        ("pulse_text", "pulse_image_path"),
        ("news_text", "news_image_path"),
        ("market_text", "market_image_path"),
        ("prices_text", "prices_image_path"),
    ]
    for text_key, image_key in candidates:
        if context.user_data.get(text_key):
            context.user_data["publish_text"] = context.user_data[text_key]
            path = context.user_data.get(image_key)
            context.user_data["publish_photo_path"] = path if path and Path(path).exists() else None
            context.user_data["publish_photo"] = None
            context.user_data["publish_waiting"] = None
            await send_publish_preview(update.message, context)
            return

    context.user_data["publish_waiting"] = "text"
    await update.message.reply_text(
        "✍️ <b>Новый пост</b>\n\n"
        "Отправь текст — я покажу предпросмотр и дам кнопки для фото и публикации.",
        parse_mode=ParseMode.HTML,
    )


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

    if data in {"auto_news", "auto_pulse", "auto_market", "auto_prices", "live_board"}:
        state = get_state(context.application)
        state[data] = not state[data]
        await query.edit_message_text(
            automation_status_text(state),
            parse_mode=ParseMode.HTML,
            reply_markup=automation_keyboard(state),
        )
        return

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
    caption = markdown_to_telegram_html(text)[:1024]
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

    html_text = markdown_to_telegram_html(
        text
    )

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

        image_path = make_market_visual(data)
        market_text = (
            "🌐 <b>TRD MARKET</b>\n\n"
            "<blockquote><b>Главное:</b> состояние рынка в одном экране.</blockquote>\n\n"
            f"Капитализация: <b>{format_money(data['market_cap'])}</b>\n"
            f"Изменение 24ч: <b>{format_pct(data['market_cap_change_24h'])}</b>\n"
            f"₿ BTC: <b>{format_price((data['btc'] or {}).get('current_price'))}</b>\n"
            f"Изменение BTC 24ч: <b>{format_pct(price_change_24h(data['btc'] or {}))}</b>\n"
            f"Ξ ETH: <b>{format_price((data['eth'] or {}).get('current_price'))}</b>\n"
            f"Изменение ETH 24ч: <b>{format_pct(price_change_24h(data['eth'] or {}))}</b>"
        )
        context.user_data["market_text"] = market_text
        context.user_data["market_image_path"] = str(image_path) if image_path else None
        if image_path:
            await update.message.reply_photo(
                photo=image_path,
                caption=markdown_to_telegram_html(market_text),
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                format_market_data(data),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as e:
        logger.exception(
            "Market error"
        )

        await update.message.reply_text(
            f"❌ Ошибка рынка:\n{e}"
        )


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
        "🤖 Автоматика настраивается через /autostatus.\n\nНовый пост: /newpost."
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
        ("autostatus", "настройки автомониторинга"),
        ("newpost", "написать новый пост"),
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
        CommandHandler(
            "newpost",
            publish_command,
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
