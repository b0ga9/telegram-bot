import os
import re
import html
import logging
from typing import Optional

import httpx

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
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

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("trd-pulse")


# ============================================================
# CONFIG CHECK
# ============================================================

def check_config():
    required = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "ADMIN_USER_ID": ADMIN_USER_ID,
    }

    missing = [key for key, value in required.items() if not value]

    if missing:
        raise RuntimeError(
            "Не заданы переменные окружения: "
            + ", ".join(missing)
        )


# ============================================================
# ADMIN
# ============================================================

def is_admin(update: Update) -> bool:
    if not update.effective_user:
        return False

    if not ADMIN_USER_ID:
        return False

    return str(update.effective_user.id) == str(ADMIN_USER_ID)


async def admin_only(update: Update) -> bool:
    if is_admin(update):
        return True

    if update.message:
        await update.message.reply_text(
            "⛔ Команда доступна только администратору."
        )

    return False


# ============================================================
# HTTP
# ============================================================

async def get_json(url: str, params: Optional[dict] = None):
    timeout = httpx.Timeout(20.0)

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

    total_market_cap = data.get("total_market_cap", {}).get("usd", 0)
    total_volume = data.get("total_volume", {}).get("usd", 0)

    market_cap_change = data.get(
        "market_cap_change_percentage_24h_usd",
        0,
    )

    btc_dominance = data.get(
        "market_cap_percentage", {}
    ).get("btc", 0)

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
        key=lambda x: x.get("price_change_percentage_24h") or 0,
        reverse=True,
    )[:5]

    losers = sorted(
        coins,
        key=lambda x: x.get("price_change_percentage_24h") or 0,
    )[:5]

    return {
        "market_cap": total_market_cap,
        "volume": total_volume,
        "market_cap_change": market_cap_change,
        "btc_dominance": btc_dominance,
        "btc": btc,
        "eth": eth,
        "gainers": gainers,
        "losers": losers,
    }


def format_usd(value):
    if value is None:
        return "—"

    value = float(value)

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

    value = float(value)

    if value >= 1000:
        return f"${value:,.0f}"

    if value >= 1:
        return f"${value:,.2f}"

    return f"${value:.4f}"


def format_percent(value):
    if value is None:
        return "—"

    return f"{float(value):+.2f}%"


def format_market_data(data):
    btc = data["btc"]
    eth = data["eth"]

    lines = [
        "📊 <b>TRD MARKET</b>",
        "",
        f"🌐 Капитализация: <b>{format_usd(data['market_cap'])}</b>",
        f"💰 Объём 24ч: <b>{format_usd(data['volume'])}</b>",
        f"📈 Изменение рынка: <b>{format_percent(data['market_cap_change'])}</b>",
        f"₿ BTC dominance: <b>{data['btc_dominance']:.2f}%</b>",
        "",
    ]

    if btc:
        lines.append(
            f"₿ <b>BTC</b> {format_price(btc.get('current_price'))} "
            f"({format_percent(btc.get('price_change_percentage_24h'))})"
        )

    if eth:
        lines.append(
            f"♦️ <b>ETH</b> {format_price(eth.get('current_price'))} "
            f"({format_percent(eth.get('price_change_percentage_24h'))})"
        )

    lines.append("")
    lines.append("🔥 <b>TOP GAINERS</b>")

    for coin in data["gainers"]:
        symbol = (coin.get("symbol") or "").upper()
        name = coin.get("name") or symbol
        change = format_percent(
            coin.get("price_change_percentage_24h")
        )

        lines.append(
            f"• <b>{symbol}</b> {name} — {change}"
        )

    lines.append("")
    lines.append("🔻 <b>TOP LOSERS</b>")

    for coin in data["losers"]:
        symbol = (coin.get("symbol") or "").upper()
        name = coin.get("name") or symbol
        change = format_percent(
            coin.get("price_change_percentage_24h")
        )

        lines.append(
            f"• <b>{symbol}</b> {name} — {change}"
        )

    return "\n".join(lines)


# ============================================================
# OPENAI
# ============================================================

