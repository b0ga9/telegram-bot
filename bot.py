import asyncio
import hashlib
import html
import logging
import os
import re
from datetime import datetime, timezone

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")

ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

COINGECKO_API = "https://api.coingecko.com/api/v3"
OPENAI_API = "https://api.openai.com/v1/responses"

# Automatic monitoring
PRICE_CHECK_INTERVAL = 15 * 60       # 15 minutes
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

        await send_long_message(
            context.bot,
            update.effective_chat.id,
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


async def generate_pulse(recent_news=""):
    market = await get_market_data()

    prompt = build_pulse_prompt(
        market,
        recent_news,
    )

    result = await openai_response(
        prompt,
        web_search=False,
        max_output_tokens=700,
        retries=0,
    )

    return result


async def pulse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    try:
        news = await get_news_analysis()

        if "NO_NEWS" in news:
            news = ""

        pulse = await generate_pulse(news)

        context.user_data["pulse_text"] = pulse

        await send_long_message(
            context.bot,
            update.effective_chat.id,
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

def get_state(application):
    state = application.bot_data

    state.setdefault("last_prices", {})
    state.setdefault("last_price_alert", {})
    state.setdefault("last_news_key", None)
    state.setdefault("last_news_hash", None)
    state.setdefault("last_auto_news_at", 0)
    state.setdefault("last_auto_pulse_at", 0)
    state.setdefault("last_news_text", "")
    state.setdefault("monitor_started_at", datetime.now(timezone.utc))

    return state


def find_price_triggers(coins, state):
    triggers = []

    now = asyncio.get_running_loop().time()

    for coin in coins:
        coin_id = coin.get("id")

        if not coin_id:
            continue

        symbol = coin.get("symbol", "").upper()

        one_hour = price_change_1h(coin)

        if coin_id == "bitcoin":
            threshold = BTC_ALERT_1H
        elif coin_id == "ethereum":
            threshold = ETH_ALERT_1H
        else:
            threshold = ALT_ALERT_1H

        if abs(one_hour) < threshold:
            continue

        last_alert = state["last_price_alert"].get(
            coin_id,
            0,
        )

        if now - last_alert < PRICE_ALERT_COOLDOWN:
            continue

        triggers.append(
            {
                "id": coin_id,
                "symbol": symbol,
                "name": coin.get("name", symbol),
                "price": coin.get("current_price"),
                "1h": one_hour,
                "24h": price_change_24h(coin),
            }
        )

    return sorted(
        triggers,
        key=lambda x: abs(x["1h"]),
        reverse=True,
    )


def build_price_alert(triggers):
    if not triggers:
        return None

    lines = [
        "🚨 <b>TRD MARKET ALERT</b>",
        "",
    ]

    strongest = triggers[0]

    if strongest["1h"] > 0:
        lines.append(
            "⚡ Рынок заметно ускорился вверх."
        )
    else:
        lines.append(
            "⚡ На рынке усилилось движение вниз."
        )

    lines.append("")

    for item in triggers[:3]:
        emoji = "🟢" if item["1h"] > 0 else "🔴"

        lines.append(
            f"{emoji} <b>{item['symbol']}</b> "
            f"{format_price(item['price'])} "
            f"<b>{format_pct(item['1h'])}</b> за 1ч"
        )

    lines.append("")
    lines.append(
        "📊 Сигнал основан на заметном изменении цены."
    )

    return "\n".join(lines)


def should_trigger_pulse(triggers):
    if not triggers:
        return False

    # BTC/ETH strong move
    for item in triggers:
        if item["id"] in ("bitcoin", "ethereum"):
            return True

    # Broad altcoin move
    return len(triggers) >= 3


async def automatic_price_check(application):
    state = get_state(application)

    try:
        coins = await get_monitored_prices()

        triggers = find_price_triggers(
            coins,
            state,
        )

        # Save prices
        for coin in coins:
            state["last_prices"][coin["id"]] = {
                "price": coin.get("current_price"),
                "1h": price_change_1h(coin),
                "24h": price_change_24h(coin),
            }

        if not triggers:
            logger.info("Automatic price check: no trigger")
            return

        now = asyncio.get_running_loop().time()

        alert = build_price_alert(triggers)

        if alert:
            await application.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=alert,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

            for item in triggers[:3]:
                state["last_price_alert"][item["id"]] = now

        # Automatic Pulse
        if should_trigger_pulse(triggers):
            last_pulse = state["last_auto_pulse_at"]

            if now - last_pulse >= PULSE_COOLDOWN:
                await automatic_pulse(
                    application,
                    coins,
                )

                state["last_auto_pulse_at"] = now

    except Exception:
        logger.exception(
            "Automatic price monitoring failed"
        )


async def automatic_pulse(application, coins):
    try:
        market = await get_market_data()

        recent_news = get_state(application).get(
            "last_news_text",
            "",
        )

        prompt = build_pulse_prompt(
            market,
            recent_news,
        )

        result = await openai_response(
            prompt,
            web_search=False,
            max_output_tokens=600,
            retries=0,
        )

        await send_long_message(
            application.bot,
            TELEGRAM_CHANNEL_ID,
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

        state["last_auto_news_at"] = now

        importance, key, post = parse_news_result(
            result
        )

        logger.info(
            "Automatic news: importance=%s key=%s",
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
            post[:1000].lower().encode("utf-8")
        ).hexdigest()

        if fingerprint == state["last_news_hash"]:
            logger.info(
                "Automatic news skipped: duplicate"
            )
            return

        if key == state["last_news_key"]:
            logger.info(
                "Automatic news skipped: same key"
            )
            return

        await send_long_message(
            application.bot,
            TELEGRAM_CHANNEL_ID,
            post,
        )

        state["last_news_key"] = key
        state["last_news_hash"] = fingerprint
        state["last_news_text"] = post

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

    # Initial delay so bot can start normally
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

def publish_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📝 Только текст",
                    callback_data="publish_text",
                ),
                InlineKeyboardButton(
                    "🖼 С фотографией",
                    callback_data="publish_photo",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Отмена",
                    callback_data="publish_cancel",
                )
            ],
        ]
    )


