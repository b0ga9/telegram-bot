import html
import logging
import os
import re
from typing import Any

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# CONFIG CHECK
# ============================================================

def check_config() -> None:
    required = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "ADMIN_USER_ID": ADMIN_USER_ID,
    }

    missing = [key for key, value in required.items() if not value]

    if missing:
        raise RuntimeError(
            "Missing environment variables: "
            + ", ".join(missing)
        )


# ============================================================
# HTTP
# ============================================================

async def get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | list[Any]:
    response = await client.get(
        url,
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


# ============================================================
# MARKET DATA
# ============================================================

async def get_market_data() -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        global_data = await get_json(
            client,
            f"{COINGECKO_API}/global",
        )

        markets = await get_json(
            client,
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

    data = global_data["data"]

    coins = {
        coin["symbol"].upper(): coin
        for coin in markets
    }

    sorted_gainers = sorted(
        markets,
        key=lambda x: x.get("price_change_percentage_24h") or 0,
        reverse=True,
    )

    sorted_losers = sorted(
        markets,
        key=lambda x: x.get("price_change_percentage_24h") or 0,
    )

    btc = coins.get("BTC", {})
    eth = coins.get("ETH", {})

    return {
        "total_market_cap": data.get("total_market_cap", {}).get("usd"),
        "total_volume": data.get("total_volume", {}).get("usd"),
        "market_cap_change_24h": data.get("market_cap_change_percentage_24h_usd"),
        "btc_dominance": data.get("market_cap_percentage", {}).get("btc"),
        "active_cryptocurrencies": data.get("active_cryptocurrencies"),
        "btc": btc,
        "eth": eth,
        "gainers": sorted_gainers[:5],
        "losers": sorted_losers[:5],
    }


def format_market_data(data: dict[str, Any]) -> str:
    def fmt_money(value: Any) -> str:
        if value is None:
            return "N/A"

        if value >= 1_000_000_000_000:
            return f"${value / 1_000_000_000_000:.2f}T"

        if value >= 1_000_000_000:
            return f"${value / 1_000_000_000:.2f}B"

        if value >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"

        return f"${value:,.0f}"

    def fmt_pct(value: Any) -> str:
        if value is None:
            return "N/A"

        return f"{value:+.2f}%"

    def coin_line(coin: dict[str, Any]) -> str:
        return (
            f"{coin.get('symbol', '').upper()} "
            f"${coin.get('current_price', 0):,.2f} | "
            f"1h {fmt_pct(coin.get('price_change_percentage_1h_in_currency'))} | "
            f"24h {fmt_pct(coin.get('price_change_percentage_24h'))} | "
            f"7d {fmt_pct(coin.get('price_change_percentage_7d_in_currency'))}"
        )

    lines = [
        "📊 РЫНОК",
        "",
        f"Market Cap: {fmt_money(data['total_market_cap'])}",
        f"Volume 24h: {fmt_money(data['total_volume'])}",
        f"Market Cap 24h: {fmt_pct(data['market_cap_change_24h'])}",
        f"BTC Dominance: {data['btc_dominance']:.2f}%",
        f"Active Coins: {data['active_cryptocurrencies']:,}",
        "",
        "₿ BTC",
        coin_line(data["btc"]),
        "",
        "Ξ ETH",
        coin_line(data["eth"]),
        "",
        "🔥 ЛИДЕРЫ",
    ]

    for coin in data["gainers"]:
        lines.append(
            f"{coin['symbol'].upper()} "
            f"{fmt_pct(coin.get('price_change_percentage_24h'))}"
        )

    lines.extend([
        "",
        "🔻 СЛАБЫЕ",
    ])

    for coin in data["losers"]:
        lines.append(
            f"{coin['symbol'].upper()} "
            f"{fmt_pct(coin.get('price_change_percentage_24h'))}"
        )

    return "\n".join(lines)


# ============================================================
# OPENAI
# ============================================================

async def openai_response(
    prompt: str,
    web_search: bool = False,
) -> str:

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "reasoning": {
            "effort": "low",
        },
        "max_output_tokens": 1800,
    }

    if web_search:
        payload["tools"] = [
            {
                "type": "web_search",
            }
        ]

    async with httpx.AsyncClient() as client:
        response = await client.post(
            OPENAI_API,
            headers=headers,
            json=payload,
            timeout=120,
        )

        response.raise_for_status()

        result = response.json()

    output_text = result.get("output_text")

    if output_text:
        return output_text.strip()

    # Fallback for Responses API output structure.
    parts = []

    for item in result.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                parts.append(text)

    return "\n".join(parts).strip()


# ============================================================
# NEWS
# ============================================================

async def get_news_analysis() -> str:
    prompt = """
Ты — аналитический модуль TRD Pulse.

Найди через Web Search самые важные финансовые и макроэкономические события
за последние 24 часа.

Приоритет:

1. ФРС / ECB / другие центральные банки
2. инфляция CPI / PCE
3. рынок труда / NFP / Jobless Claims
4. ставки и доходности облигаций
5. доллар США
6. S&P 500 / Nasdaq
7. золото / нефть
8. криптовалюты
9. геополитика, санкции и заявления правительств

Правила:

- Не придумывай события.
- Отделяй ФАКТ от ИНТЕРПРЕТАЦИИ.
- Для важных утверждений используй надежные источники.
- Если есть сомнение — прямо укажи это.
- Не давай торговых рекомендаций.
- Не используй HTML.

ФОРМАТИРОВАНИЕ:

Используй Markdown:

**жирный**
*курсив*
***жирный курсив***
`цены, тикеры, проценты`
> цитаты

Ссылки обязательно оформляй как Markdown-гиперссылки:

[Reuters](https://...)
[Bloomberg](https://...)
[Federal Reserve](https://...)

НЕ показывай длинные URL обычным текстом.

Формат:

📰 **TRD NEWS**

━━━━━━━━━━━━

**1. Заголовок события**

**Факт:** ...

*Почему это важно:* ...

> Ключевая цитата, если есть.

Источник: [Reuters](https://...)

━━━━━━━━━━━━

В конце:

***ИТОГ:*** кратко опиши, что сейчас является главным фактором для рынков.
"""


    return await openai_response(
        prompt,
        web_search=True,
    )


# ============================================================
# PULSE
# ============================================================

async def generate_pulse() -> str:
    market_data = await get_market_data()
    market_text = format_market_data(market_data)

    news = await get_news_analysis()

    prompt = f"""
Ты — главный аналитический модуль TRD Pulse.

Твоя задача — объединить данные рынка и актуальные новости.

ДАННЫЕ РЫНКА:

{market_text}

АКТУАЛЬНЫЕ НОВОСТИ:

{news}

Создай финальный TRD Pulse.

ВАЖНО:

- Не придумывай данные.
- Не добавляй события, которых нет в предоставленных данных.
- Разделяй факты и интерпретации.
- Не давай торговых рекомендаций.
- Не говори "покупать", "продавать", "лонг", "шорт".
- Не используй HTML.

Используй Markdown:

**жирный**
*курсив*
***жирный курсив***
`цены / тикеры / проценты`
> цитаты

Источники оформляй только гиперссылками:

[Reuters](https://...)
[Federal Reserve](https://...)
[AP](https://...)

Никаких длинных URL в тексте.

ФОРМАТ:

⚡ **TRD PULSE**

━━━━━━━━━━━━

📊 **РЫНОК**

Ключевые показатели.

━━━━━━━━━━━━

₿ **BTC**

Цена и изменения.

━━━━━━━━━━━━

Ξ **ETH**

Цена и изменения.

━━━━━━━━━━━━

🔥 **ЛИДЕРЫ**

Топ движений.

━━━━━━━━━━━━

🔻 **СЛАБЫЕ**

Топ отрицательных движений.

━━━━━━━━━━━━

📈 **BTC DOMINANCE**

Значение и краткий контекст.

━━━━━━━━━━━━

🌍 **МИР**

Главные мировые события.

━━━━━━━━━━━━

🏦 **МАКРО**

Главные макроэкономические факторы.

━━━━━━━━━━━━

🧠 **ЧТО ПРОИСХОДИТ**

Краткий анализ текущей ситуации.

━━━━━━━━━━━━

⚠️ **РИСК**

Главные факторы риска.

━━━━━━━━━━━━

***TRD SIGNAL***

Выбери одно:

🟢 **RISK-ON**
🟡 **NEUTRAL**
🔴 **RISK-OFF**

И объясни выбор одной короткой фразой.

Источники размести в конце соответствующих блоков.
"""


    return await openai_response(
        prompt,
        web_search=False,
    )


# ============================================================
# MARKDOWN → TELEGRAM HTML
# ============================================================

def markdown_to_telegram_html(text: str) -> str:
    """
    Безопасно преобразует ограниченный Markdown
    в Telegram HTML.

    Поддерживается:

    **bold**
    *italic*
    ***bold italic***
    `code`
    [text](url)
    > quote
    """

    # --------------------------------------------------------
    # Protect code spans
    # --------------------------------------------------------

    code_blocks: list[str] = []

    def protect_code(match: re.Match) -> str:
        value = match.group(1)

        placeholder = f"___TRD_CODE_{len(code_blocks)}___"

        code_blocks.append(
            f"<code>{html.escape(value)}</code>"
        )

        return placeholder

    text = re.sub(
        r"`([^`\n]+)`",
        protect_code,
        text,
    )

    # --------------------------------------------------------
    # Protect Markdown links
    # --------------------------------------------------------

    links: list[str] = []

    def protect_link(match: re.Match) -> str:
        label = html.escape(match.group(1))
        url = match.group(2).strip()

        # Telegram HTML accepts http/https links.
        if not re.match(r"^https?://", url, re.IGNORECASE):
            return html.escape(match.group(0))

        placeholder = f"___TRD_LINK_{len(links)}___"

        links.append(
            f'<a href="{html.escape(url, quote=True)}">{label}</a>'
        )

        return placeholder

    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        protect_link,
        text,
    )

    # --------------------------------------------------------
    # Escape HTML
    # --------------------------------------------------------

    text = html.escape(text)

    # --------------------------------------------------------
    # Bold italic
    # --------------------------------------------------------

    text = re.sub(
        r"\*\*\*(.+?)\*\*\*",
        r"<b><i>\1</i></b>",
        text,
        flags=re.DOTALL,
    )

    # --------------------------------------------------------
    # Bold
    # --------------------------------------------------------

    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL,
    )

    # --------------------------------------------------------
    # Italic
    # --------------------------------------------------------

    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        r"<i>\1</i>",
    )

    # --------------------------------------------------------
    # Quote
    # --------------------------------------------------------

    lines = text.splitlines()
    result_lines = []

    for line in lines:
        if line.startswith("&gt; "):
            result_lines.append(
                f"<blockquote>{line[5:]}</blockquote>"
            )
        else:
            result_lines.append(line)

    text = "\n".join(result_lines)

    # --------------------------------------------------------
    # Restore links
    # --------------------------------------------------------

    for index, value in enumerate(links):
        text = text.replace(
            f"___TRD_LINK_{index}___",
            value,
        )

    # --------------------------------------------------------
    # Restore code
    # --------------------------------------------------------

    for index, value in enumerate(code_blocks):
        text = text.replace(
            f"___TRD_CODE_{index}___",
            value,
        )

    return text.strip()