async def openai_response(
    prompt: str,
    *,
    web_search: bool = False,
    max_output_tokens: int = 700,
):
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "reasoning": {
            "effort": "low",
        },
        "max_output_tokens": max_output_tokens,
    }

    if web_search:
        payload["tools"] = [
            {
                "type": "web_search",
            }
        ]

    timeout = httpx.Timeout(
        connect=20.0,
        read=60.0,
        write=20.0,
        pool=20.0,
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            OPENAI_API,
            headers=headers,
            json=payload,
        )

    if response.status_code != 200:
        try:
            error_data = response.json()
        except Exception:
            error_data = response.text

        if response.status_code == 429:
            raise RuntimeError(
                "OpenAI временно ограничил запросы.\n\n"
                f"{error_data}"
            )

        raise RuntimeError(
            f"OpenAI API error {response.status_code}: "
            f"{error_data}"
        )

    data = response.json()

    # Responses API normally exposes output_text.
    text = data.get("output_text")

    if text:
        return text.strip()

    # Fallback parser.
    output = data.get("output", [])

    result = []

    for item in output:
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                result.append(content.get("text", ""))

    text = "\n".join(result).strip()

    if not text:
        raise RuntimeError(
            "OpenAI не вернул текстовый ответ."
        )

    return text


# ============================================================
# NEWS
# ============================================================

NEWS_PROMPT = """
Ты редактор короткого Telegram-канала TRD Pulse.

Нужно подготовить КОРОТКУЮ крипто-финансовую новость.

Найди только действительно важные события за последние 24 часа.

Приоритет:
- Bitcoin и Ethereum
- крипторынок
- ФРС и ЕЦБ
- инфляция
- ставки
- доллар
- доходности облигаций
- фондовый рынок
- золото
- нефть
- важные геополитические события, если они реально влияют на рынки.

Формат:

📰 **ЗАГОЛОВОК**

1–2 коротких предложения с фактом.

**Почему важно:** одно короткое предложение.

Если есть источник, добавь:
🔗 [Источник](URL)

Правила:
- максимум 3 новости;
- каждая новость очень короткая;
- не писать длинный анализ;
- не писать прогнозы;
- не давать торговых рекомендаций;
- не использовать фразы вроде «вам стоит купить»;
- только важная информация;
- отличай подтверждённый факт от предположения;
- не придумывай события;
- используй Markdown;
- ссылки обязательно оформляй как [Название](URL).
"""


async def get_news_analysis():
    return await openai_response(
        NEWS_PROMPT,
        web_search=True,
        max_output_tokens=650,
    )


# ============================================================
# PULSE
# ============================================================

async def generate_pulse():
    market = await get_market_data()

    news = await get_news_analysis()

    market_text = format_market_data(market)

    prompt = f"""
Ты главный редактор TRD Pulse.

Создай короткий Telegram-пост о текущем состоянии рынка.

Это НЕ длинный отчёт.

Максимум примерно 150–200 слов.

Используй данные рынка и новости ниже.

РЫНОК:
{market_text}

НОВОСТИ:
{news}

Формат:

⚡ **TRD PULSE**

Короткий заголовок.

**Рынок**
1–2 предложения.

**Главное**
• короткий пункт
• короткий пункт
• короткий пункт

**Что важно**
Одно короткое предложение.

В конце:
TRD Pulse · market update

Не давать торговых рекомендаций.
Не писать «покупать», «продавать», «лонг», «шорт».
Не повторять огромные списки данных.
Текст должен выглядеть как профессиональный Telegram-пост.
"""


    return await openai_response(
        prompt,
        web_search=False,
        max_output_tokens=650,
    )


# ============================================================
# MARKDOWN -> TELEGRAM HTML
# ============================================================

def markdown_to_telegram_html(text: str) -> str:
    """
    Преобразует ограниченный Markdown в безопасный Telegram HTML.
    """

    placeholders = {}

    def save_placeholder(value):
        key = f"___TRD_PLACEHOLDER_{len(placeholders)}___"
        placeholders[key] = value
        return key

    # Protect URLs inside Markdown links.
    def protect_link(match):
        label = match.group(1)
        url = match.group(2)

        safe_label = html.escape(label)
        safe_url = html.escape(url, quote=True)

        return save_placeholder(
            f'<a href="{safe_url}">{safe_label}</a>'
        )

    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s\)]+)\)",
        protect_link,
        text,
    )

    # Protect code.
    def protect_code(match):
        value = html.escape(match.group(1))
        return save_placeholder(
            f"<code>{value}</code>"
        )

    text = re.sub(
        r"`([^`]+)`",
        protect_code,
        text,
    )

    # Escape remaining HTML.
    text = html.escape(text)

    # Bold italic.
    text = re.sub(
        r"\*\*\*(.+?)\*\*\*",
        r"<b><i>\1</i></b>",
        text,
    )

    # Bold.
    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text,
    )

    # Italic.
    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        r"<i>\1</i>",
        text,
    )

    # Blockquotes.
    lines = text.splitlines()

    result = []

    for line in lines:
        if line.startswith("&gt; "):
            result.append(
                f"<blockquote>{line[5:]}</blockquote>"
            )
        else:
            result.append(line)

    text = "\n".join(result)

    # Restore placeholders.
    for key, value in placeholders.items():
        text = text.replace(key, value)

    return text.strip()