def editor_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✏️ Изменить текст",
                    callback_data="edit_text",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🖼 Фото",
                    callback_data="edit_photo",
                ),
                InlineKeyboardButton(
                    "🔗 Источник",
                    callback_data="edit_source",
                ),
            ],
            [
                InlineKeyboardButton(
                    "✅ Опубликовать",
                    callback_data="publish_confirm",
                ),
                InlineKeyboardButton(
                    "❌ Отмена",
                    callback_data="publish_cancel",
                ),
            ],
        ]
    )


async def publish_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    text = (
        context.user_data.get("pulse_text")
        or context.user_data.get("news_text")
    )

    if not text:
        text = (
            "У тебя пока нет подготовленного "
            "NEWS или PULSE."
        )

    context.user_data["publish_text"] = text
    context.user_data["publish_photo"] = None
    context.user_data["publish_source"] = None
    context.user_data["publish_waiting"] = None

    await update.message.reply_text(
        "📤 <b>Публикация TRD Pulse</b>\n\n"
        "Выбери формат:",
        parse_mode=ParseMode.HTML,
        reply_markup=publish_keyboard(),
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

    if data == "publish_cancel":
        context.user_data.clear()

        await query.edit_message_text(
            "❌ Публикация отменена."
        )
        return

    if data == "publish_text":
        context.user_data["publish_photo"] = None

        await query.edit_message_text(
            "📝 Выбран режим <b>только текст</b>.\n\n"
            "Проверь пост перед публикацией.",
            parse_mode=ParseMode.HTML,
            reply_markup=editor_keyboard(),
        )
        return

    if data == "publish_photo":
        context.user_data["publish_waiting"] = "photo"

        await query.edit_message_text(
            "🖼 Отправь фотографию следующим сообщением.\n\n"
            "После этого я покажу предпросмотр."
        )
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

        await update.message.reply_text(
            "✏️ Текст обновлён.",
            reply_markup=editor_keyboard(),
        )
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

        await update.message.reply_text(
            "🔗 Источник добавлен.",
            reply_markup=editor_keyboard(),
        )
        return


async def publish_to_channel(
    query,
    context,
):
    text = context.user_data.get(
        "publish_text",
        "",
    )

    photo = context.user_data.get(
        "publish_photo"
    )

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
        if photo:
            await context.bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=photo,
                caption=html_text[:1000],
                parse_mode=ParseMode.HTML,
            )
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
        "<b>Команды:</b>\n"
        "/market — состояние рынка\n"
        "/prices — цены отслеживаемых монет\n"
        "/news — важные новости\n"
        "/pulse — рыночный Pulse\n"
        "/publish — подготовить публикацию\n"
        "/autostatus — состояние автомониторинга\n\n"
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
