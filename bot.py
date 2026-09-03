import html
import logging
import os
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

COINGECKO_API = "https://api.coingecko.com/api/v3"
OPENAI_API = "https://api.openai.com/v1/responses"

HTTP_TIMEOUT = 30.0


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("trd-pulse")


# ============================================================
# VALIDATION
# ============================================================

def check_config() -> None:
    missing = []

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHANNEL_ID:
        missing.append("TELEGRAM_CHANNEL_ID")

    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")

    if missing:
        raise RuntimeError(
            "Не заданы переменные окружения: "
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
    response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()


# ============================================================
# COINGECKO
# ============================================================

async def get_market_data() -> dict[str, Any]:
    """
    Получает базовую картину крипторынка:

    - BTC
    - ETH
    - top gainers
    - top losers
    - market cap
    - volume
    """

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={
            "User-Agent": "TRD-Pulse/1.0",
            "Accept": "application/json",
        },
    ) as client:

        global_data = await get_json(
            client,
            f"{COINGECKO_API}/global",
        )

        coins = await get_json(
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

    if not isinstance(global_data, dict):
        raise RuntimeError("Некорректный ответ CoinGecko global")

    if not isinstance(coins, list):
        raise RuntimeError("Некорректный ответ CoinGecko markets")

    market = global_data.get("data", {})

    cleaned = []

    for coin in coins:
        cleaned.append(
            {
                "symbol": str(coin.get("symbol", "")).upper(),
                "name": coin.get("name"),
                "price_usd": coin.get("current_price"),
                "market_cap": coin.get("market_cap"),
                "volume_24h": coin.get("total_volume"),
                "change_1h": coin.get("price_change_percentage_1h_in_currency"),
                "change_24h": coin.get("price_change_percentage_24h_in_currency"),
                "change_7d": coin.get("price_change_percentage_7d_in_currency"),
            }
        )

    gainers = sorted(
        cleaned,
        key=lambda x: x.get("change_24h") or -999,
        reverse=True,
    )[:10]

    losers = sorted(
        cleaned,
        key=lambda x: x.get("change_24h") or 999,
    )[:10]

    btc = next(
        (coin for coin in cleaned if coin["symbol"] == "BTC"),
        None,
    )

    eth = next(
        (coin for coin in cleaned if coin["symbol"] == "ETH"),
        None,
    )

    return {
        "market_cap_usd": market.get("total_market_cap", {}).get("usd"),
        "volume_24h_usd": market.get("total_volume", {}).get("usd"),
        "market_cap_change_24h": market.get("market_cap_change_percentage_24h_usd"),
        "btc": btc,
        "eth": eth,
        "top_gainers": gainers,
        "top_losers": losers,
    }


# ============================================================
# FORMAT MARKET DATA
# ============================================================

def format_market_data(data: dict[str, Any]) -> str:
    lines = []

    market_cap = data.get("market_cap_usd")
    volume = data.get("volume_24h_usd")
    market_change = data.get("market_cap_change_24h")

    if market_cap:
        lines.append(
            f"Total crypto market cap: ${market_cap:,.0f}"
        )

    if volume:
        lines.append(
            f"24h volume: ${volume:,.0f}"
        )

    if market_change is not None:
        lines.append(
            f"Market cap 24h change: {market_change:.2f}%"
        )

    lines.append("")

    for label in ("btc", "eth"):
        coin = data.get(label)

        if not coin:
            continue

        lines.append(
            f"{coin['symbol']}: "
            f"${coin['price_usd']:,.2f} | "
            f"1h {coin.get('change_1h', 0) or 0:+.2f}% | "
            f"24h {coin.get('change_24h', 0) or 0:+.2f}% | "
            f"7d {coin.get('change_7d', 0) or 0:+.2f}%"
        )

    lines.append("")
    lines.append("TOP GAINERS")

    for coin in data["top_gainers"][:10]:
        change = coin.get("change_24h") or 0
        lines.append(
            f"{coin['symbol']}: {change:+.2f}%"
        )

    lines.append("")
    lines.append("TOP LOSERS")

    for coin in data["top_losers"][:10]:
        change = coin.get("change_24h") or 0
        lines.append(
            f"{coin['symbol']}: {change:+.2f}%"
        )

    return "\n".join(lines)


# ============================================================
# OPENAI
# ============================================================

async def openai_response(
    prompt: str,
    web_search: bool = False,
) -> str:

    tools = []

    if web_search:
        tools.append(
            {
                "type": "web_search"
            }
        )

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "reasoning": {
            "effort": "low"
        },
        "max_output_tokens": 1800,
    }

    if tools:
        payload["tools"] = tools

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(
        timeout=90.0,
        headers=headers,
    ) as client:

        response = await client.post(
            OPENAI_API,
            json=payload,
        )

        if response.status_code >= 400:
            logger.error(
                "OpenAI error: %s",
                response.text,
            )

        response.raise_for_status()

        data = response.json()

    # Responses API normally exposes the final generated text here.
    output_text = data.get("output_text")

    if output_text:
        return output_text.strip()

    # Fallback parser.
    result = []

    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text = content.get("text")

                if text:
                    result.append(text)

    if result:
        return "\n".join(result).strip()

    raise RuntimeError(
        "OpenAI не вернул текстовый результат."
    )


# ============================================================
# NEWS + MACRO
# ============================================================