# ============================================================
# MESSAGE SENDING
# ============================================================

async def send_long_message(
    message,
    text: str,
    *,
    reply_markup=None,
):
    """
    Telegram message limit ≈ 4096 characters.
    """

    MAX_LENGTH = 3900

    if len(text) <= MAX_LENGTH:
        try:
            return await message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception:
            return await message.reply_text(
                re.sub("<[^>]+>", "", text),
                reply_markup=reply_markup,
            )

    parts = []

    current = ""

    for paragraph in text.split("\n\n"):
        candidate = (
            current + "\n\n" + paragraph
            if current
            else paragraph
        )

        if len(candidate) > MAX_LENGTH:
            if current:
                parts.append(current)

            current = paragraph
        else:
            current = candidate

    if current:
        parts.append(current)

    for i, part in enumerate(parts):
        await message.reply_text(
            part,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=(
                reply_markup
                if i == len(parts) - 1
                else None
            ),
        )


# ============================================================
# PUBLISH UI
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


def confirm_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Опубликовать",
                    callback_data="publish_confirm",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🖼 Заменить фото",
                    callback_data="publish_replace_photo",
                ),
                InlineKeyboardButton(
                    "❌ Отмена",
                    callback_data="publish_cancel",
                ),
            ],
        ]
    )


# ============================================================
# COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    text = """
⚡ <b>TRD PULSE</b>

Панель управления ботом.

<b>Команды:</b>

/market — состояние рынка
/news — важные новости
/pulse — короткий TRD Pulse
/publish — подготовить публикацию

<b>Публикация:</b>

