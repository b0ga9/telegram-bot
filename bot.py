import logging
import os
from typing import Any

import httpx
from telegram import Update
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
# CONFIG CHECK
# ============================================================

def check_config() -> None:
    missing = []

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHANNEL_ID:
        missing.append("TELEGRAM_CHANNEL_ID")

    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")

    if not os.getenv("ADMIN_USER_ID"):
        missing.append("ADMIN_USER_ID")

    if missing:
        raise RuntimeError(
            "Не заданы переменные окружения: "
            + ", ".join(missing)
        )


# ============================================================
# HTTP JSON
# ============================================================

async def get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | list[Any]:

    response = await client.get(
        url,
        params=params,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# COINGECKO
# ============================================================

async def get_market_data() -> dict[str, Any]:

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={
            "User-Agent": "TRD-Pulse/2.0",
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
        raise RuntimeError(
            "Некорректный ответ CoinGecko global"
        )

    if not isinstance(coins, list):
        raise RuntimeError(
            "Некорректный ответ CoinGecko markets"
        )

    market = global_data.get("data", {})

    cleaned = []

    for coin in coins:

        cleaned.append(
            {
                "id": coin.get("id"),
                "symbol": str(
                    coin.get("symbol", "")
                ).upper(),
                "name": coin.get("name"),

                "price_usd": coin.get(
                    "current_price"
                ),

                "market_cap": coin.get(
                    "market_cap"
                ),

                "volume_24h": coin.get(
                    "total_volume"
                ),

                "change_1h": coin.get(
                    "price_change_percentage_1h_in_currency"
                ),

                "change_24h": coin.get(
                    "price_change_percentage_24h_in_currency"
                ),

                "change_7d": coin.get(
                    "price_change_percentage_7d_in_currency"
                ),
            }
        )

    gainers = sorted(
        cleaned,
        key=lambda x: x.get("change_24h")
        if x.get("change_24h") is not None
        else -999,
        reverse=True,
    )[:10]

    losers = sorted(
        cleaned,
        key=lambda x: x.get("change_24h")
        if x.get("change_24h") is not None
        else 999,
    )[:10]

    btc = next(
        (
            coin
            for coin in cleaned
            if coin["symbol"] == "BTC"
        ),
        None,
    )

    eth = next(
        (
            coin
            for coin in cleaned
            if coin["symbol"] == "ETH"
        ),
        None,
    )

    return {
        "market_cap_usd": market.get(
            "total_market_cap", {}
        ).get("usd"),

        "volume_24h_usd": market.get(
            "total_volume", {}
        ).get("usd"),

        "market_cap_change_24h": market.get(
            "market_cap_change_percentage_24h_usd"
        ),

        "btc_dominance": market.get(
            "market_cap_percentage", {}
        ).get("btc"),

        "active_cryptocurrencies": market.get(
            "active_cryptocurrencies"
        ),

        "markets": market.get(
            "markets"
        ),

        "btc": btc,
        "eth": eth,

        "top_gainers": gainers,
        "top_losers": losers,
    }


# ============================================================
# FORMAT MARKET
# ============================================================

def format_market_data(
    data: dict[str, Any],
) -> str:

    lines = []

    market_cap = data.get(
        "market_cap_usd"
    )

    volume = data.get(
        "volume_24h_usd"
    )

    market_change = data.get(
        "market_cap_change_24h"
    )

    dominance = data.get(
        "btc_dominance"
    )

    active = data.get(
        "active_cryptocurrencies"
    )

    if market_cap:
        lines.append(
            f"Total market cap: "
            f"${market_cap:,.0f}"
        )

    if volume:
        lines.append(
            f"24h volume: "
            f"${volume:,.0f}"
        )

    if market_change is not None:
        lines.append(
            f"Market cap 24h: "
            f"{market_change:+.2f}%"
        )

    if dominance is not None:
        lines.append(
            f"BTC dominance: "
            f"{dominance:.2f}%"
        )

    if active:
        lines.append(
            f"Active cryptocurrencies: "
            f"{active:,}"
        )

    lines.append("")

    for label in ("btc", "eth"):

        coin = data.get(label)

        if not coin:
            continue

        price = coin.get(
            "price_usd"
        )

        if price is None:
            continue

        lines.append(
            f"{coin['symbol']}: "
            f"${price:,.2f} | "
            f"1h {coin.get('change_1h') or 0:+.2f}% | "
            f"24h {coin.get('change_24h') or 0:+.2f}% | "
            f"7d {coin.get('change_7d') or 0:+.2f}%"
        )

    lines.append("")
    lines.append("TOP GAINERS")

    for coin in data["top_gainers"][:5]:

        change = coin.get(
            "change_24h"
        )

        if change is None:
            continue

        lines.append(
            f"{coin['symbol']}: "
            f"{change:+.2f}%"
        )

    lines.append("")
    lines.append("TOP LOSERS")

    for coin in data["top_losers"][:5]:

        change = coin.get(
            "change_24h"
        )

        if change is None:
            continue

        lines.append(
            f"{coin['symbol']}: "
            f"{change:+.2f}%"
        )

    return "\n".join(lines)


# ============================================================
# OPENAI
# ============================================================

async def openai_response(
    prompt: str,
    web_search: bool = False,
) -> str:

    payload = {
        "model": OPENAI_MODEL,

        "input": prompt,

        "reasoning": {
            "effort": "low"
        },

        "max_output_tokens": 1800,
    }

    if web_search:

        payload["tools"] = [
            {
                "type": "web_search"
            }
        ]

    headers = {
        "Authorization":
            f"Bearer {OPENAI_API_KEY}",

        "Content-Type":
            "application/json",
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

    output_text = data.get(
        "output_text"
    )

    if output_text:
        return output_text.strip()

    result = []

    for item in data.get(
        "output",
        []
    ):

        for content in item.get(
            "content",
            []
        ):

            if content.get(
                "type"
            ) == "output_text":

                text = content.get(
                    "text"
                )

                if text:
                    result.append(
                        text
                    )

    if result:
        return "\n".join(
            result
        ).strip()

    raise RuntimeError(
        "OpenAI не вернул текстовый результат."
    )


# ============================================================
# NEWS
# ============================================================

async def get_news_analysis() -> str:

    prompt = """
Ты — news research engine проекта TRD Pulse.

Используй Web Search.

Найди наиболее важные события последних 24 часов,
которые способны повлиять на финансовые рынки.

ПРИОРИТЕТ:

1. Федеральная резервная система США
2. Европейский центральный банк
3. другие центральные банки
4. инфляция
5. CPI / PCE
6. NFP / занятость
7. безработица
8. процентные ставки
9. доходности облигаций
10. доллар США
11. S&P 500
12. Nasdaq
13. золото
14. нефть
15. Bitcoin
16. Ethereum
17. крипторынок
18. геополитика
19. санкции
20. важные заявления правительств

НЕ ПРИДУМЫВАЙ СОБЫТИЯ.

Для каждого действительно важного события:

• Что произошло
• Когда произошло
• Какой рынок затрагивает
• Почему это важно
• Факт или интерпретация

ОСОБО ВАЖНО:

Если источник не подтверждает причинность,
не говори, что событие вызвало движение рынка.

Используй:

"возможный фактор"

"рынок мог отреагировать"

"совпадает по времени"

"прямая причинность не подтверждена"

Если причинность подтверждена источниками,
это можно указать.

В конце каждого события укажи:

Источник: название источника + ссылка.

Пиши на русском.

Будь кратким.

Не добавляй несущественные новости.
"""

    return await openai_response(
        prompt,
        web_search=True,
    )


# ============================================================
# PULSE
# ============================================================

async def generate_pulse(
    market_data: dict[str, Any],
    news: str,
) -> str:

    market_text = format_market_data(
        market_data
    )

    prompt = f"""
Ты — главный аналитический движок TRD Pulse.

Твоя задача — объединить реальные рыночные данные
с research report новостей.

========================
MARKET DATA
========================

{market_text}

========================
NEWS RESEARCH
========================

{news}

========================
ПРАВИЛА
========================

Не выдавай предположение за факт.

Если событие произошло одновременно
с движением рынка, но причинность не доказана:

"возможный фактор"

"рынок мог отреагировать"

"совпадает по времени"

"прямая причинность не подтверждена"

Не пиши:

"рынок вырос из-за X"

если источник не подтверждает такую связь.

Не давай торговых рекомендаций.

Не используй:

"покупать"

"продавать"

"лонг"

"шорт"

========================
ФОРМАТ
========================

TRD PULSE ⚡

📊 РЫНОК

Краткая оценка общего состояния рынка.

₿ BTC

Цена + 24h + ключевой контекст.

Ξ ETH

Цена + 24h + ключевой контекст.

🔥 ЛИДЕРЫ

2–4 наиболее заметных движения.

🔻 СЛАБЫЕ

2–4 наиболее заметных падения.

📈 BTC DOMINANCE

Что происходит с доминацией BTC.

🌍 МИР

Только действительно важные события.

🏦 МАКРО

ФРС / ставки / инфляция / доллар /
облигации — только если актуально.

🧠 ЧТО ПРОИСХОДИТ

Главная аналитическая связка между рынком
и событиями.

Четко отделяй факт от интерпретации.

⚠️ РИСК

Что способно резко изменить ситуацию.

========================
СТИЛЬ
========================

Коротко.

Плотно.

Профессионально.

Без воды.

Без кликбейта.

Не повторяй одну и ту же информацию.

Пост должен быть готов для Telegram.

Не добавляй служебные комментарии.
"""

    return await openai_response(
        prompt,
        web_search=False,
    )


# ============================================================
# ADMIN
# ============================================================

def is_admin(
    update: Update,
) -> bool:

    admin_id = os.getenv(
        "ADMIN_USER_ID"
    )

    user = update.effective_user

    if not admin_id or not user:
        return False

    return str(user.id) == str(
        admin_id
    )


# ============================================================
# TELEGRAM
# ============================================================

async def send_long_message(
    update: Update,
    text: str,
) -> None:

    if not update.message:
        return

    chunk_size = 3900

    for i in range(
        0,
        len(text),
        chunk_size,
    ):

        await update.message.reply_text(
            text[
                i:i + chunk_size
            ]
        )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    text = """
TRD Pulse ⚡

Команды:

/market — состояние крипторынка
/news — важные новости и макро
/pulse — полный TRD Pulse
/publish — создать и опубликовать Pulse

TRD Pulse использует реальные рыночные данные
и Web Search.
"""

    await update.message.reply_text(
        text
    )


# ============================================================
# MARKET
# ============================================================

async def market(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    try:

        data = await get_market_data()

        text = format_market_data(
            data
        )

        await send_long_message(
            update,
            "📊 TRD MARKET\n\n"
            + text,
        )

    except Exception as exc:

        logger.exception(
            "Market error"
        )

        await update.message.reply_text(
            f"Ошибка получения рынка: {exc}"
        )


# ============================================================
# NEWS
# ============================================================

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
            "🌍 TRD NEWS\n\n"
            + result,
        )

    except Exception as exc:

        logger.exception(
            "News error"
        )

        await update.message.reply_text(
            f"Ошибка поиска новостей: {exc}"
        )


# ============================================================
# PULSE
# ============================================================

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

        market_data = (
            await get_market_data()
        )

        news_data = (
            await get_news_analysis()
        )

        result = await generate_pulse(
            market_data,
            news_data,
        )

        await send_long_message(
            update,
            result,
        )

    except Exception as exc:

        logger.exception(
            "Pulse error"
        )

        await update.message.reply_text(
            f"Ошибка генерации Pulse: {exc}"
        )


# ============================================================
# PUBLISH
# ============================================================

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
            "⚡ Создаю TRD Pulse..."
        )

        market_data = (
            await get_market_data()
        )

        news_data = (
            await get_news_analysis()
        )

        result = await generate_pulse(
            market_data,
            news_data,
        )

        # В первой версии НЕ используем HTML parse mode.
        # Это предотвращает ошибки Telegram,
        # если модель вернет специальные символы.

        await context.bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=result,
        )

        await update.message.reply_text(
            "✅ TRD Pulse опубликован."
        )

    except Exception as exc:

        logger.exception(
            "Publish error"
        )

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

    logger.error(
        "Unhandled exception: %s",
        context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    check_config()

    application = (
        Application.builder()
        .token(
            TELEGRAM_BOT_TOKEN
        )
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "market",
            market,
        )
    )

    application.add_handler(
        CommandHandler(
            "news",
            news,
        )
    )

    application.add_handler(
        CommandHandler(
            "pulse",
            pulse,
        )
    )

    application.add_handler(
        CommandHandler(
            "publish",
            publish,
        )
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