# ============================================================
# MESSAGE SENDING
# ============================================================

async def send_long_message(
    update: Update,
    text: str,
) -> None:

    formatted = markdown_to_telegram_html(text)

    max_length = 3900

    for i in range(0, len(formatted), max_length):
        chunk = formatted[i:i + max_length]

        try:
            await update.message.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
            )

        except Exception:
            # Safety fallback:
            # if Telegram rejects malformed formatting,
            # send plain text instead.
            logger.exception(
                "Telegram HTML formatting failed. "
                "Sending plain text."
            )

            await update.message.reply_text(
                text[i:i + max_length],
            )


# ============================================================
# ADMIN
# ============================================================

def is_admin(update: Update) -> bool:
    if not update.effective_user:
        return False

    return str(update.effective_user.id) == str(ADMIN_USER_ID)


async def admin_only(
    update: Update,
) -> bool:

    if not is_admin(update):
        if update.message:
            await update.message.reply_text(
                "Команда доступна только администратору."
            )

        return False

    return True


# ============================================================
# COMMANDS
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

    await update.message.reply_text(
        "⚡ TRD Pulse\n\n"
        "/market — состояние рынка\n"
        "/news — важные новости\n"
        "/pulse — полный анализ\n"
        "/publish — опубликовать Pulse в канал"
    )