1. Создаём текст.
2. Выбираем фото или текст.
3. Смотрим предпросмотр.
4. Подтверждаем публикацию.
"""

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


async def market_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    try:
        await update.message.reply_text(
            "⏳ Получаю данные рынка..."
        )

        data = await get_market_data()

        text = format_market_data(data)

        await send_long_message(
            update.message,
            text,
        )

    except Exception as e:
        logger.exception("Market error")

        await update.message.reply_text(
            f"❌ Ошибка получения рынка:\n{e}"
        )


async def news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    try:
        await update.message.reply_text(
            "📰 Ищу только важные новости..."
        )

        news = await get_news_analysis()

        context.user_data["last_news"] = news

        html_text = markdown_to_telegram_html(news)

        await send_long_message(
            update.message,
            html_text,
        )

    except Exception as e:
        logger.exception("News error")

        await update.message.reply_text(
            f"❌ Ошибка поиска новостей:\n{e}"
        )


async def pulse_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    try:
        await update.message.reply_text(
            "⚡ Собираю короткий TRD Pulse..."
        )

        pulse = await generate_pulse()

        context.user_data["last_pulse"] = pulse
        context.user_data["publish_text"] = pulse

        html_text = markdown_to_telegram_html(pulse)

        await send_long_message(
            update.message,
            html_text,
        )

    except Exception as e:
        logger.exception("Pulse error")

        await update.message.reply_text(
            f"❌ Ошибка создания Pulse:\n{e}"
        )


async def publish_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    text = context.user_data.get("publish_text")

    if not text:
        await update.message.reply_text(
            "У меня пока нет текста для публикации.\n\n"
            "Сначала используй /news или /pulse."
        )
        return

    context.user_data["publish_mode"] = None
    context.user_data["publish_photo"] = None

    await update.message.reply_text(
        "📤 <b>TRD PULSE — публикация</b>\n\n"
        "Как опубликовать пост?",
        parse_mode=ParseMode.HTML,
        reply_markup=publish_keyboard(),
    )


# ============================================================
# CALLBACKS
# ============================================================

async def publish_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(update):
        await query.edit_message_text(
            "⛔ Доступ запрещён."
        )
        return

    action = query.data

    # --------------------------------------------------------
    # TEXT ONLY
    # --------------------------------------------------------

    if action == "publish_text":
        context.user_data["publish_mode"] = "text"
        context.user_data["publish_photo"] = None

        text = context.user_data.get("publish_text", "")

        preview = markdown_to_telegram_html(text)

        await query.edit_message_text(
            "👀 <b>ПРЕДПРОСМОТР</b>\n\n"
            + preview
            + "\n\n"
            "Опубликовать этот пост?",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_keyboard(),
        )

        return

    # --------------------------------------------------------
    # PHOTO
    # --------------------------------------------------------

    if action == "publish_photo":
        context.user_data["publish_mode"] = "photo"

        await query.edit_message_text(
            "🖼 <b>Отправь фотографию</b>\n\n"
            "Можно отправить баннер монеты, "
            "график или изображение новости.\n\n"
            "После получения фото я покажу предпросмотр.",
            parse_mode=ParseMode.HTML,
        )

        return

    # --------------------------------------------------------
    # REPLACE PHOTO
    # --------------------------------------------------------

    if action == "publish_replace_photo":
        context.user_data["publish_mode"] = "photo"
        context.user_data["publish_photo"] = None

        await query.edit_message_text(
            "🖼 Отправь новую фотографию."
        )

        return

    # --------------------------------------------------------
    # CONFIRM
    # --------------------------------------------------------

    if action == "publish_confirm":
        await publish_to_channel(update, context)
        return

    # --------------------------------------------------------
    # CANCEL
    # --------------------------------------------------------

    if action == "publish_cancel":
        context.user_data.pop("publish_mode", None)
        context.user_data.pop("publish_photo", None)

        await query.edit_message_text(
            "❌ Публикация отменена."
        )


# ============================================================
# PHOTO HANDLER
# ============================================================

async def photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    mode = context.user_data.get("publish_mode")

    if mode != "photo":
        await update.message.reply_text(
            "Сначала используй /publish и выбери "
            "«🖼 С фотографией»."
        )
        return

    if not update.message.photo:
        return

    photo = update.message.photo[-1]

    context.user_data["publish_photo"] = photo.file_id

    text = context.user_data.get("publish_text", "")

    preview = markdown_to_telegram_html(text)

    # Telegram caption has a much smaller limit than a message.
    if len(preview) > 1000:
        preview = preview[:950].rstrip() + "…"

    await update.message.reply_photo(
        photo=photo.file_id,
        caption=preview,
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    mode = context.user_data.get("publish_mode")

    if mode == "photo":
        await update.message.reply_text(
            "🖼 Сейчас я жду фотографию.\n\n"
            "Отправь изображение как фото."
        )
        return

    await update.message.reply_text(
        "Используй команды:\n\n"
        "/market\n"
        "/news\n"
        "/pulse\n"
        "/publish"
    )


# ============================================================
# ACTUAL PUBLISH
# ============================================================

async def publish_to_channel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    text = context.user_data.get("publish_text")

    if not text:
        await query.edit_message_text(
            "❌ Нет текста для публикации."
        )
        return

    html_text = markdown_to_telegram_html(text)

    photo_id = context.user_data.get("publish_photo")
    mode = context.user_data.get("publish_mode")

    try:
        if mode == "photo" and photo_id:
            caption = html_text

            if len(caption) > 1000:
                caption = caption[:950].rstrip() + "…"

            await context.bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=photo_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )

        else:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=html_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )

        context.user_data.pop("publish_mode", None)
        context.user_data.pop("publish_photo", None)

        await query.edit_message_text(
            "✅ <b>Опубликовано в TRD Pulse.</b>",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.exception("Publish error")

        await query.edit_message_text(
            "❌ Ошибка публикации:\n\n"
            f"{e}"
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception(
        "Unhandled exception:",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
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
        CommandHandler("market", market_command)
    )

    application.add_handler(
        CommandHandler("news", news_command)
    )

    application.add_handler(
        CommandHandler("pulse", pulse_command)
    )

    application.add_handler(
        CommandHandler("publish", publish_command)
    )

    application.add_handler(
        CallbackQueryHandler(
            publish_callback,
            pattern=r"^publish_",
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

    application.add_error_handler(
        error_handler
    )

    logger.info("TRD Pulse bot started.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