async def get_news_analysis() -> str:
    prompt = """
Ты работаешь как news researcher для проекта TRD Pulse.

Используй Web Search и найди наиболее важные события последних 24 часов,
которые потенциально способны повлиять на финансовые рынки.

Приоритет:

1. ФРС / ЕЦБ / центральные банки
2. инфляция / CPI / PCE
3. NFP / рынок труда / безработица
4. ставки и доходности облигаций
5. доллар США
6. S&P 500 / Nasdaq
7. золото / нефть
8. крипторынок
9. войны и геополитика
10. санкции
11. решения правительств
12. крупные заявления официальных лиц

Не придумывай события.

Для каждого действительно важного события укажи:

- что произошло;
- когда;
- какой рынок это затрагивает;
- почему событие может иметь значение;
- является ли связь подтвержденной или только возможной.

Не превращай возможную причинность в установленный факт.

Верни краткий структурированный research report на русском языке.
"""

    return await openai_response(
        prompt,
        web_search=True,
    )


# ============================================================
# TRD PULSE
# ============================================================

async def generate_pulse(
    market_data: dict[str, Any],
    news: str,
) -> str:

    market_text = format_market_data(market_data)

    prompt = f"""
Ты — аналитический движок TRD Pulse.

Твоя задача — объединить реальные рыночные данные и найденные новости.

ВАЖНО:

Нельзя выдавать предположение за установленную причину.

Если событие произошло одновременно с движением рынка,
используй формулировки:

"возможный фактор"

"рынок мог отреагировать"

"совпадает по времени"

"прямая причинность не подтверждена"

Если есть подтвержденная связь из надежного источника,
можно сказать, что событие непосредственно связано с движением.

=== MARKET DATA ===

{market_text}

=== NEWS RESEARCH ===

{news}

=== ФОРМАТ ===

TRD PULSE ⚡

📊 РЫНОК
Кратко опиши состояние рынка.

₿ BTC
Цена и главное движение.

Ξ ETH
Цена и главное движение.

🔥 ЛИДЕРЫ
2–4 наиболее заметных движения.

🔻 СЛАБЫЕ
2–4 наиболее заметных падения.

🌍 МИР
Только действительно важные события.

🏦 МАКРО
ФРС / ставки / инфляция / доллар / облигации,
если это сейчас действительно актуально.

🧠 ЧТО ПРОИСХОДИТ
Свяжи рыночное движение с новостями,
но четко отделяй факты от интерпретаций.

⚠️ РИСК
Что сейчас может резко изменить картину.

Не давай торговую рекомендацию и не пиши "покупать" или "продавать".

Стиль:

коротко;
плотно;
профессионально;
без воды;
без кликбейта.

Ответ должен быть готов для публикации в Telegram.
"""

    return await openai_response(
        prompt,
        web_search=False,
    )


# ============================================================
# TELEGRAM HELPERS
# ============================================================

def is_admin(update: Update) -> bool:
    """
    Пока используем простой список Telegram user ID.

    ADMIN_USER_ID задаётся через environment.
    """

    admin_id = os.getenv("ADMIN_USER_ID")

    if not admin_id:
        return False

    user = update.effective_user

    if not user:
        return False

    return str(user.id) == str(admin_id)


async def send_long_message(
    update: Update,
    text: str,
) -> None:

    if not update.message:
        return

    # Telegram имеет ограничение около 4096 символов.
    chunk_size = 3900

    for i in range(0, len(text), chunk_size):
        await update.message.reply_text(
            text[i:i + chunk_size]
        )


# ============================================================
# COMMANDS
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    text = """
TRD Pulse ⚡

Доступные команды:

/market — состояние крипторынка
/news — важные новости и макро
/pulse — полный TRD Pulse
/publish — создать и опубликовать Pulse

Бот работает с реальными рыночными данными и Web Search.
"""

    await update.message.reply_text(text)


async def market(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    try:
        data = await get_market_data()
        text = format_market_data(data)

        await send_long_message(
            update,
            "📊 TRD MARKET\n\n" + text,
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

    if not is_admin(update):
        await update.message.reply_text(
            "Команда доступна только администратору."
        )
        return

    try:
        await update.message.reply_text(
            "🔎 Ищу актуальные новости..."
        )

        result = await get_news_analysis()

        await send_long_message(
            update,
            "🌍 TRD NEWS\n\n" + result,
        )

    except Exception as exc:
        logger.exception("News error")

        await update.message.reply_text(
            f"Ошибка поиска новостей: {exc}"
        )


async def pulse(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not is_admin(update):
        await update.message.reply_text(
            "Команда доступна только администратору."
        )
        return

    try:
        await update.message.reply_text(
            "⚡ Собираю TRD Pulse..."
        )

        market_data = await get_market_data()
        news_data = await get_news_analysis()

        result = await generate_pulse(
            market_data,
            news_data,
        )

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

    if not is_admin(update):
        await update.message.reply_text(
            "Команда доступна только администратору."
        )
        return

    try:
        await update.message.reply_text(
            "⚡ Создаю TRD Pulse и готовлю публикацию..."
        )

        market_data = await get_market_data()
        news_data = await get_news_analysis()

        result = await generate_pulse(
            market_data,
            news_data,
        )

        await context.bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=result,
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
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    logger.exception(
        "Unhandled exception",
        exc_info=context.error,
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

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "TRD Pulse started. Model=%s",
        OPENAI_MODEL,
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