async def market(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

    try:
        data = await get_market_data()
        text = format_market_data(data)

        await send_long_message(
            update,
            text,
        )

    except Exception as exc:
        logger.exception("Market error")

        await update.message.reply_text(
            f"Ошибка получения рынка: {exc}"
        )


async def news(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

    try:
        result = await get_news_analysis()

        await send_long_message(
            update,
            result,
        )

    except Exception as exc:
        logger.exception("News error")

        await update.message.reply_text(
            f"Ошибка получения новостей: {exc}"
        )


async def pulse(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

    try:
        result = await generate_pulse()

        await send_long_message(
            update,
            result,
        )

    except Exception as exc:
        logger.exception("Pulse error")

        await update.message.reply_text(
            f"Ошибка генерации Pulse: {exc}"
        )


async def publish(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not await admin_only(update):
        return

    try:
        result = await generate_pulse()

        formatted = markdown_to_telegram_html(result)

        await context.bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=formatted,
            parse_mode=ParseMode.HTML,
        )

        await update.message.reply_text(
            "✅ TRD Pulse опубликован в канал."
        )

    except Exception as exc:
        logger.exception("Publish error")

        await update.message.reply_text(
            f"Ошибка публикации: {exc}"
        )


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    check_config()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("market", market)
    )

    application.add_handler(
        CommandHandler("news", news)
    )

    application.add_handler(
        CommandHandler("pulse", pulse)
    )

    application.add_handler(
        CommandHandler("publish", publish)
    )

    logger.info("TRD Pulse bot started")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
